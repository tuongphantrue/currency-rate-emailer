"""
currency_rate_emailer.py

Fetches exchange rates for a watchlist of currencies -> VND, from five
independent sources, and emails a summary. Designed to run on GitHub Actions
(see .github/workflows/send-currency-rate.yml) or locally via cron. No local
computer needs to stay on.

Data sources (each rendered as its own section, each degrades gracefully if
it fails on a given run):
  1. Market mid-rate: https://www.exchangerate-api.com/ (open.er-api.com, free, no key)
  2. Vietcombank official buy/sell rates: https://www.vietcombank.com.vn/ (public JSON endpoint;
     may be blocked from cloud/datacenter IPs like GitHub Actions runners)
  3. fawazahmed0/currency-api: free, no key, independent aggregator (jsDelivr CDN + pages.dev mirror)
  4. exchangerate.fun (haxqer/FreeExchangeRateApi): free, no key, hourly updates
  5. fxratesapi.com: free, no key needed for the latest-rates endpoint

Extra features:
  - Best-rate highlight: which source gives you the most/least VND per currency
  - Cross-source discrepancy alert: flags currencies where sources disagree a lot
  - Quick amount conversion: converts configured VND amounts into your watchlist
  - Historical tracking + weekly trend: logs every run to rate_history.csv and
    emails a 7-day % change summary once a week

Usage:
    python currency_rate_emailer.py generate   # fetch rates, build email body -> email_body.txt
    python currency_rate_emailer.py send       # send email_body.txt via SMTP

Required environment variables (set as GitHub Actions secrets, or export locally):
    GMAIL_ADDRESS       - sender gmail address
    GMAIL_APP_PASSWORD  - Gmail App Password (not your normal password)
    CURRENCY_RECIPIENT  - recipient email address

Optional environment variables:
    WATCHLIST                     - comma-separated currency codes, default below
    ALERT_THRESHOLD_PERCENT       - only send if some rate moved >= this % since last run
                                     (leave unset to always send)
    DISCREPANCY_THRESHOLD_PERCENT - flag a currency if sources disagree by >= this % (default 1.0)
    CONVERT_AMOUNTS_VND           - comma-separated VND amounts to quick-convert, e.g. "1000000,5000000"
"""

import os
import sys
import csv
import json
import smtplib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText

import requests

# --- Config -------------------------------------------------------------

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

def now_vn():
    """Current time in Vietnam (UTC+7), regardless of the runner's local timezone."""
    return datetime.now(VN_TZ)


DEFAULT_WATCHLIST = ["USD", "EUR", "JPY", "CNY", "KRW", "GBP", "SGD", "AUD"]
WATCHLIST = os.environ.get("WATCHLIST", ",".join(DEFAULT_WATCHLIST)).split(",")
WATCHLIST = [c.strip() for c in WATCHLIST]

MARKET_API_URL = "https://open.er-api.com/v6/latest/VND"  # base=VND -> we invert to VND-per-unit
VCB_API_URL = "https://www.vietcombank.com.vn/api/exchangerates"  # official VCB buy/sell, already in VND

# fawazahmed0/currency-api: free, no key, independent aggregator, mirrored on two CDNs
# so a fallback is available if the primary CDN has a hiccup.
FAWAZ_PRIMARY_URL = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/vnd.json"
FAWAZ_FALLBACK_URL = "https://latest.currency-api.pages.dev/v1/currencies/vnd.json"

# exchangerate.fun (haxqer/FreeExchangeRateApi): free, no key, hourly updates, base is
# always USD regardless of the `base` query param, so we compute cross-rates ourselves.
FUN_API_URL = "https://api.exchangerate.fun/latest"

# fxratesapi.com: free, no key needed for the latest-rates endpoint (per their docs/npm wrapper).
FXRATES_API_URL = "https://api.fxratesapi.com/latest"

