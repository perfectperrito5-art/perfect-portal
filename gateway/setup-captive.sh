#!/usr/bin/env bash
# Captive portal networking sketch for Ubuntu + FiberHome HG6145F3 (or later ZTE).
# Review carefully before running. Needs root.
#
# Your current ONT (from router.jpeg):
#   Model: FiberHome HG6145F3 (GPON)  Admin: http://192.168.1.1  FW: RP3484
#
# Recommended topology (software captive portal):
#   Fiber ── FiberHome/ZTE ONT ── Ethernet ── Ubuntu PC (this gateway)
#                                              └── Wi‑Fi AP (hostapd) or second LAN to clients
#
# ISP ONTs (FiberHome / ZTE) usually cannot run a full captive portal.
# Put the Ubuntu PC as the gateway clients use for DNS/DHCP/NAT.

set -euo pipefail

WAN_IF="${WAN_IF:-eth0}"     # uplink toward FiberHome/ZTE (e.g. 192.168.1.0/24)
LAN_IF="${LAN_IF:-wlan0}"    # client-facing interface
PORTAL_IP="${PORTAL_IP:-10.10.0.1}"
PORTAL_PORT="${PORTAL_PORT:-5000}"
ONT_GATEWAY="${ONT_GATEWAY:-192.168.1.1}"

echo "This script prints the recommended commands; it does not auto-apply by default."
echo "WAN_IF=$WAN_IF LAN_IF=$LAN_IF PORTAL_IP=$PORTAL_IP ONT=$ONT_GATEWAY"
echo
cat <<EOF
# 0) On FiberHome HG6145F3 (http://$ONT_GATEWAY):
#    - Confirm PC gets WAN-side IP via DHCP from ONT (e.g. 192.168.1.10)
#    - Do NOT rely on ONT captive-portal features (usually absent)
#    - Optional: reserve a DHCP IP for the Ubuntu PC MAC

# 1) Install helpers
sudo apt update
sudo apt install -y hostapd dnsmasq nftables iptables

# 2) Static IP on client-facing NIC
# Edit /etc/netplan/*.yaml → $LAN_IF address $PORTAL_IP/24

# 3) dnsmasq: DHCP + DNS hijack unknown hosts to portal
# /etc/dnsmasq.d/perfect-portal.conf
#   interface=$LAN_IF
#   dhcp-range=10.10.0.50,10.10.0.200,12h
#   address=/#/$PORTAL_IP
#   dhcp-option=3,$PORTAL_IP
#   dhcp-option=6,$PORTAL_IP

# 4) nftables sketch (allow DNS/DHCP/portal; allow established for authorized MACs)
#   table inet filter {
#     set portal_allowed { type ether_addr; flags timeout; }
#     chain input { type filter hook input priority 0; policy drop;
#       iifname "$LAN_IF" udp dport {53,67} accept
#       iifname "$LAN_IF" tcp dport {53,$PORTAL_PORT} accept
#       ct state established,related accept
#     }
#     chain forward { type filter hook forward priority 0; policy drop;
#       ether saddr @portal_allowed accept
#       ct state established,related accept
#     }
#   }
#   table ip nat {
#     chain prerouting { type nat hook prerouting priority -100;
#       iifname "$LAN_IF" tcp dport {80,443} dnat to $PORTAL_IP:$PORTAL_PORT
#     }
#     chain postrouting { type nat hook postrouting priority 100;
#       oifname "$WAN_IF" masquerade
#     }
#   }

# 5) Enable IP forwarding
#   echo 1 | sudo tee /proc/sys/net/ipv4/ip_forward

# 6) Run Perfect Portal
#   cd /home/admin_perfect/perfect_portal
#   source .venv/bin/activate
#   gunicorn -b 0.0.0.0:$PORTAL_PORT 'run:app'

# 7) Set in .env:
#   GATEWAY_ENABLED=true
#   PORTAL_BASE_URL=http://$PORTAL_IP:$PORTAL_PORT
EOF