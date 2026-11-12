# Perfect Portal

**Paid Wi‑Fi access for shops, cafés, lodges, and similar businesses — run from a Linux computer, independent of which ISP router you use.**

This document is written for three audiences at once:

| Reader | What to read first |
|--------|--------------------|
| Business owner | Sections 1–3 and 8 |
| Network technician | Sections 4–6 and 11 |
| Programmer | Sections 4, 7, 9–11 |

A slide deck can be built from this document later. This file is the source of truth.

---

## 1. Executive summary

Perfect Portal is a **captive-portal hotspot**: a customer joins the Wi‑Fi, sees a branded page, pays (or receives a voucher from an attendant), and then gets internet for a **fixed duration**.

The product is **software on a Linux PC**, not firmware locked inside one brand of router. The fibre modem (FiberHome today, another brand tomorrow) only supplies internet. If the modem is replaced, you keep the same portal and mainly update **network addresses** so the PC and the customer Wi‑Fi stay on one subnet.

**Today:** the same PC is both the **server** (website, vouchers, sessions) and the **access point** (it broadcasts Wi‑Fi).

**Tomorrow:** you can keep the PC as the server and attach a **real access-point device** as the radio. That is a configuration choice (`AP_ROLE` plus interface and IP settings). It is not hardcoded.

---

## 2. Who this is for

The system fits businesses that sell **time online**, not a monthly home subscription:

- Cafés, restaurants, and bars  
- Small shops and kiosks  
- Guest houses, lodges, and waiting rooms  
- Salons, pharmacies, and similar walk-in trade  
- Events or pop-up sites that already have fibre or a router  

**End users of the software**

| Role | What they do |
|------|----------------|
| Customer | Connects to Wi‑Fi, follows “how to get online”, redeems a voucher |
| Attendant / cashier | Receives payment, issues a voucher, can call/SMS the customer |
| Owner / admin | Sets packages, prices, branding, vouchers, and can cut a session |

It is **not** a replacement for an ISP, a mesh Wi‑Fi product, or a full hotel PMS. It is the **paid-access layer** in front of an existing internet link.

---

## 3. What the customer experiences

1. Phone joins the hotspot (SSID, e.g. Perfect-WiFi).  
2. The phone opens a sign-in / captive page (or the customer visits the portal URL).  
3. They pick a package and follow short steps (Kiswahili on the “how to get online” block).  
4. They pay the business number, then get a voucher from the attendant.  
5. They enter the voucher → **You’re connected**.  
6. Internet stays on until the **package duration ends**, or an admin revokes the device.

AzamPay online checkout is available but **off by default** until merchant credentials and callback verification are configured. Voucher/direct-payment mode remains available.

---

## 4. Architecture (how the pieces fit)

The design is three layers. Keeping them separate is what makes the ISP router and the Wi‑Fi radio replaceable.

```
                    CUSTOMERS (phones)
                            │
                            │  Wi-Fi
                            ▼
                 ┌─────────────────────┐
                 │  Access Point role  │  AP_ROLE=pc  → this PC's Wi-Fi
                 │  (radio only)       │  AP_ROLE=device → hardware AP
                 └──────────┬──────────┘
                            │  LAN (customer subnet)
                            ▼
                 ┌─────────────────────┐
                 │  Gateway + Portal   │  Always this Linux PC
                 │  Flask · DHCP · DNS │  PORTAL_IP is this machine
                 │  nftables allow-list│
                 └──────────┬──────────┘
                            │  WAN (Ethernet)
                            ▼
                 ┌─────────────────────┐
                 │  ISP router / ONT   │  FiberHome, ZTE, … — internet only
                 └─────────────────────┘
                            │
                            ▼
                         Internet
```

### 4.1 Portal application (Python / Flask)

- Customer pages (packages, voucher redeem, connected/status)  
- Admin (packages, vouchers, payments, sessions, appearance, password)  
- Database of packages, vouchers, and **timed sessions**  
- Calls `gateway/authorize.sh` after a successful redeem  

### 4.2 Gateway (this PC)

- DHCP/DNS for customers (`dnsmasq`)  
- NAT from the customer network out through the ISP Ethernet  
- Firewall: unpaid MACs cannot browse; paid MACs are allow-listed **with a timeout**  
- Optional: `hostapd` when this PC is the Wi‑Fi radio  

### 4.3 Uplink (ISP device)

