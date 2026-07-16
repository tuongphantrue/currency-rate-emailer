"""
currency_rate_emailer.py

Fetches exchange rates for a watchlist of currencies -> VND and emails a summary.
Designed to run on GitHub Actions (see .github/workflows/send-currency-rate.yml)
or locally via cron. No local computer needs to stay on.

Data source: https://www.exchangerate-api.com/ (open.er-api.com, free, no key needed)

Usage:
    python currency_rate_emailer.py generate   # fetch rates, build email body -> email_body.txt
    python currency_rate_emailer.py send       # send email_body.txt via SMTP

Required environment variables (set as GitHub Actions secrets, or export locally):
    GMAIL_ADDRESS       - sender gmail address
    GMAIL_APP_PASSWORD  - Gmail App Password (not your normal password)
    CURRENCY_RECIPIENT  - recipient email address

Optional environment variables:
    WATCHLIST                  - comma-separated currency codes, default below
    ALERT_THRESHOLD_PERCENT    - only send if some rate moved >= this % since last run
                                  (leave unset to always send)
"""

import os
import sys
import json
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import requests

# --- Config -------------------------------------------------------------

DEFAULT_WATCHLIST = ["USD", "EUR", "JPY", "CNY", "KRW", "GBP", "SGD", "AUD"]
WATCHLIST = os.environ.get("WATCHLIST", ",".join(DEFAULT_WATCHLIST)).split(",")

API_URL = "https://open.er-api.com/v6/latest/VND"  # base=VND -> we invert to VND-per-unit

EMAIL_BODY_FILE = "email_body.txt"
STATE_FILE = "last_rates.json"

ALERT_THRESHOLD_PERCENT = os.environ.get("ALERT_THRESHOLD_PERCENT")
ALERT_THRESHOLD_PERCENT = float(ALERT_THRESHOLD_PERCENT) if ALERT_THRESHOLD_PERCENT else None

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
CURRENCY_RECIPIENT = os.environ.get("CURRENCY_RECIPIENT")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


# --- Fetch ----------------------------------------------------------------

def fetch_rates():
    """Returns {currency_code: VND_per_unit}."""
    resp = requests.get(API_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("result") != "success":
        raise RuntimeError(f"API error: {data}")

    vnd_to_x = data["rates"]  # base is VND, e.g. {"USD": 0.0000398, ...}
    rates = {}
    for code in WATCHLIST:
        code = code.strip()
        rate = vnd_to_x.get(code)
        if rate:
            rates[code] = 1 / rate  # invert -> VND per 1 unit of `code`
    return rates


# --- State (for % change + threshold) --------------------------------------

def load_previous_rates():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def save_rates(rates):
    with open(STATE_FILE, "w") as f:
        json.dump(rates, f)


def should_send(rates, previous_rates):
    if ALERT_THRESHOLD_PERCENT is None or previous_rates is None:
        return True
    for code, rate in rates.items():
        if code in previous_rates:
            pct = abs((rate - previous_rates[code]) / previous_rates[code] * 100)
            if pct >= ALERT_THRESHOLD_PERCENT:
                return True
    return False


# --- Formatting -------------------------------------------------------------

def format_email_body(rates, previous_rates):
    lines = [f"Exchange rates to VND - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    lines.append(f"{'Currency':<10}{'1 unit = VND':<18}{'Change'}")
    lines.append("-" * 38)

    for code, rate in rates.items():
        change_str = ""
        if previous_rates and code in previous_rates:
            prev = previous_rates[code]
            pct = (rate - prev) / prev * 100
            arrow = "UP" if pct > 0 else ("DOWN" if pct < 0 else "FLAT")
            change_str = f"{arrow} {pct:+.2f}%"
        lines.append(f"{code:<10}{rate:,.2f}{'':<6}{change_str}")

    return "\n".join(lines)


# --- Email --------------------------------------------------------------------

def send_email(body):
    msg = MIMEText(body)
    msg["Subject"] = "Daily Exchange Rates -> VND"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = CURRENCY_RECIPIENT

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [CURRENCY_RECIPIENT], msg.as_string())


# --- Commands -----------------------------------------------------------------

def cmd_generate():
    rates = fetch_rates()
    previous_rates = load_previous_rates()

    if not should_send(rates, previous_rates):
        print("No significant change, skipping email.")
        # Write an empty marker so `send` knows to skip
        open(EMAIL_BODY_FILE, "w").close()
        return

    body = format_email_body(rates, previous_rates)
    with open(EMAIL_BODY_FILE, "w") as f:
        f.write(body)

    print(body)
    save_rates(rates)


def cmd_send():
    if not os.path.exists(EMAIL_BODY_FILE):
        print("No email body found, run 'generate' first.")
        return

    with open(EMAIL_BODY_FILE) as f:
        body = f.read()

    if not body.strip():
        print("Email body empty, nothing to send.")
        return

    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and CURRENCY_RECIPIENT):
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD / CURRENCY_RECIPIENT not set, skipping send.")
        return

    send_email(body)
    print("Email sent.")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "generate"
    if command == "generate":
        cmd_generate()
    elif command == "send":
        cmd_send()
    else:
        print(f"Unknown command: {command}. Use 'generate' or 'send'.")
