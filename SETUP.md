# Setup guide

This guide takes you from a bare Debian VM to a working Messenger ⇄ Signal relay: a private Matrix homeserver, the mautrix-meta and mautrix-signal bridges in relay mode, and maubot running this plugin.

The hardest part was actually creating the facebook account for the relay. Have tried using this method for a few days now and the facebook account is still up. Will update the repo if this approach is unsafe.

> Commands use placeholders: `matrix.example.com` for your server, `@you:matrix.example.com` for your Matrix account, `/path/to/matrix-docker-ansible-deploy` for the playbook folder. Replace them with your own values.

---

## 0. What you need

- **A host** for a small VM, such as Proxmox. 2 vCPU, 4 GB RAM and 40 GB disk is plenty.
- **A domain** whose DNS provider is supported by Traefik/lego for the DNS-01 challenge (Cloudflare and many others).
- **A Tailscale account** This is what I used, to avoid opening ports for the server.
- **A second phone number for the Signal bot**, plus a phone that keeps that Signal account as its *primary device*. An old phone works, and so does a cloned Signal app on Android ("Dual apps" / app cloning). I luckily have dual SIM in my phone.
- **A Facebook account you can afford to lose.** Bridging is against Meta's terms of service (see [step 7](#7-messenger-bridge)).

### The accounts involved

| Account | What it is |
|---|---|
| `@you:matrix.example.com` | Your Matrix account. It logs in to both bridges and is used in Element |
| `@bot.maubot:matrix.example.com` | The relay bot, created by the playbook |
| Signal bot number | A Signal account that posts the forwarded messages in the Signal group |
| Messenger account | The Facebook account that sits in the Messenger group |

---

## 1. Create the VM (Proxmox guide since that's what I did)

