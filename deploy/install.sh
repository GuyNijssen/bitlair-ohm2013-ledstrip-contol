#!/bin/sh
# Install the island controller and the Art-Net network module as pi-agent
# modules. Run as root on the Pi from a checkout of this repo:
#
#   sudo deploy/install.sh
#
# The controller (pimod-artnet) starts at boot only if it was running when
# the Pi went down (ledpoles-boot.service); start/stop it from the pi-agent
# page or the ett dashboard. The network module (pimod-artnet-net) is
# started and enabled, but changes nothing until a mode is applied. Network
# config: /etc/ledpoles/network.json (written by the module).
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/ledpoles
for arg in "$@"; do
  echo "unknown option: $arg (--enable is gone: the controller starts at boot if it was running at shutdown)" >&2
  exit 1
done

id ledpoles >/dev/null 2>&1 || useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin ledpoles

# Code: replace in full, no stray files left behind.
rm -rf "$DEST.new"
mkdir -p "$DEST.new"
(cd "$SRC" && tar --exclude=.git -cf - .) | tar -xf - -C "$DEST.new"
rm -rf "$DEST"
mv "$DEST.new" "$DEST"
chown -R root:root "$DEST"

# Config: never overwrite an edited one.
install -d -m 755 /etc/ledpoles
[ -f /etc/ledpoles/island.toml ] || install -m 644 "$SRC/island/island.example.toml" /etc/ledpoles/island.toml

# The DHCP server only needs the dnsmasq binary (dnsmasq-base); the full
# dnsmasq package would also start a DNS server on every interface.
command -v dnsmasq >/dev/null || apt-get install -y dnsmasq-base

install -m 644 "$SRC/deploy/pimod-artnet.service" "$SRC/deploy/pimod-artnet-net.service" \
  "$SRC/deploy/ledpoles-dhcp.service" "$SRC/deploy/ledpoles-boot.service" /etc/systemd/system/
if [ -d /etc/pi-agent/modules.d ]; then
  install -m 644 "$SRC/deploy/pi-agent-module.toml" /etc/pi-agent/modules.d/artnet.toml
  install -m 644 "$SRC/deploy/pi-agent-module-net.toml" /etc/pi-agent/modules.d/artnet-net.toml
else
  echo "note: pi-agent not installed, module manifests skipped"
fi
systemctl daemon-reload
# Boot start goes through ledpoles-boot (only if it was running at shutdown).
systemctl disable pimod-artnet.service 2>/dev/null || true
systemctl enable ledpoles-boot.service
if systemctl is-active --quiet pimod-artnet.service; then
  systemctl restart pimod-artnet.service
fi
systemctl enable pimod-artnet-net.service
systemctl restart pimod-artnet-net.service

echo "installed. config: /etc/ledpoles/island.toml  services: pimod-artnet, pimod-artnet-net"