# Sent with every request. Several of these hosts block the bare default
# "python-requests/x.y" User-Agent, so we look like an ordinary browser instead.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

SOURCES = [
    ("Market mid-rate", "https://www.exchangerate-api.com/"),
    ("Vietcombank official rate", "https://www.vietcombank.com.vn/en-us/personal/support/exchange-rates"),
    ("fawazahmed0/currency-api", "https://github.com/fawazahmed0/currency-api"),
    ("exchangerate.fun", "https://www.exchangerate.fun/"),
    ("fxratesapi.com", "https://fxratesapi.com/"),
]

EMAIL_BODY_FILE = "email_body.txt"
STATE_FILE = "last_rates.json"
HISTORY_FILE = "rate_history.csv"

ALERT_THRESHOLD_PERCENT = os.environ.get("ALERT_THRESHOLD_PERCENT")
ALERT_THRESHOLD_PERCENT = float(ALERT_THRESHOLD_PERCENT) if ALERT_THRESHOLD_PERCENT else None

DISCREPANCY_THRESHOLD_PERCENT = float(os.environ.get("DISCREPANCY_THRESHOLD_PERCENT", "1.0"))

CONVERT_AMOUNTS_VND = [
    float(a) for a in os.environ.get("CONVERT_AMOUNTS_VND", "1000000,5000000,10000000").split(",") if a.strip()
]

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
CURRENCY_RECIPIENT = os.environ.get("CURRENCY_RECIPIENT")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


# --- Fetch ----------------------------------------------------------------

