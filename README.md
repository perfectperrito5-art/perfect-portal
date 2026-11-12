# Perfect Portal

Software captive Wi‑Fi for small businesses: customers join the hotspot, pay or redeem a voucher, and get internet for a set time.

**Full project documentation** (architecture, who it is for, challenges, how to switch from PC-as-AP to a hardware AP):

**[docs/PROJECT.md](docs/PROJECT.md)**

Access-point runbook: [gateway/README-AP.md](gateway/README-AP.md)

## Quick start

```bash
cd /home/admin_perfect/perfect_portal
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

- Portal: http://127.0.0.1:5000/
- Admin: http://127.0.0.1:5000/auth/login (credentials from `.env`)

## Hotspot on this PC

Ethernet stays on the ISP router; Wi‑Fi becomes the customer AP:

```bash
sudo ./gateway/start-ap.sh
```

Network addresses, subnet, and whether the radio is **this PC** or a **real AP** are all in `gateway/ap.env` (`AP_ROLE=pc` or `AP_ROLE=device`). They are not hardcoded.

## Notes

- Change `SECRET_KEY` and the admin password before production.
- AzamPay online checkout is configurable and remains off until merchant credentials and callback verification are configured. Voucher/direct-payment mode remains available.
- A PowerPoint deck can be produced later from `docs/PROJECT.md`.
