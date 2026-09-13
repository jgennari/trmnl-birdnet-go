#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root." >&2
  exit 1
fi

install -d -m 0755 /usr/local/libexec
install -m 0755 "$project_dir/birdnet_trmnl.py" /usr/local/libexec/birdnet-trmnl
install -m 0644 "$project_dir/systemd/birdnet-trmnl.service" /etc/systemd/system/birdnet-trmnl.service
install -m 0644 "$project_dir/systemd/birdnet-trmnl.timer" /etc/systemd/system/birdnet-trmnl.timer

if [ ! -e /etc/birdnet-trmnl.env ]; then
  install -m 0600 "$project_dir/systemd/birdnet-trmnl.env.example" /etc/birdnet-trmnl.env
  echo "Created /etc/birdnet-trmnl.env. Add the TRMNL webhook URL before enabling the timer."
fi

systemctl daemon-reload
echo "Test with: systemctl start birdnet-trmnl.service"
echo "Enable with: systemctl enable --now birdnet-trmnl.timer"
