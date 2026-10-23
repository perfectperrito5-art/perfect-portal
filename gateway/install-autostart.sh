#!/usr/bin/env bash
# Install / enable Perfect Portal AP auto-start on boot (systemd).
#
# Usage:
#   sudo ./gateway/install-autostart.sh
#   sudo ./gateway/install-autostart.sh --disable   # turn auto-start off

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_SRC="$SCRIPT_DIR/perfect-portal-ap.service"
SERVICE_DST="/etc/systemd/system/perfect-portal-ap.service"
UNIT_NAME="perfect-portal-ap.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo:  sudo $0" >&2
  exit 1
fi

chmod +x \
  "$SCRIPT_DIR/start-ap.sh" \
  "$SCRIPT_DIR/stop-ap.sh" \
  "$SCRIPT_DIR/authorize.sh" \
  "$SCRIPT_DIR/ensure-forward.sh" \
  "$SCRIPT_DIR/wait-for-uplink.sh" \
  "$SCRIPT_DIR/install-autostart.sh"

if [[ "${1:-}" == "--disable" ]]; then
  systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
  echo "Auto-start disabled. AP was stopped if it was running via systemd."
  echo "Start manually anytime:  sudo $SCRIPT_DIR/start-ap.sh"
  exit 0
fi

if [[ ! -f "$SERVICE_SRC" ]]; then
  echo "Missing $SERVICE_SRC" >&2
  exit 1
fi

# Ensure NetworkManager waits for online (harmless if already set)
systemctl enable NetworkManager-wait-online.service 2>/dev/null || true

install -m 644 "$SERVICE_SRC" "$SERVICE_DST"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"

echo
echo "Installed: $SERVICE_DST"
echo "Enabled on boot: $UNIT_NAME"
echo
echo "Commands:"
echo "  sudo systemctl start $UNIT_NAME     # start now"
echo "  sudo systemctl stop $UNIT_NAME      # stop AP"
echo "  sudo systemctl status $UNIT_NAME    # check"
echo "  sudo journalctl -u $UNIT_NAME -e    # logs"
echo "  sudo $0 --disable                   # remove auto-start"
echo
echo "Start it now? Run:  sudo systemctl start $UNIT_NAME"
echo "(Skip if AP is already running from start-ap.sh — stop first, then start the service.)"