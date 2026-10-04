"""maubot-bridge-relay: relay messages between two bridged Matrix rooms.

- Messenger room -> Signal room: everything (text + media)
- Signal room -> Messenger room: only messages starting with a trigger (default "!m")
- Native replies in both directions (via an event-ID mapping)
- "!r" reply or a 🔎/🔍 reaction in the Signal room lists the Messenger reactions on a message
- "!health" reports bridge login status via the mautrix provisioning API
- Bridge warnings from the bridge-bot rooms are forwarded to an alert room
"""
import re
import time

import aiohttp
from typing import Type

from maubot import Plugin, MessageEvent
from maubot.handlers import event
from mautrix.types import EventType, MessageType, RelationType
from mautrix.util.async_db import Connection, UpgradeTable
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

TEXT_TYPES = {MessageType.TEXT, MessageType.EMOTE}
MEDIA_TYPES = {MessageType.IMAGE, MessageType.VIDEO, MessageType.FILE, MessageType.AUDIO}
BRIDGE_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")  # e.g. " (Signal)" added by the bridge
MAX_AGE_MS = 5 * 60 * 1000  # ignore old/backfilled events (e.g. history pulled in by the bridge)
MAP_KEEP_DAYS = 90  # how long to remember which message became which (for replies)

upgrade_table = UpgradeTable()


@upgrade_table.register(description="Event mapping for replies")
async def upgrade_v1(conn: Connection) -> None:
    await conn.execute(
        """CREATE TABLE event_map (
            src_room  TEXT NOT NULL,
            src_event TEXT NOT NULL,
            dst_room  TEXT NOT NULL,
            dst_event TEXT NOT NULL,
            ts        BIGINT NOT NULL,
            PRIMARY KEY (src_room, src_event)
        )"""
    )


@upgrade_table.register(description="Reaction tracking for !r")
async def upgrade_v2(conn: Connection) -> None:
    await conn.execute(
        """CREATE TABLE reactions (
            room_id        TEXT NOT NULL,
            target_event   TEXT NOT NULL,
            sender         TEXT NOT NULL,
            reaction_event TEXT NOT NULL,
            reaction_key   TEXT NOT NULL,
            ts             BIGINT NOT NULL,
            PRIMARY KEY (room_id, target_event, sender)
        )"""
    )


def strip_reply_fallback(body: str) -> str:
    """Remove the old-style '> quoted text' block some clients put before a reply."""
    if not body.startswith("> "):
        return body
    lines = body.split("\n")
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        for key in ("messenger_room", "signal_room", "trigger", "messenger_label", "signal_label",
                    "alert_rooms", "alert_target", "health_command", "bridge_user", "bridges",
                    "reactions_command", "reactions_emojis"):
            helper.copy(key)


