#!/usr/bin/env bash
# Start Perfect Portal Wi‑Fi Access Point on this Ubuntu PC.
# Ethernet (WAN) stays connected to FiberHome for internet.
# Wi‑Fi (LAN) becomes the visible SSID for customers.
#
# Usage:
#   sudo ./gateway/start-ap.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/ap.env"
AP_ROLE="${AP_ROLE:-pc}"

RUNTIME_DIR="$SCRIPT_DIR/runtime"
HOSTAPD_CFG="$RUNTIME_DIR/hostapd.conf"
DNSMASQ_CFG="$RUNTIME_DIR/dnsmasq-portal.conf"
PID_DIR="$RUNTIME_DIR/pids"
LOG="$SCRIPT_DIR/gateway.log"

log() { echo "$(date -Is) $*" | tee -a "$LOG"; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run with sudo:  sudo $0" >&2
    exit 1
  fi
}

need_root
mkdir -p "$RUNTIME_DIR" "$PID_DIR"

# Refuse to run on the broken 7.0 kernel if someone boots it by mistake
RUNNING_KERNEL="$(uname -r)"
if [[ "$RUNNING_KERNEL" == 7.0.0-28-generic ]]; then
  echo "You are on broken kernel $RUNNING_KERNEL." >&2
  echo "Reboot into 6.17.0-35-generic, then run again." >&2
  exit 1
fi
log "Kernel: $RUNNING_KERNEL (AP uses this running kernel; no kernel install needed)"

ensure_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1
}

MISSING=()
ensure_cmd hostapd || MISSING+=(hostapd)
ensure_cmd dnsmasq || MISSING+=(dnsmasq)
ensure_cmd iw || MISSING+=(iw)
ensure_cmd nft || MISSING+=(nftables)

