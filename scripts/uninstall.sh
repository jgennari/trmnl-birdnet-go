#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this uninstaller as root." >&2
  exit 1
fi

systemctl disable --now birdnet-trmnl.timer 2>/dev/null || true
rm -f /etc/systemd/system/birdnet-trmnl.service
rm -f /etc/systemd/system/birdnet-trmnl.timer
rm -f /usr/local/libexec/birdnet-trmnl
systemctl daemon-reload
echo "Preserved /etc/birdnet-trmnl.env and cached image metadata."