def fetch_market_rates():
    """Returns {currency_code: VND_per_unit} from the market mid-rate API."""
    resp = requests.get(MARKET_API_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("result") != "success":
        raise RuntimeError(f"Market API error: {data}")

    vnd_to_x = data["rates"]  # base is VND, e.g. {"USD": 0.0000398, ...}
    rates = {}
    for code in WATCHLIST:
        rate = vnd_to_x.get(code)
        if rate:
            rates[code] = 1 / rate  # invert -> VND per 1 unit of `code`
    return rates


def fetch_vcb_rates():
    """Returns {currency_code: {"buy": VND, "sell": VND}} from Vietcombank's public feed.
    Rates are already denominated in VND, no inversion needed. If a currency isn't
    in VCB's list, it's simply omitted (falls back to market-only in the email).
    """
    resp = requests.get(VCB_API_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    rates = {}
    for row in data.get("Data", []):
        code = row.get("currencyCode")
        if code and code.strip() in WATCHLIST:
            try:
                buy = float(row.get("transfer") or row.get("cash") or 0)
                sell = float(row.get("sell") or 0)
            except ValueError:
                continue
            if buy or sell:
                rates[code] = {"buy": buy, "sell": sell}
    return rates


def fetch_fawaz_rates():
    """Returns {currency_code: VND_per_unit} from fawazahmed0/currency-api (base=VND).
    Tries the jsDelivr CDN first, falls back to the pages.dev mirror if that fails.
    """
    vnd_to_x = None
    last_error = None
    for url in (FAWAZ_PRIMARY_URL, FAWAZ_FALLBACK_URL):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            vnd_to_x = data["vnd"]  # e.g. {"usd": 0.0000398, ...}
            break
        except Exception as e:
            last_error = e
            continue

    if vnd_to_x is None:
        raise RuntimeError(f"Both fawazahmed0 endpoints failed: {last_error}")

    rates = {}
    for code in WATCHLIST:
        rate = vnd_to_x.get(code.lower())
        if rate:
            rates[code] = 1 / rate  # invert -> VND per 1 unit of `code`
    return rates


def fetch_fun_rates():
    """Returns {currency_code: VND_per_unit} from exchangerate.fun.
    The API always responds with base=USD regardless of the `base` param requested,
    so we compute the VND cross-rate ourselves: VND per 1 X = rates[VND] / rates[X].
    """
    resp = requests.get(FUN_API_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    usd_to_x = data["rates"]  # base is USD, e.g. {"VND": 26253.6, "EUR": 0.87, ...}
    vnd_per_usd = usd_to_x.get("VND")
    if not vnd_per_usd:
        raise RuntimeError("VND not present in exchangerate.fun response")

    rates = {}
    for code in WATCHLIST:
        if code == "USD":
            rates[code] = vnd_per_usd
            continue
        usd_per_code = usd_to_x.get(code)
        if usd_per_code:
            rates[code] = vnd_per_usd / usd_per_code
    return rates


def fetch_fxrates_rates():
    """Returns {currency_code: VND_per_unit} from fxratesapi.com (base=VND, no key needed)."""
    params = {
        "base": "VND",
        "currencies": ",".join(WATCHLIST),
        "format": "json",
    }
    resp = requests.get(FXRATES_API_URL, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        raise RuntimeError(f"fxratesapi.com error: {data}")

    vnd_to_x = data["rates"]  # base is VND, e.g. {"USD": 0.0000398, ...}
    rates = {}
    for code in WATCHLIST:
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


# --- Historical tracking + weekly trend -------------------------------------

def append_history(rates):
    """Appends this run's market rates to a CSV: timestamp,currency,rate"""
    is_new_file = not os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["timestamp", "currency", "rate"])
        ts = now_vn().strftime("%Y-%m-%d %H:%M")
        for code, rate in rates.items():
            writer.writerow([ts, code, rate])


def weekly_trend_section():
    """Once a week (first run after midnight Monday, Vietnam time), compares
    today's rate to the rate from ~7 days ago and returns a summary section,
    or None if it's not time yet / there's not enough history.
    """
    vn_now = now_vn()
    is_weekly_slot = vn_now.weekday() == 0 and vn_now.hour == 0  # Monday, 00:xx
    if not is_weekly_slot or not os.path.exists(HISTORY_FILE):
        return None

    cutoff = vn_now - timedelta(days=7)
    oldest_near_cutoff = {}  # currency -> (timestamp, rate) closest to 7 days ago
    latest = {}  # currency -> (timestamp, rate) most recent

    with open(HISTORY_FILE) as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M").replace(tzinfo=VN_TZ)
                rate = float(row["rate"])
            except (ValueError, KeyError):
                continue
            code = row["currency"]

            if code not in latest or ts > latest[code][0]:
                latest[code] = (ts, rate)

            if ts <= cutoff and (code not in oldest_near_cutoff or ts > oldest_near_cutoff[code][0]):
                oldest_near_cutoff[code] = (ts, rate)

    lines = []
    for code in WATCHLIST:
        if code in latest and code in oldest_near_cutoff:
            _, old_rate = oldest_near_cutoff[code]
            _, new_rate = latest[code]
            pct = (new_rate - old_rate) / old_rate * 100
            arrow = "UP" if pct > 0 else ("DOWN" if pct < 0 else "FLAT")
            lines.append(f"{code:<10}{arrow} {pct:+.2f}% over the past week")

    if not lines:
        return None  # not enough history yet (less than a week of data)

    return ["Weekly trend (7-day change)"] + ["-" * 38] + lines


# --- Best-rate + discrepancy analysis ----------------------------------------

def collect_comparable_rates(rates, vcb_rates, fawaz_rates, fun_rates, fxrates_rates):
    """Builds {currency: {source_name: vnd_per_unit}} across all successful sources.
    VCB contributes the average of its buy/sell as a single comparable figure.
    """
    comparable = {code: {} for code in WATCHLIST}

    for code, rate in rates.items():
        comparable[code][SOURCES[0][0]] = rate

    for code, vals in vcb_rates.items():
        avg = (vals["buy"] + vals["sell"]) / 2
        comparable[code][SOURCES[1][0]] = avg

    for code, rate in fawaz_rates.items():
        comparable[code][SOURCES[2][0]] = rate

    for code, rate in fun_rates.items():
        comparable[code][SOURCES[3][0]] = rate

    for code, rate in fxrates_rates.items():
        comparable[code][SOURCES[4][0]] = rate

    return comparable


def best_rate_section(comparable):
    """For each currency with 2+ sources, shows which source gives the most/least VND."""
    lines = []
    for code in WATCHLIST:
        by_source = comparable.get(code, {})
        if len(by_source) < 2:
            continue
        best_source, best_rate = max(by_source.items(), key=lambda kv: kv[1])
        worst_source, worst_rate = min(by_source.items(), key=lambda kv: kv[1])
        if best_source == worst_source:
            continue
        lines.append(f"{code:<5} highest: {best_rate:,.2f} VND ({best_source})")
        lines.append(f"{'':<5} lowest:  {worst_rate:,.2f} VND ({worst_source})")

    if not lines:
        return None
    return ["Best / lowest rate by source"] + ["-" * 38] + lines


def discrepancy_section(comparable):
    """Flags currencies where sources disagree by >= DISCREPANCY_THRESHOLD_PERCENT."""
    lines = []
    for code in WATCHLIST:
        by_source = comparable.get(code, {})
        if len(by_source) < 2:
            continue
        max_rate = max(by_source.values())
        min_rate = min(by_source.values())
        spread_pct = (max_rate - min_rate) / min_rate * 100
        if spread_pct >= DISCREPANCY_THRESHOLD_PERCENT:
            lines.append(f"{code:<5} sources disagree by {spread_pct:.2f}% (range {min_rate:,.2f} - {max_rate:,.2f} VND)")

    if not lines:
        return None
    return [f"Source discrepancy alert (>= {DISCREPANCY_THRESHOLD_PERCENT:.1f}% spread)"] + ["-" * 38] + lines


# --- Quick amount conversion --------------------------------------------------

def conversion_section(rates):
    """Converts each configured VND amount into every watchlist currency, using
    the market mid-rate.
    """
    if not CONVERT_AMOUNTS_VND or not rates:
        return None

    lines = ["Quick conversions (market mid-rate)"]
    lines.append("-" * 38)
    for amount in CONVERT_AMOUNTS_VND:
        parts = []
        for code in WATCHLIST:
            if code in rates:
                converted = amount / rates[code]
                parts.append(f"{code} {converted:,.2f}")
        lines.append(f"{amount:,.0f} VND = " + " | ".join(parts))
    return lines


# --- Formatting -------------------------------------------------------------

def format_email_body(rates, vcb_rates, fawaz_rates, fun_rates, fxrates_rates, previous_rates,
                       vcb_error=None, fawaz_error=None, fun_error=None, fxrates_error=None):
    lines = [f"Exchange rates to VND - {now_vn().strftime('%Y-%m-%d %H:%M')}\n"]

    comparable = collect_comparable_rates(rates, vcb_rates, fawaz_rates, fun_rates, fxrates_rates)

    best = best_rate_section(comparable)
    if best:
        lines += best + [""]

    discrepancy = discrepancy_section(comparable)
    if discrepancy:
        lines += discrepancy + [""]

    lines.append("Market mid-rate")
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

    used_sources = [SOURCES[0]]  # market mid-rate always used if we got this far

    if vcb_rates:
        lines.append("")
        lines.append("Vietcombank official rate")
        lines.append(f"{'Currency':<10}{'Buy (VND)':<16}{'Sell (VND)'}")
        lines.append("-" * 38)
        for code in rates:
            if code in vcb_rates:
                buy = vcb_rates[code]["buy"]
                sell = vcb_rates[code]["sell"]
                lines.append(f"{code:<10}{buy:,.2f}{'':<4}{sell:,.2f}")
        used_sources.append(SOURCES[1])
    elif vcb_error:
        lines.append("")
        lines.append(f"Vietcombank official rate: unavailable this run ({vcb_error})")

    if fawaz_rates:
        lines.append("")
        lines.append("fawazahmed0/currency-api (independent aggregator)")
        lines.append(f"{'Currency':<10}{'1 unit = VND'}")
        lines.append("-" * 38)
        for code in rates:
            if code in fawaz_rates:
                lines.append(f"{code:<10}{fawaz_rates[code]:,.2f}")
        used_sources.append(SOURCES[2])
    elif fawaz_error:
        lines.append("")
        lines.append(f"fawazahmed0/currency-api: unavailable this run ({fawaz_error})")

    if fun_rates:
        lines.append("")
        lines.append("exchangerate.fun (independent aggregator)")
        lines.append(f"{'Currency':<10}{'1 unit = VND'}")
        lines.append("-" * 38)
        for code in rates:
            if code in fun_rates:
                lines.append(f"{code:<10}{fun_rates[code]:,.2f}")
        used_sources.append(SOURCES[3])
    elif fun_error:
        lines.append("")
        lines.append(f"exchangerate.fun: unavailable this run ({fun_error})")

    if fxrates_rates:
        lines.append("")
        lines.append("fxratesapi.com (independent aggregator)")
        lines.append(f"{'Currency':<10}{'1 unit = VND'}")
        lines.append("-" * 38)
        for code in rates:
            if code in fxrates_rates:
                lines.append(f"{code:<10}{fxrates_rates[code]:,.2f}")
        used_sources.append(SOURCES[4])
    elif fxrates_error:
        lines.append("")
        lines.append(f"fxratesapi.com: unavailable this run ({fxrates_error})")

    conversions = conversion_section(rates)
    if conversions:
        lines.append("")
        lines += conversions

    trend = weekly_trend_section()
    if trend:
        lines.append("")
        lines += trend

    lines.append("")
    lines.append("Sources:")
    for name, url in used_sources:
        lines.append(f"  {name}: {url}")

    return "\n".join(lines)


# --- Email --------------------------------------------------------------------

def send_email(body):
    msg = MIMEText(body)
    msg["Subject"] = f"Daily Exchange Rates -> VND - {now_vn().strftime('%Y-%m-%d %H:%M')}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = CURRENCY_RECIPIENT

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [CURRENCY_RECIPIENT], msg.as_string())


# --- Commands -----------------------------------------------------------------

def cmd_generate():
    rates = fetch_market_rates()
    previous_rates = load_previous_rates()

    if not should_send(rates, previous_rates):
        print("No significant change, skipping email.")
        open(EMAIL_BODY_FILE, "w").close()
        return

    try:
        vcb_rates = fetch_vcb_rates()
        vcb_error = None
    except Exception as e:
        print(f"Vietcombank source failed ({e}), continuing without it.")
        vcb_rates = {}
        vcb_error = str(e)

    try:
        fawaz_rates = fetch_fawaz_rates()
        fawaz_error = None
    except Exception as e:
        print(f"fawazahmed0 source failed ({e}), continuing without it.")
        fawaz_rates = {}
        fawaz_error = str(e)

    try:
        fun_rates = fetch_fun_rates()
        fun_error = None
    except Exception as e:
        print(f"exchangerate.fun source failed ({e}), continuing without it.")
        fun_rates = {}
        fun_error = str(e)

    try:
        fxrates_rates = fetch_fxrates_rates()
        fxrates_error = None
    except Exception as e:
        print(f"fxratesapi.com source failed ({e}), continuing without it.")
        fxrates_rates = {}
        fxrates_error = str(e)

    body = format_email_body(rates, vcb_rates, fawaz_rates, fun_rates, fxrates_rates, previous_rates,
                              vcb_error, fawaz_error, fun_error, fxrates_error)
    with open(EMAIL_BODY_FILE, "w") as f:
        f.write(body)

    print(body)
    save_rates(rates)
    append_history(rates)


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
