#!/bin/sh
# Remove the island controller and the Art-Net network module from this Pi.
# Keeps /etc/ledpoles (config) and the network as it is now: to get eth0
# back on DHCP for everything, apply the "shared" mode on the pi-agent page
# before uninstalling (or delete the NetworkManager profile "artnet" and set
# autoconnect on the eth0 profile).
set -eu
systemctl disable --now ledpoles-boot.service pimod-artnet.service pimod-artnet-net.service ledpoles-dhcp.service 2>/dev/null || true
rm -f /etc/systemd/system/pimod-artnet.service /etc/systemd/system/pimod-artnet-net.service \
  /etc/systemd/system/ledpoles-dhcp.service /etc/systemd/system/ledpoles-boot.service \
  /etc/pi-agent/modules.d/artnet.toml /etc/pi-agent/modules.d/artnet-net.toml
systemctl daemon-reload
rm -rf /opt/ledpoles
if nmcli -t -f NAME con show | grep -qx artnet; then
  echo "note: the NetworkManager profile 'artnet' is still active; the DHCP server is stopped"
fi
echo "removed. kept: /etc/ledpoles, /var/lib/ledpoles, ledpoles user, network profiles"
