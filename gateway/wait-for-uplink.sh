#!/usr/bin/env bash
# Wait until the Ethernet uplink has an IPv4 address (DHCP after boot).
# Used by systemd before start-ap.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/ap.env"

TRIES="${1:-60}"
SLEEP_SECS="${2:-2}"

for ((i = 1; i <= TRIES; i++)); do
  if [[ -d "/sys/class/net/${WAN_IF}" ]]; then
    if ip -4 -o addr show dev "$WAN_IF" | grep -q ' inet '; then
      echo "Uplink ready: $WAN_IF"
      exit 0
    fi
  fi
  echo "Waiting for uplink $WAN_IF ($i/$TRIES)..."
  sleep "$SLEEP_SECS"
done

echo "WARNING: $WAN_IF has no IPv4 yet — starting AP anyway." >&2
exit 0