1. Install **Debian** (12 or 13; check the playbook's prerequisites). Untick all desktop environments and keep only **SSH server** and **standard system utilities**.
2. In Proxmox, under **Options**, enable **Start at boot** and **QEMU Guest Agent**.
3. Inside the VM, as root:
   ```bash
   apt update && apt full-upgrade -y
   apt install -y sudo curl git qemu-guest-agent unattended-upgrades dnsutils
   systemctl start qemu-guest-agent
   timedatectl set-timezone Europe/Paris   # your time zone
   ```

---

I forgot setting the right timezone and it made reading logs quite difficult. So don't forget!


## 2. Tailscale (Optional)

Setup TailScale normally and disable key expiry.

---

## 3. DNS and certificates

1. **Create an A record** at your DNS provider: `matrix.example.com` → the VM's **Tailscale IP** (100.x.x.x).
   The server stays private, reachable only from your tailnet. The bridges only make outgoing connections, so **no ports need to be opened** on your router.
2. **Create an API token** at your DNS provider, so Traefik can complete the DNS-01 challenge.
3. **Verify the record:**
   ```bash
   dig +short matrix.example.com    # should print the 100.x.x.x address
   ```

---

## 4. Install Matrix with the playbook

This guide uses [matrix-docker-ansible-deploy](https://github.com/spantaleev/matrix-docker-ansible-deploy) and runs it **on the VM itself**.

### 4.1 Tools and playbook

```bash
apt install -y pipx just
pipx install --include-deps ansible
pipx ensurepath && source ~/.bashrc

git clone https://github.com/spantaleev/matrix-docker-ansible-deploy.git
cd matrix-docker-ansible-deploy
just roles
```

### 4.2 Inventory

```bash
mkdir -p inventory/host_vars/matrix.example.com
cp examples/vars.yml inventory/host_vars/matrix.example.com/vars.yml
```

Create `inventory/hosts`:
```ini
[matrix_servers]
matrix.example.com ansible_connection=local ansible_python_interpreter=/usr/bin/python3
```

### 4.3 vars.yml

Start from the example file you just copied. Then add or adjust the settings from [`examples/playbook-vars.example.yml`](examples/playbook-vars.example.yml) in this repo, step by step:

- **Server name.** It **can't be changed later**:
  ```yaml
  matrix_domain: matrix.example.com
  matrix_server_fqn_matrix: matrix.example.com   # otherwise Synapse ends up at matrix.matrix.example.com
  ```
- **Secrets.** The example file has them empty. Generate random ones:
  ```bash
  F=inventory/host_vars/matrix.example.com/vars.yml
  sed -i "s|^matrix_homeserver_generic_secret_key: ''|matrix_homeserver_generic_secret_key: '$(openssl rand -hex 32)'|" "$F"
  sed -i "s|^postgres_connection_password: ''|postgres_connection_password: '$(openssl rand -hex 32)'|" "$F"
  ```
- **No federation, no web client, no encryption in Element:** see the example file. The bridges in this setup run unencrypted.
- **DNS-01 certificates:** see the example file. Set `..._provider` to your DNS provider and fill in its environment variables. The `traefik_config_certificatesResolvers_acme_email` line already exists in the example `vars.yml`, so just fill it in.
- **Bridges:** for now, only `matrix_bridge_mautrix_meta_messenger_enabled: true` and `matrix_bridge_mautrix_signal_enabled: true`. Relay mode and maubot come later.

**Verify variable names before running.** The playbook renames variables over time. Any name that only appears in your own `vars.yml` is wrong:
```bash
grep -rlF "SOME_VARIABLE_NAME" . | grep -v inventory/
```
If nothing is printed, look up the current name in `docs/`.

### 4.4 Install and create your account

```bash
just install-all
just register-user you YOUR_PASSWORD yes    # "yes" = admin. Put a space before the command to keep it out of history
curl https://matrix.example.com/_matrix/client/versions   # JSON = server + certificate OK
```

Then log in with the **Element** app on your PC: homeserver `matrix.example.com`, username `you`.

---

## 5. Check that Element creates unencrypted rooms

The `force_disable` setting from 4.3 is served at `/.well-known/matrix/client`:
```bash
curl https://matrix.example.com/.well-known/matrix/client   # should contain "force_disable": true
```

Log out of Element and back in so it re-reads the setting. New rooms should show **"Send an unencrypted message…"** in the composer.

Element **desktop/web** honors this setting. The newer Element **mobile** app might not, so do the setup steps from your PC!

---

## 6. Signal bridge

1. **Set up the bot number** as the primary device on its phone (or a cloned Signal app).
2. **Link the bridge:** in Element on your PC, start a chat with `@signalbot:matrix.example.com` and send `login`. Then, on the bot phone, go to **Settings → Linked devices → +** and scan the QR code.
3. **Create the Signal group:** from your personal Signal, create a group that includes the bot number, and send a message. A matching **portal room** appears in Element.

The bot phone must come online **at least every 30 days**, or linked devices (the bridge) get unlinked.

---

## 7. Messenger bridge

### 7.1 The account

Brand-new Facebook accounts often get checkpointed or deactivated, especially if they're automated right away. What worked:

- **Create the account** from the same country as the server and IP, if possible. I used the server with TailScale as an exit node to have the server use the same IP as when I got the facebook cookies needed.
- **Use it normally for a few days:** add a profile picture, add friends, send a few messages by hand, and have someone add it to the group.
- **Turn on 2FA.**

Even then, it can get locked. Don't use an account linked to anything you care about, because Meta can enforce across linked accounts.

### 7.2 Log in from the server's location (Tailscale exit node)

The bridge logs in with **browser cookies**. To make the browser session come from the same IP as the server, route your PC through the VM:

```bash
cat > /etc/sysctl.d/99-tailscale.conf << 'EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF
sysctl -p /etc/sysctl.d/99-tailscale.conf
tailscale set --advertise-exit-node
```
Then:
1. In the Tailscale admin console, open the VM's **Edit route settings** and tick **Use as exit node**.
2. On your PC, select the VM as **exit node**. A "what is my IP" check should now show the server's location.

### 7.3 Copy the cookies

1. **Log in** to **facebook.com** in a private window. messenger.com may ask for approval on a phone, and facebook.com cookies work fine.
2. **Find the cookies:** open DevTools (F12), go to **Storage → Cookies → https://www.facebook.com**, and copy the values of these cookies into this JSON:
   ```json
   {"c_user": "...", "xs": "...", "datr": "...", "sb": "..."}
   ```
3. **Log in the bridge:** in Element, start a chat with the Messenger bridge bot (`@messengerbot:matrix.example.com`), send `login`, choose **facebook.com**, and paste the JSON text.
4. **Clean up:** **close the browser window without logging out** (logging out kills the bridge's session). Turn off the exit node, and delete the JSON.

When the Messenger group gets a new message, its portal room appears in Element.

---

## 8. Relay mode

The bridges only forward messages from logged-in users. **Relay mode** lets the bot's messages be sent through your bridge logins.

Add to `vars.yml` (see the example file for the full blocks):
```yaml
matrix_bridges_relay_enabled: true
matrix_bridge_mautrix_signal_bridge_relay_admin_only: false
matrix_bridge_mautrix_meta_messenger_bridge_relay_admin_only: false
```
Also add the `..._configuration_extension_yaml` blocks from the example. They do two things:
- **Make you a bridge admin** (`permissions`). Without this, `set-relay` fails in group rooms.
- **Pass the bot's text through unchanged** (`message_formats`). The bot already writes `Name (Network): text`, so without this you'd get "Relay: Name (Network): text".

Keep the `!unsafe` tag on those blocks, so Ansible doesn't try to process the `{{ }}` templates. Each bridge can only have **one** extension block, so merge everything into it.

```bash
just install-all
```

Check that the formats landed:
```bash
grep -A7 "message_formats" /matrix/mautrix-signal/config/config.yaml
```

---

## 9. maubot and the plugin

### 9.1 Enable maubot

```yaml
matrix_bot_maubot_enabled: true
matrix_bot_maubot_initial_password: 'LONG_RANDOM'
matrix_bot_maubot_admins:
  - yourname: 'WEB_UI_PASSWORD'
```
```bash
just install-all
```
The web UI is at `https://matrix.example.com/_matrix/maubot/`.

### 9.2 Access token for the bot

```bash
F=inventory/host_vars/matrix.example.com/vars.yml
PW=$(grep "^matrix_bot_maubot_initial_password" "$F" | cut -d"'" -f2)
curl -s -X POST https://matrix.example.com/_matrix/client/v3/login -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"bot.maubot\"},\"password\":\"$PW\",\"initial_device_display_name\":\"maubot\"}"
```
In the maubot UI, go to **Clients → +** and add `@bot.maubot:matrix.example.com` with the `access_token` and `device_id`. Set a display name (e.g. "Relay") and turn on **Autojoin**.

### 9.3 Prepare the portal rooms

In **both** portal rooms (the Signal group and the Messenger group), in Element:
1. **Invite** `@bot.maubot:matrix.example.com`.
2. **Send the set-relay command:** `!signal set-relay` in the Signal room, and `!fb set-relay` in the Messenger room.
   You can find the Messenger prefix with: `grep -rn "command_prefix" /matrix/ | grep -v signal`.
3. **Wait for the confirmation:** "Messages sent by users who haven't logged in will now be relayed…".

`set-relay` must be sent in the **portal** room, not in your chat with the bridge bot.

### 9.4 Upload the plugin and create the instance

1. **Build the plugin** with `./build.sh` in this repo, then upload the `.mbp` under **Plugins → +**.
2. **Create the instance:** go to **Instances → +**, pick your client, and choose type `no.andreash.relay`.
3. **Fill in the config:**
   - Find each **room ID** in Element under **Room settings → Advanced**, and copy it **exactly**. Newer room versions have **no `:server` suffix**, so don't add one.
   - Set `messenger_room` to the Messenger portal room and `signal_room` to the Signal group's portal room.
4. **Save,** and make sure the instance is **enabled**.

### 9.5 Alerts and `!health`

- **Alerts:** invite the bot to your chats with both bridge bots. Add those room IDs to `alert_rooms`, and set `alert_target` to a room bridged to your own Signal, e.g. your DM with the bot number.
- **`!health`:** fill in each bridge's provisioning **shared secret**:
  ```bash
  for b in mautrix-signal mautrix-meta-messenger; do echo "$b: $(grep -A4 '^provisioning:' /matrix/$b/config/config.yaml | awk '/shared_secret/{print $2}' | tr -d "'\"")"; done
  ```
  The bridge URLs use their **internal** address and port. Check them with `grep -n "address:" /matrix/<bridge>/config/config.yaml`. In this setup they were `http://matrix-mautrix-signal:8080` and `http://matrix-mautrix-meta-messenger:29319`.

Keep these secrets out of git. They only belong in the maubot instance config.

---

## 10. Testing checklist

Send from your **personal** apps, not from Element, to avoid confusion!

| Test | Expected |
|---|---|
| Message in Messenger | Appears in Signal as "Name (Messenger): …" |
| `!m hi` in Signal | Appears in Messenger as "Name (Signal): hi" |
| `hi` (no `!m`) in Signal | Nothing is forwarded |
| Photo in Messenger | Photo appears in Signal |
| Swipe-reply with `!m …` in Signal | Native reply in Messenger |
| Reply `!r`, or react 🔎, to a forwarded message in Signal | The bot lists the Messenger reactions |
| `!health` in Signal | Status for each bridge |
| `ping` in a bridge-bot chat | "⚠️ Bridge: …" arrives in your alert room |
| Reboot the VM | Everything comes back on its own |

To see what the plugin decides for each message, temporarily set `matrix_bot_maubot_logging_level: DEBUG`, run `just install-all`, and watch:
```bash
journalctl -fu matrix-bot-maubot
```
Remove the line again afterwards, because DEBUG logs message contents.

When testing with a new Messenger account, go easy: a few natural messages, no bursts.

---

## 11. Maintenance

- **Monthly updates:** the mautrix bridges release around the middle of each month.
  ```bash
  cd /path/to/matrix-docker-ansible-deploy && git pull && just roles && just install-all
  ```
  If the playbook stops with a "renamed variable" error, rename it as the message says.
- **Signal:** the bot phone must be online at least every 30 days, with Signal updated.
- **Messenger:** if the session expires, an alert arrives. Repeat [step 7.2–7.3](#72-log-in-from-the-servers-location-tailscale-exit-node).
- **Backups:** keep the scheduled Proxmox backup running.

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Playbook: *"Your configuration contains a variable, which now has a different name"* | The variable was renamed | Rename it exactly as the message says, then rerun |
| A variable only shows up in your own `vars.yml` when grepping | Wrong or outdated name | Look up the current name in `docs/` (e.g. the bridges use `matrix_bridge_mautrix_…`) |
| Bridge bot: *"Your message was not bridged: this bridge has not been configured to support encryption"* | Element created an encrypted room | Set `force_disable` (step 5). Leave the room and create a new one from Element desktop |
| *"That command can only be ran in portal rooms"* | You sent `set-relay` in the bridge-bot chat | Send it in the room that mirrors the actual chat |
| *"You're not allowed to use yourself as relay"* | Relay is admin-only by default | `..._bridge_relay_admin_only: false` for both bridges |
| *"You don't have permission to manage the relay in this room"* (groups) | You're not admin in the group room | Add yourself as bridge `admin` under `permissions` in the extension blocks |
| Bridge templates like `{{ .Message }}` break or vanish | Ansible tried to template them | Use `!unsafe \|` on the extension block |
| maubot UI: *"Invalid authorization token"* | The web UI session expired after a restart | Log out and back in to the maubot UI |
| `curl` login: *"Invalid username or password"* | Wrong password (or a placeholder left in) | Pull the password from `vars.yml` (step 9.2) |
| Plugin log: *"Skip: room not configured"* | Room ID mismatch, often a `:server` suffix added by hand | Copy the ID exactly from Element |
| Nothing is relayed and there are no plugin logs | Log level too quiet | Temporarily set DEBUG (step 10) |
| `docker logs`: *"configured logging driver does not support reading"* | The playbook doesn't use Docker's log driver | Use `journalctl -u matrix-<service>` |
| Names show up as "Anna (Signal) (Messenger)" | The bridge adds a network suffix | The plugin strips a trailing `(…)`. Make sure you run the current version |
| `qemu-guest-agent` fails to start | Agent not enabled in Proxmox | Enable it under **Options**, then do a full shutdown and start (not a reboot) |
| `systemd-ssh-generator … AF_VSOCK` in logs | No vsock device in the VM | Harmless, ignore it |
| Ansible: *"discovered Python interpreter"* warning | Interpreter not pinned | Harmless. Add `ansible_python_interpreter=/usr/bin/python3` to `hosts` |
| PC on a phone hotspot can't reach Tailscale devices | A phone's VPN doesn't cover hotspot clients | Install Tailscale on the PC |
| Exit node selected, but no internet on the PC | Forwarding blocked (sysctl or the firewall) | Check step 7.2. Docker's firewall rules can interfere |
| messenger.com asks to approve the login on a phone | A normal security check | Approve it, or use facebook.com cookies instead |
| New Messenger accounts get quarantined or deactivated | Meta's anti-spam on fresh accounts | Age the account first (step 7.1), or use an older account |
| `git commit` fails on the server | No git identity set | `git config --global user.name/user.email`. Use your GitHub noreply email so commits link to your profile |