class RelayBot(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()
        self._last_health = {}
        self._last_lookup = {}

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable:
        return upgrade_table

    async def _remember(self, room_a, event_a, room_b, event_b) -> None:
        """Store the mapping in both directions, so replies work both ways."""
        now = int(time.time() * 1000)
        q = ("INSERT INTO event_map (src_room, src_event, dst_room, dst_event, ts) VALUES ($1, $2, $3, $4, $5) "
             "ON CONFLICT (src_room, src_event) DO NOTHING")
        await self.database.execute(q, room_a, event_a, room_b, event_b, now)
        await self.database.execute(q, room_b, event_b, room_a, event_a, now)
        await self.database.execute("DELETE FROM event_map WHERE ts < $1",
                                    now - MAP_KEEP_DAYS * 24 * 3600 * 1000)

    async def _lookup(self, room_id, event_id, target_room):
        return await self.database.fetchval(
            "SELECT dst_event FROM event_map WHERE src_room=$1 AND src_event=$2 AND dst_room=$3",
            room_id, event_id, target_room)

    async def _display_name(self, room_id, user_id) -> str:
        try:
            member = await self.client.get_state_event(room_id, EventType.ROOM_MEMBER, user_id)
            if member and member.displayname:
                return BRIDGE_SUFFIX.sub("", member.displayname) or member.displayname
        except Exception:
            pass
        return user_id.split(":")[0].lstrip("@")

    # --- Reactions: remember who reacted with what (one reaction per person, like Signal/Messenger) ---
    @event.on(EventType.REACTION)
    async def on_reaction(self, evt) -> None:
        if evt.room_id not in (self.config["messenger_room"], self.config["signal_room"]):
            return
        rel = evt.content.relates_to
        if not rel or not rel.event_id or not rel.key:
            return
        now = int(time.time() * 1000)

        # 🔎/🔍 on a forwarded message in the Signal group = show its Messenger reactions
        emojis = {e.replace("\ufe0f", "") for e in (self.config["reactions_emojis"] or [])}
        if (evt.room_id == self.config["signal_room"] and evt.sender != self.client.mxid
                and rel.key.replace("\ufe0f", "") in emojis and now - evt.timestamp < MAX_AGE_MS):
            if time.time() - self._last_lookup.get(rel.event_id, 0) >= 10:  # avoid double answers
                self._last_lookup[rel.event_id] = time.time()
                self.log.info(f"Reaction list requested via {rel.key} for {rel.event_id}")
                await self._reaction_list(evt.room_id, rel.event_id, rel.event_id)
            return
        await self.database.execute(
            "INSERT INTO reactions (room_id, target_event, sender, reaction_event, reaction_key, ts) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (room_id, target_event, sender) DO UPDATE SET "
            "reaction_event=excluded.reaction_event, reaction_key=excluded.reaction_key, ts=excluded.ts",
            evt.room_id, rel.event_id, evt.sender, evt.event_id, rel.key, now)
        await self.database.execute("DELETE FROM reactions WHERE ts < $1",
                                    now - MAP_KEEP_DAYS * 24 * 3600 * 1000)

    @event.on(EventType.ROOM_REDACTION)
    async def on_redaction(self, evt) -> None:
        redacts = getattr(evt, "redacts", None) or getattr(evt.content, "redacts", None)
        if redacts:
            await self.database.execute(
                "DELETE FROM reactions WHERE room_id=$1 AND reaction_event=$2", evt.room_id, redacts)

    async def _reaction_list(self, room_id, reply_to, answer_to) -> None:
        """reply_to = the forwarded message in the Signal group; answer_to = event our answer replies to."""
        signal_room, messenger_room = self.config["signal_room"], self.config["messenger_room"]
        if not reply_to:
            body = f"Reply to a forwarded message with {self.config['reactions_command']} to see its reactions."
        else:
            original = await self._lookup(signal_room, reply_to, messenger_room)
            if not original:
                body = "I don't know that message (only messages forwarded after the update are tracked)."
            else:
                rows = await self.database.fetch(
                    "SELECT sender, reaction_key FROM reactions WHERE room_id=$1 AND target_event=$2 ORDER BY ts",
                    messenger_room, original)
                if not rows:
                    body = "No reactions on Messenger yet."
                else:
                    counts = {}
                    for row in rows:
                        counts[row["reaction_key"]] = counts.get(row["reaction_key"], 0) + 1
                    summary = " · ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
                    lines = [summary]
                    for row in sorted(rows, key=lambda r: -counts[r["reaction_key"]]):
                        name = await self._display_name(messenger_room, row["sender"])
                        lines.append(f"{name}: {row['reaction_key']}")
                    body = "\n".join(lines)
        await self.client.send_message_event(room_id, EventType.ROOM_MESSAGE, {
            "msgtype": "m.text", "body": body,
            "m.relates_to": {"m.in_reply_to": {"event_id": answer_to}},
        })

    async def _check_bridge(self, bridge: dict) -> str:
        name = bridge.get("name", "Bridge")
        url = f"{str(bridge.get('url', '')).rstrip('/')}/_matrix/provision/v3/whoami"
        try:
            async with self.http.get(
                url,
                params={"user_id": self.config["bridge_user"]},
                headers={"Authorization": f"Bearer {bridge.get('secret', '')}"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return f"⚠️ {name}: API error (HTTP {resp.status})"
                data = await resp.json(content_type=None)
        except Exception as e:
            self.log.warning(f"Health check for {name} failed: {e!r}")
            return f"❌ {name}: not reachable (container down?)"
        logins = data.get("logins") or []
        if not logins:
            return f"❌ {name}: not logged in"
        lines = []
        for login in logins:
            state = (login.get("state") or {}).get("state_event") or login.get("state_event") or "UNKNOWN"
            icon = "✅" if state == "CONNECTED" else "⚠️"
            lines.append(f"{icon} {name}: {state.lower().replace('_', ' ')}")
        return "\n".join(lines)

    async def _health(self, room_id) -> None:
        now = time.time()
        if now - self._last_health.get(room_id, 0) < 10:
            return  # simple cooldown against spam
        self._last_health[room_id] = now
        lines = ["✅ Relay bot: running"]
        for bridge in self.config["bridges"] or []:
            lines.append(await self._check_bridge(bridge))
        await self.client.send_message_event(
            room_id, EventType.ROOM_MESSAGE, {"msgtype": "m.text", "body": "\n".join(lines)})

    @event.on(EventType.ROOM_MESSAGE)
    async def relay(self, evt: MessageEvent) -> None:
        # Never relay our own messages (prevents echo loops)
        self.log.info(f"Got {evt.content.msgtype} from {evt.sender} in {evt.room_id}")
        if evt.sender == self.client.mxid:
            return
        age = time.time() * 1000 - evt.timestamp
        if age > MAX_AGE_MS:
            self.log.info(f"Skip: too old ({age / 1000:.0f}s)")
            return

        content = evt.content

        # "!health" in the Signal group or the alert room (not Messenger, to avoid automated sends there)
        if (content.msgtype == MessageType.TEXT
                and (content.body or "").strip().lower() == str(self.config["health_command"]).lower()
                and evt.room_id in (self.config["signal_room"], self.config["alert_target"])):
            self.log.info(f"Health check requested in {evt.room_id}")
            await self._health(evt.room_id)
            return

        # "!r" as a reply in the Signal group: list the Messenger reactions on that message
        if evt.room_id == self.config["signal_room"] and content.msgtype == MessageType.TEXT:
            raw = content.serialize()
            r_to = ((raw.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")
            if strip_reply_fallback(raw.get("body") or "").strip().lower() == str(self.config["reactions_command"]).lower():
                self.log.info(f"Reaction list requested for {r_to}")
                await self._reaction_list(evt.room_id, r_to, evt.event_id)
                return

        # Forward bridge warnings (notices in the bridge-bot chats) to the alert room
        if evt.room_id in (self.config["alert_rooms"] or []):
            target = self.config["alert_target"]
            if target and content.msgtype == MessageType.NOTICE:
                text = (content.body or "")[:500]
                self.log.info(f"Forwarding bridge notice from {evt.room_id}")
                await self.client.send_message_event(
                    target, EventType.ROOM_MESSAGE, {"msgtype": "m.text", "body": f"⚠️ Bridge: {text}"})
            return

        rel = content.relates_to
        if rel and rel.rel_type == RelationType.REPLACE:
            self.log.info("Skip: edit")
            return
        is_media = content.msgtype in MEDIA_TYPES
        if content.msgtype not in TEXT_TYPES and not is_media:
            self.log.info("Skip: unsupported msgtype")
            return

        if evt.room_id == self.config["messenger_room"]:
            target, label, need_trigger = self.config["signal_room"], self.config["messenger_label"], False
        elif evt.room_id == self.config["signal_room"]:
            target, label, need_trigger = self.config["messenger_room"], self.config["signal_label"], True
        else:
            self.log.info(f"Skip: room not configured (messenger_room={self.config['messenger_room']}, "
                          f"signal_room={self.config['signal_room']})")
            return

        data = content.serialize()
        reply_to = ((data.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")
        body = data.get("body") or ""
        if reply_to and not is_media:
            body = strip_reply_fallback(body)
        filename = data.get("filename")
        # For media, "body" is only a caption if it differs from the filename
        text = (body if filename and body != filename else "") if is_media else body

        if need_trigger:
            trigger = self.config["trigger"]
            stripped = text.lstrip()
            if not (stripped == trigger or stripped.startswith(trigger + " ")):
                self.log.info("Skip: no trigger")
                return
            text = stripped[len(trigger):].strip()

        prefix = f"{await self._display_name(evt.room_id, evt.sender)} ({label})"

        if is_media:
            # Re-send the same file (same mxc:// URL) with our caption
            data["filename"] = filename or body
            data["body"] = f"{prefix}: {text}" if text else prefix
            for key in ("m.relates_to", "format", "formatted_body", "m.mentions"):
                data.pop(key, None)
        else:
            if not text:
                return
            data = {"msgtype": "m.text", "body": f"{prefix}: {text}"}

        if reply_to:
            mapped = await self._lookup(evt.room_id, reply_to, target)
            if mapped:
                data["m.relates_to"] = {"m.in_reply_to": {"event_id": mapped}}
                self.log.info(f"Reply: {reply_to} -> {mapped}")
            else:
                self.log.info(f"Reply target {reply_to} not found in map, sending as normal message")

        self.log.info(f"Relaying to {target}")
        new_event = await self.client.send_message_event(target, EventType.ROOM_MESSAGE, data)
        try:
            await self._remember(evt.room_id, evt.event_id, target, new_event)
        except Exception as e:
            self.log.warning(f"Could not store event mapping: {e!r}")
