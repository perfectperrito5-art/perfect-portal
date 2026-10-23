#!/usr/bin/env bash
# Stop Perfect Portal Access Point and restore Wi‑Fi to NetworkManager.
# Usage: sudo ./gateway/stop-ap.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/ap.env"

RUNTIME_DIR="$SCRIPT_DIR/runtime"
PID_DIR="$RUNTIME_DIR/pids"
LOG="$SCRIPT_DIR/gateway.log"
QUIET="${1:-}"

log() {
  if [[ "$QUIET" != "--quiet" ]]; then
    echo "$(date -Is) $*" | tee -a "$LOG"
  else
    echo "$(date -Is) $*" >> "$LOG"
  fi
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo:  sudo $0" >&2
  exit 1
fi

log "Stopping Perfect Portal AP..."

# Portal
if [[ -f "$PID_DIR/portal.pid" ]]; then
  kill "$(cat "$PID_DIR/portal.pid")" 2>/dev/null || true
  rm -f "$PID_DIR/portal.pid"
fi
pkill -f "gunicorn -b 0.0.0.0:${PORTAL_PORT}" 2>/dev/null || true

# hostapd / dnsmasq
if [[ -f "$PID_DIR/hostapd.pid" ]]; then
  kill "$(cat "$PID_DIR/hostapd.pid")" 2>/dev/null || true
  rm -f "$PID_DIR/hostapd.pid"
fi
if [[ -f "$PID_DIR/dnsmasq.pid" ]]; then
  kill "$(cat "$PID_DIR/dnsmasq.pid")" 2>/dev/null || true
  rm -f "$PID_DIR/dnsmasq.pid"
fi
pkill -x hostapd 2>/dev/null || true
pkill -f "dnsmasq --conf-file=$RUNTIME_DIR/dnsmasq-portal.conf" 2>/dev/null || true

# Firewall
nft delete table inet portal 2>/dev/null || true
iptables -D FORWARD -i "$LAN_IF" -o "$WAN_IF" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i "$WAN_IF" -o "$LAN_IF" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
iptables -t nat -D POSTROUTING -s "$PORTAL_NET" -o "$WAN_IF" -j MASQUERADE 2>/dev/null || true

# Interface cleanup
if [[ -d "/sys/class/net/$LAN_IF" ]]; then
  ip addr flush dev "$LAN_IF" 2>/dev/null || true
  ip link set "$LAN_IF" down 2>/dev/null || true
fi

# Give wireless LAN back to NetworkManager (PC-as-AP mode only)
if [[ -d "/sys/class/net/$LAN_IF/wireless" ]]; then
  if [[ -d "/sys/class/net/$LAN_IF" ]]; then
    ip link set "$LAN_IF" up 2>/dev/null || true
  fi
  if command -v nmcli >/dev/null 2>&1; then
    nmcli device set "$LAN_IF" managed yes >/dev/null 2>&1 || true
    nmcli device reconnect "$LAN_IF" >/dev/null 2>&1 || true
    nmcli radio wifi on >/dev/null 2>&1 || true
    nmcli device wifi rescan >/dev/null 2>&1 || true
  fi
fi

rm -f "$RUNTIME_DIR/ap-running.env"
log "AP stopped. Wi‑Fi returned to NetworkManager (Settings should list the adapter again)."

if [[ "$QUIET" != "--quiet" ]]; then
  echo "Perfect Portal AP stopped."
  echo "Ubuntu Wi‑Fi Settings should show your adapter again shortly."
  echo "Reconnect to your home Wi‑Fi (e.g. Cleopa_5G) if you want."
fi