#!/usr/bin/env bash
# Authorize or revoke a client MAC on the Perfect Portal AP.
# Usage: authorize.sh <authorize|revoke> <MAC> [IP] [seconds]
#
# 4th argument is remaining time in SECONDS (not minutes), so a 24-hour
# package is 86400s of wall-clock time. Legacy values ending in "m" are
# still accepted as minutes.
#
# Safe to call from the Flask app (non-root): escalates via sudoers installed
# by start-ap.sh.

set -u

ACTION="${1:-}"
MAC="${2:-}"
IP="${3:-}"
DURATION="${4:-0}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SET_NAME="portal_allowed"
TABLE="inet"
FAMILY_TABLE="portal"
LOG_FILE="$SCRIPT_DIR/runtime/authorize.log"

mkdir -p "$SCRIPT_DIR/runtime" 2>/dev/null || true

log() {
  local line
  line="$(date -Is) $*"
  echo "$line"
  echo "$line" >> "$LOG_FILE" 2>/dev/null || true
}

if [[ -z "$ACTION" || -z "$MAC" ]]; then
  echo "Usage: $0 <authorize|revoke> <MAC> [IP] [seconds]" >&2
  exit 1
fi

# Re-exec as root when needed (NOPASSWD sudoers from start-ap.sh)
if [[ "${EUID}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo -n "$SCRIPT_DIR/authorize.sh" "$ACTION" "$MAC" "$IP" "$DURATION"
  fi
  log "ERROR: must run as root (sudoers missing?). MAC=$MAC"
  exit 1
fi

MAC="$(echo "$MAC" | tr '[:lower:]' '[:upper:]' | tr '-' ':')"

if [[ "$MAC" == "UNKNOWN" || ! "$MAC" =~ ^([0-9A-F]{2}:){5}[0-9A-F]{2}$ ]]; then
  log "ERROR: invalid MAC=$MAC"
  exit 1
fi

ensure_set() {
  if ! nft list table "$TABLE" "$FAMILY_TABLE" >/dev/null 2>&1; then
    log "ERROR: nft table inet portal not loaded — is the AP started?"
    return 1
  fi
  if ! nft list set "$TABLE" "$FAMILY_TABLE" "$SET_NAME" >/dev/null 2>&1; then
    nft add set "$TABLE" "$FAMILY_TABLE" "$SET_NAME" '{ type ether_addr; flags timeout; }'
  fi
}

# Keep Docker/UFW from blocking AP → Ethernet internet (inline; do not depend on sudoers)
if [[ -f "$SCRIPT_DIR/ap.env" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/ap.env"
  sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true
  iptables -C FORWARD -i "${LAN_IF}" -o "${WAN_IF}" -j ACCEPT 2>/dev/null || \
    iptables -I FORWARD 1 -i "${LAN_IF}" -o "${WAN_IF}" -j ACCEPT 2>/dev/null || \
    log "WARN: could not insert FORWARD allow ${LAN_IF}->${WAN_IF}"
  iptables -C FORWARD -i "${WAN_IF}" -o "${LAN_IF}" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
    iptables -I FORWARD 1 -i "${WAN_IF}" -o "${LAN_IF}" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  iptables -t nat -C POSTROUTING -s "${PORTAL_NET}" -o "${WAN_IF}" -j MASQUERADE 2>/dev/null || \
    iptables -t nat -A POSTROUTING -s "${PORTAL_NET}" -o "${WAN_IF}" -j MASQUERADE 2>/dev/null || \
    log "WARN: could not add MASQUERADE on ${WAN_IF}"
fi

case "$ACTION" in
  authorize)
    log "AUTHORIZE mac=$MAC ip=$IP duration=$DURATION"
    ensure_set || exit 1
    # Replace any existing element first
    nft delete element "$TABLE" "$FAMILY_TABLE" "$SET_NAME" "{ $MAC }" 2>/dev/null || true
    timeout_spec=""
    if [[ "$DURATION" =~ ^[0-9]+m$ ]]; then
      timeout_spec="${DURATION}"
    elif [[ "$DURATION" =~ ^[0-9]+s$ ]]; then
      timeout_spec="${DURATION}"
    elif [[ "$DURATION" =~ ^[0-9]+$ ]] && [[ "$DURATION" -gt 0 ]]; then
      timeout_spec="${DURATION}s"
    fi
    if [[ -n "$timeout_spec" ]]; then
      nft add element "$TABLE" "$FAMILY_TABLE" "$SET_NAME" "{ $MAC timeout ${timeout_spec} }"
    else
      nft add element "$TABLE" "$FAMILY_TABLE" "$SET_NAME" "{ $MAC }"
    fi
    log "OK authorized $MAC"
    ;;
  revoke)
    log "REVOKE mac=$MAC ip=$IP"
    nft delete element "$TABLE" "$FAMILY_TABLE" "$SET_NAME" "{ $MAC }" 2>/dev/null || true
    log "OK revoked $MAC"
    ;;
  *)
    echo "Unknown action: $ACTION" >&2
    exit 1
    ;;
esac

exit 0