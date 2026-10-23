# Perfect Portal Access Point (no extra AP hardware)

Your PC uses:

- **Ethernet `enp0s31f6`** → FiberHome (internet uplink)
- **Wi‑Fi `wlp1s0`** (Intel 8265) → broadcasts the hotspot SSID

## Auto-start after reboot (recommended)

```bash
cd /home/admin_perfect/perfect_portal
sudo ./gateway/install-autostart.sh
sudo systemctl start perfect-portal-ap.service
```

After that, a reboot brings Perfect-WiFi + portal back automatically  
(waits for Ethernet DHCP, then runs `start-ap.sh`).

| Command | Meaning |
|---------|---------|
| `sudo systemctl status perfect-portal-ap` | Is it running? |
| `sudo systemctl stop perfect-portal-ap` | Stop AP |
| `sudo systemctl start perfect-portal-ap` | Start AP |
| `sudo journalctl -u perfect-portal-ap -e` | Boot/runtime logs |
| `sudo ./gateway/install-autostart.sh --disable` | Turn off auto-start |

## One-time / manual start

If apt complains about `linux-*-7.0.0-28-generic`, fix that first (keeps **6.17.0-35-generic**):

```bash
cd /home/admin_perfect/perfect_portal
sudo ./gateway/fix-kernel-apt.sh
sudo ./gateway/start-ap.sh
```

Normal manual start (when apt is healthy):

```bash
cd /home/admin_perfect/perfect_portal
sudo ./gateway/start-ap.sh
```

That will:

1. Install `hostapd`, `dnsmasq`, `iw` if missing (asks sudo)
2. Disconnect Wi‑Fi from `Cleopa_5G` (Ethernet keeps internet)
3. Broadcast SSID **Perfect-WiFi** (open by default)
4. Give phones IPs in `10.10.0.0/24`
5. Redirect them to Perfect Portal
6. Block internet until a **voucher** is redeemed

## Phone demo (voucher only)

1. On PC admin: http://10.10.0.1:5000/auth/login → Vouchers → generate codes  
   (or http://127.0.0.1:5000/auth/login while portal is running)
2. On phone: connect to **Perfect-WiFi**
3. Captive page should open (or visit http://10.10.0.1:5000)
4. Redeem voucher → internet unlocks for that phone

After redeem, the phone should browse normally. Reconnecting to Perfect-WiFi
should **not** force a new voucher while the package time is still valid.

If internet still fails after redeem, restart the AP once (installs sudoers +
re-syncs paid MACs):

```bash
sudo ./gateway/start-ap.sh
```

## Stop

```bash
sudo ./gateway/stop-ap.sh
```

Then reconnect Wi‑Fi to `Cleopa_5G` in Ubuntu Settings if you want.

## Optional settings (`gateway/ap.env`)

| Variable | Meaning |
|----------|---------|
| `AP_ROLE` | `pc` = this computer broadcasts Wi‑Fi; `device` = hardware AP on `LAN_IF` |
| `SSID` | Network name phones see (`AP_ROLE=pc` only) |
| `WIFI_PASS` | Empty = open; or set 8+ chars for WPA2 |
| `WAN_IF` / `LAN_IF` | Internet cable vs customer side |
| `PORTAL_IP` / `PORTAL_NET` | Address of **this PC** on the customer network |
| `CAPTIVE_BLOCK=yes` | No internet until voucher |
| `CHANNEL=6` | 2.4 GHz channel (`AP_ROLE=pc`) |

When you buy a real AP: set `AP_ROLE=device`, plug the AP into `LAN_IF` in **bridge / AP mode with DHCP off**. Keep `PORTAL_IP` on this PC. Full explanation: `docs/PROJECT.md`.

## Notes

- Starting the AP **turns off** PC Wi‑Fi client mode. Keep using **Ethernet** for internet on the PC.
- While the AP is running, Ubuntu **Wi‑Fi Settings may say “no network adapter”**. That is expected: the Wi‑Fi card is in Access Point mode (`Perfect-WiFi`), so NetworkManager is not managing it as a client. Run `sudo ./gateway/stop-ap.sh` to get the adapter back in Settings.
- Tell customers to **disable randomized / private MAC** for Perfect-WiFi so reconnect keeps their paid session.
  If they pay with a random MAC then switch to device MAC, the portal used to show “online” while internet stayed blocked — that is fixed (session follows the on-wire MAC).
- If Perfect-WiFi connects but apps have no internet while the Fibre router Wi‑Fi works, run once:
  `sudo ./gateway/ensure-forward.sh`  
  (also applied automatically on authorize / `start-ap.sh` — needed because Docker often blocks FORWARD).
- Selcom is not required for this voucher demo.
- The Android menu **Use this network / Do not use this network** is a phone system screen (not part of our portal CSS). Tap **Use this network** after a successful voucher.