- Provides internet on Ethernet  
- Does **not** need a built-in captive portal  
- Change of brand (FiberHome → ZTE, etc.) is a **WAN/IP** change, not a rewrite of the portal  

**Important distinction:** the **portal IP** is always the computer that runs Flask. A cheap hardware AP usually **cannot** run this application. When you buy an AP, you point customers at the **PC’s LAN IP**, and the AP only transmits radio. You do not “move the portal IP onto the AP” unless that AP is itself a Linux box running this software.

---

## 5. Deployment modes (you choose)

All of this lives in `gateway/ap.env`. After editing, run `sudo ./gateway/start-ap.sh`.

### Mode A — PC is server + access point (`AP_ROLE=pc`)

Current setup.

| Setting | Meaning | Example |
|---------|---------|---------|
| `WAN_IF` | Cable to the ISP router | `enp0s31f6` |
| `LAN_IF` | PC Wi‑Fi card used as hotspot | `wlp1s0` |
| `PORTAL_IP` | Address of this PC on the customer network | `192.168.3.11` |
| `PORTAL_NET` | Customer subnet | `192.168.3.0/24` |

Phones get DHCP from the PC and use `PORTAL_IP` as gateway and DNS.

### Mode B — Hardware AP, PC stays the brain (`AP_ROLE=device`)

When you buy a real access point:

1. Put the AP in **access-point / bridge mode** and **turn off DHCP on the AP**.  
2. Plug the AP into the PC’s **customer LAN** port (`LAN_IF` — often a second Ethernet or USB Ethernet).  
3. Keep Ethernet **WAN** to the ISP router.  
4. Set:

```env
AP_ROLE="device"
WAN_IF="enp0s31f6"          # still the ISP cable
LAN_IF="enpXsY"             # cable to the hardware AP
PORTAL_IP="192.168.3.11"    # still THIS PC — not the AP's default 192.168.1.1
PORTAL_NET="192.168.3.0/24"
```

5. Match `.env`:

```env
PORTAL_BASE_URL=http://192.168.3.11:5000
```

The AP’s own management IP (often `192.168.0.1` / `192.168.1.1`) is only for configuring the AP. Customers must **not** use the AP as their internet gateway, or the PC cannot enforce paid time.

### Changing the ISP router

Keep `AP_ROLE` as it is. Update `WAN_IF` if the Ethernet name changes, and keep the PC on a subnet that can reach the new router (or keep the customer network as a **separate** subnet behind the PC, which is the recommended design).

---

## 6. Package duration (how time is counted)

A package such as **24 hours** is stored as **1440 minutes**, then converted to **exact UTC seconds** when the voucher is redeemed.

| Clock | Role |
|-------|------|
| UTC unix timestamp | Source of truth for start and expiry |
| `DISPLAY_TIMEZONE` (default `Africa/Dar_es_Salaam`) | What customers and admin **see** |
| nftables MAC timeout | Firewall copy of **remaining seconds** (refreshed while the portal runs) |

A background job every 30 seconds:

- removes expired sessions from the firewall  
- re-applies remaining time for sessions that are still valid  

**Admin packages** are entered as a number **plus a unit** (minutes / hours / days). A full-day package is **24 Hours**, not `24` minutes.

Screens that show expiry use local time (e.g. EAT) and a remaining label such as `23h 50m left`.

---

## 7. Software map (for programmers)

```
perfect_portal/
  run.py                 application entry
  .env                   secrets and PORTAL_BASE_URL
  app/
    blueprints/          portal, admin, auth, api
    models/              packages, vouchers, sessions, payments
    services/            access, vouchers, theme, payments, clock
    templates/           customer + admin HTML
    static/              CSS / uploads
  gateway/
    ap.env               IPs, interfaces, AP_ROLE
    start-ap.sh          bring up radio + DHCP + firewall + portal
    stop-ap.sh
    authorize.sh         allow/revoke a MAC for N seconds
    ensure-forward.sh    NAT / Docker FORWARD
  instance/portal.db     SQLite
  docs/PROJECT.md        this file
```

Internet unlock path:

`Redeem voucher → grant_access() → authorize.sh authorize MAC <seconds> → nftables set portal_allowed`

---

## 8. Operations (short)

**Start hotspot**

```bash
cd /home/admin_perfect/perfect_portal
sudo ./gateway/start-ap.sh
```

**Admin:** `http://<PORTAL_IP>:5000/auth/login`

**Stop:** `sudo ./gateway/stop-ap.sh`

