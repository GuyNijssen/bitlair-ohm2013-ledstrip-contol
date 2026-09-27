#!/bin/sh
# Remove the island controller module from this Pi. Keeps /etc/ledpoles
# (config) and the network setup; remove those by hand if wanted.
set -eu
systemctl disable --now pimod-artnet.service 2>/dev/null || true
rm -f /etc/systemd/system/pimod-artnet.service /etc/pi-agent/modules.d/artnet.toml
systemctl daemon-reload
rm -rf /opt/ledpoles
echo "removed. kept: /etc/ledpoles, ledpoles user, eth0/dnsmasq setup"