if ((${#MISSING[@]} > 0)); then
  echo "Missing tools: ${MISSING[*]}"
  echo "Trying to install ONLY those packages (will not upgrade kernels)..."
  # Avoid apt -f / upgrades that re-enter broken linux-image-7.0.0-28 configure
  if ! DEBIAN_FRONTEND=noninteractive apt-get install -y --no-upgrade \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      "${MISSING[@]}" iptables; then
    echo
    echo "apt is stuck on broken kernel 7.0.0-28 packages." >&2
    echo "Fix once with:  sudo $SCRIPT_DIR/fix-kernel-apt.sh" >&2
    echo "Then re-run:     sudo $0" >&2
    exit 1
  fi
else
  log "AP tools already installed (hostapd dnsmasq iw nft) — skipping apt"
fi

if ! iw list 2>/dev/null | grep -A20 'Supported interface modes' | grep -q '\* AP'; then
  log "WARNING: driver may not advertise AP mode. Continuing anyway (Intel 8265 usually works on 2.4 GHz)."
fi

if [[ ! -d "/sys/class/net/$LAN_IF" ]]; then
  echo "Wi‑Fi interface $LAN_IF not found." >&2
  exit 1
fi
if [[ ! -d "/sys/class/net/$WAN_IF" ]]; then
  echo "Ethernet interface $WAN_IF not found." >&2
  exit 1
fi

# Validate all address/interface assumptions BEFORE stopping a running AP or
# changing interface state. The DHCP config below is explicitly /24.
if [[ "$LAN_IF" == "$WAN_IF" ]]; then
  echo "LAN_IF and WAN_IF must be different interfaces (both are $LAN_IF)." >&2
  exit 1
fi
if [[ "$PORTAL_NET" != */24 ]]; then
  echo "PORTAL_NET must be a /24 network because DHCP currently uses a /24 mask." >&2
  exit 1
fi
if [[ -n "${WIFI_PASS}" && ${#WIFI_PASS} -lt 8 ]]; then
  echo "WIFI_PASS must be at least 8 characters (or leave empty for open AP)." >&2
  exit 1
fi
if ! python3 - "$PORTAL_IP" "$PORTAL_NET" "$DHCP_START" "$DHCP_END" <<'PY'
import ipaddress
import sys

portal_ip = ipaddress.ip_address(sys.argv[1])
net = ipaddress.ip_network(sys.argv[2], strict=False)
start = ipaddress.ip_address(sys.argv[3])
end = ipaddress.ip_address(sys.argv[4])
if portal_ip not in net or start not in net or end not in net:
    raise SystemExit("PORTAL_IP and DHCP range must all belong to PORTAL_NET")
if int(start) > int(end):
    raise SystemExit("DHCP_START must be less than or equal to DHCP_END")
if portal_ip in (start, end) or int(start) <= int(portal_ip) <= int(end):
    raise SystemExit("PORTAL_IP must be outside the DHCP pool")
if start == net.network_address or end == net.broadcast_address:
    raise SystemExit("DHCP pool cannot include network/broadcast addresses")
PY
then
  echo "Invalid LAN/DHCP address configuration; AP was not stopped." >&2
  exit 1
fi

log "Preflight OK: AP_ROLE=$AP_ROLE LAN=$LAN_IF WAN=$WAN_IF NET=$PORTAL_NET PORTAL=$PORTAL_IP DHCP=$DHCP_START-$DHCP_END"
log "Stopping any previous Perfect Portal AP..."
"$SCRIPT_DIR/stop-ap.sh" --quiet || true

# Free LAN interface from NetworkManager when this PC owns it
if command -v nmcli >/dev/null 2>&1; then
  nmcli device disconnect "$LAN_IF" >/dev/null 2>&1 || true
  nmcli device set "$LAN_IF" managed no >/dev/null 2>&1 || true
fi
if [[ -d "/sys/class/net/$LAN_IF/wireless" ]]; then
  rfkill unblock wifi || true
fi

# Avoid systemd hostapd/dnsmasq fighting our instance
systemctl stop hostapd 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true
systemctl disable hostapd 2>/dev/null || true
# keep dnsmasq package installed but not owning our iface
systemctl disable dnsmasq 2>/dev/null || true

ip link set "$LAN_IF" down || true
ip addr flush dev "$LAN_IF" || true
ip addr add "${PORTAL_IP}/24" dev "$LAN_IF"
ip link set "$LAN_IF" up

# hostapd config (only used when this PC is the Wi-Fi radio)
cat > "$HOSTAPD_CFG" <<EOF
interface=$LAN_IF
driver=nl80211
ssid=$SSID
country_code=$COUNTRY
hw_mode=g
channel=$CHANNEL
ieee80211n=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF

if [[ -n "${WIFI_PASS}" ]]; then
  if [[ ${#WIFI_PASS} -lt 8 ]]; then
    echo "WIFI_PASS must be at least 8 characters (or leave empty for open AP)." >&2
    exit 1
  fi
  cat >> "$HOSTAPD_CFG" <<EOF
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$WIFI_PASS
EOF
  log "AP security: WPA2-PSK"
else
  log "AP security: OPEN (no password) — good for captive portal demos"
fi

# dnsmasq config — real DNS for authorized browsing; only captive-detect
# hostnames point at the portal so phones show "Sign in to network".
#address=/connectivitycheck.gstatic.com/${PORTAL_IP} put this and these two down, btn 191 and 193
#address=/connectivitycheck.android.com/${PORTAL_IP}
#address=/clients2.google.com/${PORTAL_IP}
cat > "$DNSMASQ_CFG" <<EOF
interface=$LAN_IF
bind-interfaces
domain-needed
bogus-priv
no-resolv
no-hosts
server=1.1.1.1
server=8.8.8.8
dhcp-range=${DHCP_START},${DHCP_END},255.255.255.0,24h
dhcp-option=3,${PORTAL_IP}
dhcp-option=6,${PORTAL_IP}
address=/connectivitycheck.gstatic.com/${PORTAL_IP}
address=/connectivitycheck.android.com/${PORTAL_IP}
address=/clients2.google.com/${PORTAL_IP}
address=/captive.apple.com/${PORTAL_IP}
address=/www.apple.com/${PORTAL_IP}
address=/www.msftconnecttest.com/${PORTAL_IP}
address=/dns.msftncsi.com/${PORTAL_IP}
address=/detectportal.firefox.com/${PORTAL_IP}
address=/network-test.debian.org/${PORTAL_IP}
address=/ipv6.msftconnecttest.com/${PORTAL_IP}
dhcp-leasefile=$RUNTIME_DIR/dnsmasq.leases
log-facility=$RUNTIME_DIR/dnsmasq.log
EOF

# Validate generated service configs before attempting to launch them.
if ! dnsmasq --test --conf-file="$DNSMASQ_CFG" >> "$RUNTIME_DIR/dnsmasq.log" 2>&1; then
  log "ERROR: dnsmasq config validation failed. Review $RUNTIME_DIR/dnsmasq.log"
  exit 1
fi
# IP forwarding + NAT out via Ethernet
sysctl -w net.ipv4.ip_forward=1 >/dev/null

# Allow portal user to authorize MACs without a password prompt
SUDOERS_FILE="/etc/sudoers.d/perfect-portal"
cat > "$SUDOERS_FILE" <<EOF
# Perfect Portal — voucher/payment unlocks internet via nft + NAT
${PORTAL_USER} ALL=(root) NOPASSWD: ${SCRIPT_DIR}/authorize.sh
${PORTAL_USER} ALL=(root) NOPASSWD: ${SCRIPT_DIR}/ensure-forward.sh
EOF
chmod 440 "$SUDOERS_FILE"
if command -v visudo >/dev/null 2>&1; then
  if ! visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
    echo "ERROR: invalid sudoers file written to $SUDOERS_FILE" >&2
    rm -f "$SUDOERS_FILE"
    exit 1
  fi
fi
log "Installed sudoers: $SUDOERS_FILE"

# Allow AP clients through Docker/UFW FORWARD + NAT out Ethernet
"$SCRIPT_DIR/ensure-forward.sh"
log "Installed iptables forward/NAT $LAN_IF → $WAN_IF"

# Captive firewall table
# Unauthorized forwards are REJECTED (not dropped) so phones fail fast
# instead of waiting 1–2 minutes on TCP timeouts.
nft delete table inet portal 2>/dev/null || true
nft -f - <<EOF
table inet portal {
  set portal_allowed {
    type ether_addr
    flags timeout
  }

  chain input {
    type filter hook input priority 0; policy accept;
    iifname "$LAN_IF" udp dport { 53, 67 } accept
    iifname "$LAN_IF" tcp dport { 53, $PORTAL_PORT } accept
    iifname "$LAN_IF" tcp dport { 80, 443 } accept
  }

  chain forward {
    type filter hook forward priority 0; policy drop;
    ct state established,related accept
    ether saddr @portal_allowed accept
    # Fail fast for unpaid clients (avoids long browser hangs)
    iifname "$LAN_IF" reject with icmpx type admin-prohibited
  }

  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    iifname "$LAN_IF" ether saddr != @portal_allowed tcp dport 80 dnat ip to ${PORTAL_IP}:${PORTAL_PORT}
  }

  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    oifname "$WAN_IF" masquerade
  }
}
EOF

if [[ "${CAPTIVE_BLOCK}" != "yes" ]]; then
  nft flush chain inet portal forward
  nft add rule inet portal forward ct state established,related accept
  nft add rule inet portal forward iifname "$LAN_IF" accept
  log "CAPTIVE_BLOCK=no — all Wi‑Fi clients get internet immediately"
else
  log "CAPTIVE_BLOCK=yes — internet only after voucher/payment (MAC allow-list)"
fi

# Start hostapd only when this PC is the access point
if [[ "$AP_ROLE" == "device" ]]; then
  log "AP_ROLE=device — skipping hostapd. Plug a hardware AP into $LAN_IF (AP/bridge mode, DHCP off)."
else
  hostapd -B -P "$PID_DIR/hostapd.pid" "$HOSTAPD_CFG"
  sleep 1
  if ! kill -0 "$(cat "$PID_DIR/hostapd.pid")" 2>/dev/null; then
    log "ERROR: hostapd failed to start. Check adapter AP support / channel."
    exit 1
  fi
fi

# Start dnsmasq
dnsmasq --conf-file="$DNSMASQ_CFG" --pid-file="$PID_DIR/dnsmasq.pid"
sleep 0.5
if [[ -f "$PID_DIR/dnsmasq.pid" ]] && kill -0 "$(cat "$PID_DIR/dnsmasq.pid")" 2>/dev/null; then
  log "dnsmasq started successfully (DHCP/DNS on $LAN_IF)"
else
  log "ERROR: dnsmasq process did not remain running. Review $RUNTIME_DIR/dnsmasq.log"
  exit 1
fi

# Record useful state for diagnosing wrong-subnet/DHCP and captive-portal issues.
{
  echo "===== $(date -Is) AP startup diagnostics ====="
  echo "AP_ROLE=$AP_ROLE SSID=$SSID LAN_IF=$LAN_IF WAN_IF=$WAN_IF"
  echo "PORTAL_NET=$PORTAL_NET PORTAL_IP=$PORTAL_IP DHCP=$DHCP_START-$DHCP_END"
  ip -br addr
  echo "--- routes ---"
  ip route
  echo "--- listeners ---"
  ss -ulpn 2>/dev/null | grep -E ':(53|67)\\b' || true
  echo "--- DHCP leases ---"
  cat "$RUNTIME_DIR/dnsmasq.leases" 2>/dev/null || true
} >> "$LOG" 2>&1

# Portal process
if [[ "${START_PORTAL}" == "yes" ]]; then
  # Update .env portal URL / gateway for this session
  ENV_FILE="$PORTAL_DIR/.env"
  if [[ -f "$ENV_FILE" ]]; then
    sed -i "s|^PORTAL_BASE_URL=.*|PORTAL_BASE_URL=http://${PORTAL_IP}:${PORTAL_PORT}|" "$ENV_FILE"
    sed -i "s|^GATEWAY_ENABLED=.*|GATEWAY_ENABLED=true|" "$ENV_FILE"
    sed -i "s|^GATEWAY_SCRIPT=.*|GATEWAY_SCRIPT=${SCRIPT_DIR}/authorize.sh|" "$ENV_FILE"
  fi

  # Stop previous portal if any
  if [[ -f "$PID_DIR/portal.pid" ]] && kill -0 "$(cat "$PID_DIR/portal.pid")" 2>/dev/null; then
    kill "$(cat "$PID_DIR/portal.pid")" 2>/dev/null || true
  fi

  sudo -u "$PORTAL_USER" bash -lc "
    cd '$PORTAL_DIR'
    source .venv/bin/activate
    nohup gunicorn -b 0.0.0.0:${PORTAL_PORT} -w 2 'run:app' \
      >> '$RUNTIME_DIR/portal.log' 2>&1 &
    echo \$! > '$PID_DIR/portal.pid'
  "
  # gunicorn may fork; store the nohup shell child — prefer pgrep
  sleep 1
  pgrep -f "gunicorn -b 0.0.0.0:${PORTAL_PORT}" | head -1 > "$PID_DIR/portal.pid" || true
  log "Portal listening on http://${PORTAL_IP}:${PORTAL_PORT}"

  # Re-apply firewall allow for sessions already paid/vouchered in the DB
  sleep 1
  SYNCED="$(
    sudo -u "$PORTAL_USER" bash -lc "
      cd '$PORTAL_DIR'
      source .venv/bin/activate
      python - <<'PY'
from app import create_app
from app.services.access import sync_active_sessions_to_gateway
app = create_app()
with app.app_context():
    print(sync_active_sessions_to_gateway())
PY
    " 2>/dev/null || echo 0
  )"
  log "Synced $SYNCED active session(s) into firewall allow-list"
fi

# Writable logs for authorize.sh when called via sudo/portal
touch "$RUNTIME_DIR/authorize.log" "$SCRIPT_DIR/gateway.log" 2>/dev/null || true
chown "$PORTAL_USER:$PORTAL_USER" "$RUNTIME_DIR/authorize.log" 2>/dev/null || true
chmod 666 "$RUNTIME_DIR/authorize.log" "$SCRIPT_DIR/gateway.log" 2>/dev/null || true

cat > "$RUNTIME_DIR/ap-running.env" <<EOF
SSID=$SSID
PORTAL_IP=$PORTAL_IP
PORTAL_PORT=$PORTAL_PORT
LAN_IF=$LAN_IF
WAN_IF=$WAN_IF
STARTED_AT=$(date -Is)
EOF

echo
echo "=============================================="
echo "  Perfect Portal gateway is ON"
echo "  Role:     $AP_ROLE"
if [[ "$AP_ROLE" == "pc" ]]; then
  echo "  SSID:     $SSID"
  if [[ -n "$WIFI_PASS" ]]; then
    echo "  Password: $WIFI_PASS"
  else
    echo "  Password: (open network)"
  fi
else
  echo "  Radio:    hardware AP on $LAN_IF (this PC is not broadcasting Wi-Fi)"
fi
echo "  Portal:   http://${PORTAL_IP}:${PORTAL_PORT}"
echo "  Uplink:   $WAN_IF"
echo "  Clients:  $LAN_IF  ($PORTAL_NET)"
echo "=============================================="
if [[ "$AP_ROLE" == "pc" ]]; then
  echo "NOTE: Ubuntu Wi‑Fi Settings may say 'no adapter' while the AP runs."
  echo "      That is normal — $LAN_IF is busy as Perfect-WiFi (not a client)."
  echo "      PC internet stays on Ethernet. Restore Wi‑Fi UI with stop-ap.sh."
else
  echo "NOTE: Put the hardware AP in access-point / bridge mode (disable its DHCP)."
  echo "      Customers' gateway must be $PORTAL_IP (this PC), not the AP's own LAN IP."
fi
echo
echo "Phone tip: disable randomized/private MAC for Perfect-WiFi."
echo "Stop later with:  sudo $SCRIPT_DIR/stop-ap.sh"
echo
log "AP started SSID=$SSID portal=http://${PORTAL_IP}:${PORTAL_PORT}"