Keep the PC **awake**. If it sleeps, Wi‑Fi and timers stop with it.

Tell customers to **disable randomized / private MAC** for this SSID so reconnect keeps the paid session.

---

## 9. Security (honest scope)

In place today:

- Admin password hashing, CSRF on forms, rate limits  
- Captive firewall until a MAC is authorized  
- Secrets in `.env` (not for public git)  
- Voucher codes; AzamPay callback endpoint  

Not a full enterprise NAC product. HTTPS for the portal is recommended once a certificate and public name exist. The current LAN hotspot is HTTP on the customer subnet.

---

## 10. Challenges faced (engineering log)

These are real issues from building and testing this system, recorded so operators and reviewers are not surprised.

### 10.1 Captive phones vs a normal website

Phones open the portal under fake hostnames (`connectivitycheck.gstatic.com`, Apple captive URLs, etc.). Cookies and CSRF tokens then belong to the **wrong host**. Redeem posted to `10.x` / `192.168.x` produced **“Bad Request … missing”** instead of the success page.

**Response:** force GET traffic to `PORTAL_BASE_URL`, same-host form POST, friendly CSRF error, absolute links after unlock.

### 10.2 “Success” then no internet

Causes included: authorize script not running as root; **randomized MAC** vs cookie MAC; Docker’s FORWARD chain blocking AP traffic; kernel `7.0.0-28` breaking Wi‑Fi AP mode.

**Response:** sudoers for `authorize.sh`, prefer ARP/lease MAC, `ensure-forward.sh`, pin a working kernel (e.g. `6.17.0-35-generic`).

### 10.3 Package time feeling shorter than advertised

Several independent effects stacked:

- Expiry was shown as **UTC** while people read it as **East Africa Time** (three hours earlier on the clock).  
- Firewall timeouts were applied in **whole minutes**, then refreshed from remaining minutes (rounding).  
- Admin duration was **minutes only**, so “24” could be saved as 24 minutes instead of 24 hours.  
- SQLite datetime has **no timezone**, which is unsafe for “exactly 24 hours”.  
- A 12-hour DHCP lease on a 24-hour package caused extra IP changes (session still MAC-based, but messy).  
- Laptop **sleep** pauses the radio and timers.

**Response:** store expiry as UTC unix seconds; firewall timeout in **seconds**; display EAT + time remaining; duration amount + unit in admin; 24-hour DHCP lease; janitor every 30 seconds; keep the PC from sleeping in production.

### 10.4 Language and layout

English-only steps blocked some customers. Long package lists pushed instructions off the screen.

**Response:** short Kiswahili on the howto steps; packages scroll in a fixed area; howto + voucher button stay in a dock.

### 10.5 Mobile-money checkout

AzamPay is the configured mobile-money provider. Merchant credentials and a publicly reachable callback endpoint are required before enabling online checkout.
### 10.6 Range

A laptop Intel Wi‑Fi card as AP covers roughly one room well. A dedicated AP is the proper range upgrade; the software is already split so the radio can change.

### 10.7 Environment mismatch

The stack is **Linux**. Sending a `.rar` of the source to Windows is fine for reading code; running the gateway (hostapd, nftables) is not a Windows feature. A Windows reviewer would run only the Flask UI, or deploy Linux.

---

## 11. Limitations and next steps

| Limitation | Practical meaning |
|------------|-------------------|
| One gateway PC | That machine must stay on |
| Open or simple WPA hotspot | Captive UX is easier on open Wi‑Fi; WPA is optional in `ap.env` |
| No per-user bandwidth cap yet | Time is sold, not megabytes |
| AzamPay checkout not configured | Voucher / attendant flow |
| Presentation | PowerPoint can be produced from this document when needed |

Reasonable later work: HTTPS, bandwidth packages, reseller vouchers, SMS receipts, dedicated AP hardware, and a business slide deck.

---

## 12. Glossary

| Term | Meaning |
|------|---------|
| Captive portal | Network that intercepts new devices until they complete a page (pay / voucher) |
| MAC address | Hardware id of the phone’s Wi‑Fi; used to allow internet |
| ONT / ISP router | Fibre box from the provider; internet uplink only |
| Access point | Radio that broadcasts Wi‑Fi |
| Gateway | Device that NATs customers to the internet and enforces the allow-list |
| Voucher | One-time code tied to a time package |

---

*Perfect Portal — software captive hotspot for small businesses. Document version aligned with the duration, AP-role, and architecture work described above.*
