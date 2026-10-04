#!/bin/sh
# Builds the maubot plugin archive (.mbp is just a zip)
set -e
cd "$(dirname "$0")"
VERSION=$(awk '/^version:/{print $2}' maubot.yaml)
rm -f "relay-v$VERSION.mbp"
zip -9 -q "relay-v$VERSION.mbp" maubot.yaml base-config.yaml relay.py
echo "Built relay-v$VERSION.mbp"
