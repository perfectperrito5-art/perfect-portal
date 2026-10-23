#!/usr/bin/env bash
# Ensure Wi‑Fi clients can reach the internet via Ethernet.
# Docker/UFW often set FORWARD policy to DROP — this inserts allow rules first.
# Usage: sudo ./gateway/ensure-forward.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/ap.env"

if [[ "${EUID}" -ne 0 ]]; then
  exec sudo -n "$SCRIPT_DIR/ensure-forward.sh" "$@"
fi

sysctl -w net.ipv4.ip_forward=1 >/dev/null
# Avoid reverse-path filter dropping NAT replies on some setups
sysctl -w net.ipv4.conf.all.rp_filter=2 >/dev/null 2>&1 || true
sysctl -w "net.ipv4.conf.${LAN_IF}.rp_filter=2" >/dev/null 2>&1 || true
sysctl -w "net.ipv4.conf.${WAN_IF}.rp_filter=2" >/dev/null 2>&1 || true

# iptables (Docker uses this) — insert at top so Docker DROP does not win
iptables -C FORWARD -i "$LAN_IF" -o "$WAN_IF" -j ACCEPT 2>/dev/null || \
  iptables -I FORWARD 1 -i "$LAN_IF" -o "$WAN_IF" -j ACCEPT

iptables -C FORWARD -i "$WAN_IF" -o "$LAN_IF" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
  iptables -I FORWARD 1 -i "$WAN_IF" -o "$LAN_IF" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

iptables -t nat -C POSTROUTING -s "$PORTAL_NET" -o "$WAN_IF" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -s "$PORTAL_NET" -o "$WAN_IF" -j MASQUERADE

echo "OK forward $LAN_IF → $WAN_IF (NAT $PORTAL_NET)"