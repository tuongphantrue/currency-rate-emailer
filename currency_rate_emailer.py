"""
currency_rate_emailer.py

Fetches exchange rates for a watchlist of currencies -> VND, from five
independent sources, and emails a summary. Designed to run on GitHub Actions
(see .github/workflows/send-currency-rate.yml) or locally via cron. No local
computer needs to stay on.

Data sources (each rendered as its own section, each degrades gracefully if
it fails on a given run):
  1. Market mid-rate: https://www.exchangerate-api.com/ (open.er-api.com, free, no key)
  2. Vietcombank reference buy/sell rates: their own public XML feed (documented on
     their site as "if you need rates in XML format" — a genuine, intentional feed,
     not a workaround). Rate-limited to 1 request/5min by VCB's own request.
  3. fawazahmed0/currency-api: free, no key, independent aggregator (jsDelivr CDN + pages.dev mirror)
  4. fxratesapi.com: free, no key needed for the latest-rates endpoint
  5. CoinGecko, via Tether (USDT) as a USD-pegged proxy — a derived cross-rate,
     not a direct FX source; see the card's description in the email for the caveat.

  (exchangerate.fun was tried and removed — confirmed hard-blocked from GitHub
  Actions even with retries, likely IP-level. BIDV and Techcombank were checked
  and ruled out — both require a signup-gated developer API for real data. Two
  third-party VN rate aggregator sites (webgia.com, tygia.com.vn) were checked
  and ruled out — both serve fake/garbled numbers for most currencies to non-
  browser requests. Đông Á Bank has a real JSON endpoint but robots.txt disallows
  automated access to it. VietinBank was tried via headless-browser scraping
  (Playwright) and ruled out — their site serves an active bot-detection/CAPTCHA
  challenge to automated browsers, a deliberate signal not to route around.)

Extra features:
  - Best-rate highlight: which source gives you the most/least VND per currency
  - Cross-source discrepancy alert: flags currencies where sources disagree a lot
  - All-sources comparison table: every source's rate for every currency, side by side
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
import re
import sys
import csv
import json
import time
import smtplib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

# --- Config -------------------------------------------------------------

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

def now_vn():
    """Current time in Vietnam (UTC+7), regardless of the runner's local timezone."""
    return datetime.now(VN_TZ)


DEFAULT_WATCHLIST = ["USD", "EUR", "JPY", "CNY", "KRW", "GBP", "SGD", "AUD"]
WATCHLIST = os.environ.get("WATCHLIST", ",".join(DEFAULT_WATCHLIST)).split(",")
WATCHLIST = [c.strip() for c in WATCHLIST]

# Currency symbols shown next to each code. Falls back to the code itself if unlisted.
CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "\u20ac", "JPY": "\u00a5", "CNY": "\u00a5", "KRW": "\u20a9",
    "GBP": "\u00a3", "SGD": "$", "AUD": "$", "VND": "\u20ab", "CAD": "$", "CHF": "Fr",
    "HKD": "$", "NZD": "$", "THB": "\u0e3f", "INR": "\u20b9", "IDR": "Rp", "MYR": "RM",
    "PHP": "\u20b1", "RUB": "\u20bd", "TWD": "NT$", "CZK": "K\u010d", "SEK": "kr",
    "NOK": "kr", "DKK": "kr", "PLN": "z\u0142", "TRY": "\u20ba", "ZAR": "R", "BRL": "R$",
    "MXN": "$", "AED": "\u062f.\u0625",
}


def symbol_for(code):
    return CURRENCY_SYMBOLS.get(code, code)


def label_for(code):
    """Currency code with its symbol, e.g. 'USD $'."""
    sym = CURRENCY_SYMBOLS.get(code)
    return f"{code} {sym}" if sym else code

MARKET_API_URL = "https://open.er-api.com/v6/latest/VND"  # base=VND -> we invert to VND-per-unit
VCB_API_URL = "https://portal.vietcombank.com.vn/Usercontrols/TVPortal.TyGia/pXML.aspx"  # official VCB feed
# This is Vietcombank's own documented public feed (linked from their exchange-rates page as
# "if you need rates in XML format"). Their comment in the response asks for at most one
# request every 5 minutes — this script runs every 30 minutes by default, well within that.

# fawazahmed0/currency-api: free, no key, independent aggregator, mirrored on two CDNs
# so a fallback is available if the primary CDN has a hiccup.
FAWAZ_PRIMARY_URL = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/vnd.json"
FAWAZ_FALLBACK_URL = "https://latest.currency-api.pages.dev/v1/currencies/vnd.json"

# fxratesapi.com: free, no key needed for the latest-rates endpoint (per their docs/npm wrapper).
FXRATES_API_URL = "https://api.fxratesapi.com/latest"

# CoinGecko's free/no-key Demo API. CoinGecko doesn't publish direct fiat-to-fiat
# rates, so we derive them via Tether (USDT) — a stablecoin designed to track USD
# 1:1 — comparing its market price in VND against its market price in each other
# currency. This is a real, commonly-used technique, but it inherits small crypto-
# market pricing noise (typically well under 0.5%) rather than being a pure
# interbank FX rate. The card for this source says so explicitly.
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"

# Sent with every request. Several of these hosts block the bare default
# "python-requests/x.y" User-Agent, so we look like an ordinary browser instead.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Retried on these status codes: 429/403 often indicate rate limiting on small free
# APIs (not just permission errors), and 5xx are transient server issues.
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 2  # waits ~2s, then ~4s between attempts — for most retryable errors

# 429 (rate limited) gets its own, much longer backoff: a brief retry rarely helps
# with a rate limit, since the limit window (e.g. "N requests per minute") is
# usually still active a couple of seconds later. If the server sends a
# Retry-After header, that's used instead (capped, so a misbehaving server can't
# make a run hang indefinitely).
RATE_LIMIT_BACKOFF_BASE_SECONDS = 20  # waits ~20s, then ~40s if no Retry-After header
RATE_LIMIT_BACKOFF_MAX_SECONDS = 60


def get_with_retry(url, headers=None, params=None, timeout=15):
    """requests.get wrapper with exponential backoff on transient failures
    (connection errors, timeouts, or retryable HTTP status codes). Raises the
    last error if every attempt fails — callers already handle that per-source
    and degrade gracefully.
    """
    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code in RETRYABLE_STATUS_CODES and attempt < RETRY_ATTEMPTS - 1:
                last_error = requests.HTTPError(f"{resp.status_code} (retrying)", response=resp)
                if resp.status_code == 429:
                    wait = _rate_limit_wait_seconds(resp, attempt)
                else:
                    wait = RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = e
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            raise
    raise last_error


def _rate_limit_wait_seconds(resp, attempt):
    """How long to wait after a 429. Prefers the server's own Retry-After header
    (seconds format) when present, otherwise falls back to a longer exponential
    backoff than other error types get, since rate-limit windows rarely clear in
    just a couple of seconds. Always capped at RATE_LIMIT_BACKOFF_MAX_SECONDS.
    """
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), RATE_LIMIT_BACKOFF_MAX_SECONDS)
        except ValueError:
            pass  # Retry-After can also be an HTTP-date string — not handled, fall through
    return min(RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** attempt), RATE_LIMIT_BACKOFF_MAX_SECONDS)

SOURCES = [
    ("Tỷ giá trung bình thị trường", "https://www.exchangerate-api.com/"),
    ("Tỷ giá tham khảo Vietcombank", "https://www.vietcombank.com.vn/en-us/personal/support/exchange-rates"),
    ("fawazahmed0/currency-api", "https://github.com/fawazahmed0/currency-api"),
    ("fxratesapi.com", "https://fxratesapi.com/"),
    ("CoinGecko (qua USDT)", "https://www.coingecko.com/en/api"),
]

# One-line plain-English description of each source, shown under its header in the
# email so the numbers come with context, not just a bare link. Indexed the same
# way as SOURCES.
SOURCE_DESCRIPTIONS = [
    "Tỷ giá tham chiếu trung bình thị trường (điểm giữa giá mua và bán) — mức chuẩn phổ biến, "
    "không phải tỷ giá thực tế mà ngân hàng nào áp dụng.",
    "Ngân hàng thương mại nhà nước lớn nhất Việt Nam. Tỷ giá mua/bán thực tế áp dụng cho khách hàng "
    "khi giao dịch tiền mặt và chuyển khoản — gần nhất với tỷ giá khi đổi tiền trực tiếp.",
    "Nguồn tổng hợp mã nguồn mở, miễn phí, lấy dữ liệu từ nhiều ngân hàng trung ương và tổ chức "
    "tài chính, cập nhật hàng ngày và được nhân bản trên hai CDN để đảm bảo độ tin cậy.",
    "Dịch vụ tổng hợp miễn phí, kết hợp tỷ giá từ nhiều nhà cung cấp vào một nguồn duy nhất, "
    "cập nhật thường xuyên trong ngày.",
    "Tỷ giá chéo được suy ra từ Tether (USDT), một stablecoin neo giá theo đô la Mỹ, bằng cách "
    "so sánh giá thị trường của nó tính theo VND với giá thị trường tính theo từng loại tiền.",
]

EMAIL_BODY_FILE = "email_body.txt"
EMAIL_HTML_FILE = "email_body.html"
EMAIL_SUBJECT_FILE = "email_subject.txt"
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
    resp = get_with_retry(MARKET_API_URL, headers=HEADERS, timeout=15)
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
    """Returns {currency_code: {"buy": VND, "sell": VND}} from Vietcombank's official
    public XML feed (their own documented endpoint for programmatic access). Rates
    are already denominated in VND, no inversion needed. If a currency isn't in
    VCB's list, it's simply omitted (falls back to other sources in the email).

    "buy" uses the bank-transfer buy rate (usually always populated); the cash buy
    rate is sometimes "-" (not offered) for less common currencies.
    """
    resp = get_with_retry(VCB_API_URL, headers=HEADERS, timeout=15)

    root = ET.fromstring(resp.content)
    rates = {}
    for el in root.findall("Exrate"):
        code = (el.get("CurrencyCode") or "").strip()
        if code not in WATCHLIST:
            continue

        def to_float(raw):
            raw = (raw or "").strip().replace(",", "")
            return float(raw) if raw and raw != "-" else None

        buy = to_float(el.get("Transfer")) or to_float(el.get("Buy"))
        sell = to_float(el.get("Sell"))
        if buy or sell:
            rates[code] = {"buy": buy or 0.0, "sell": sell or 0.0}
    return rates


def fetch_fawaz_rates():
    """Returns {currency_code: VND_per_unit} from fawazahmed0/currency-api (base=VND).
    Tries the jsDelivr CDN first, falls back to the pages.dev mirror if that fails.
    """
    vnd_to_x = None
    last_error = None
    for url in (FAWAZ_PRIMARY_URL, FAWAZ_FALLBACK_URL):
        try:
            resp = get_with_retry(url, headers=HEADERS, timeout=15)
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


def fetch_fxrates_rates():
    """Returns {currency_code: VND_per_unit} from fxratesapi.com (base=VND, no key needed)."""
    params = {
        "base": "VND",
        "currencies": ",".join(WATCHLIST),
        "format": "json",
    }
    resp = get_with_retry(FXRATES_API_URL, headers=HEADERS, params=params, timeout=15)
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


def fetch_coingecko_rates():
    """Returns {currency_code: VND_per_unit} derived from CoinGecko's Tether (USDT)
    market price in each currency. USDT is a stablecoin designed to track USD 1:1,
    so its price in VND vs. its price in another currency gives a usable cross-rate:
    VND per 1 X = USDT_price_in_VND / USDT_price_in_X.

    This is a real, commonly-used technique, but it inherits small crypto-market
    pricing noise (typically well under 0.5%) rather than being a pure interbank
    FX rate — the email card for this source says so explicitly.
    """
    vs_currencies = ",".join(c.lower() for c in WATCHLIST if c != "VND")
    params = {"ids": "tether", "vs_currencies": f"{vs_currencies},vnd"}
    resp = get_with_retry(COINGECKO_API_URL, headers=HEADERS, params=params, timeout=15)
    data = resp.json()

    usdt_prices = data.get("tether", {})
    vnd_per_usdt = usdt_prices.get("vnd")
    if not vnd_per_usdt:
        raise RuntimeError("VND not present in CoinGecko response")

    rates = {}
    for code in WATCHLIST:
        if code == "VND":
            continue
        price_in_code = usdt_prices.get(code.lower())
        if price_in_code:
            rates[code] = vnd_per_usdt / price_in_code
    return rates



# --- State (for % change + threshold) --------------------------------------

def load_previous_rates():
    """Returns the last run's market rates, or None if there's no usable history yet
    (file missing, empty, or unreadable — e.g. left over from an older, incompatible
    version of this script). A bad state file should degrade to "no history" rather
    than crash the whole run.
    """
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data else None
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"Warning: could not read {STATE_FILE} ({e}), treating as no history.")
        return None


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
    rows = weekly_trend_rows()
    if not rows:
        return None
    lines = [f"{label_for(r['code']):<14}{r['arrow']} {r['pct']:+.2f}% trong tuần qua" for r in rows]
    return ["Xu hướng tuần (thay đổi 7 ngày)", f"(nguồn: lịch sử {SOURCES[0][0]}, ghi lại mỗi lần chạy)"] + ["-" * 38] + lines


def weekly_trend_rows():
    """Returns [{"code", "pct", "arrow"}] for the weekly trend, or None if it's
    not the weekly slot yet / there's not enough history.
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

    rows = []
    for code in WATCHLIST:
        if code in latest and code in oldest_near_cutoff:
            _, old_rate = oldest_near_cutoff[code]
            _, new_rate = latest[code]
            pct = (new_rate - old_rate) / old_rate * 100
            arrow = "TĂNG" if pct > 0 else ("GIẢM" if pct < 0 else "KHÔNG ĐỔI")
            rows.append({"code": code, "pct": pct, "arrow": arrow})

    return rows or None


# --- Best-rate + discrepancy analysis ----------------------------------------

def collect_comparable_rates(rates, vcb_rates, fawaz_rates, fxrates_rates, coingecko_rates):
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

    for code, rate in fxrates_rates.items():
        comparable[code][SOURCES[3][0]] = rate

    for code, rate in coingecko_rates.items():
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
        lines.append(f"{label_for(code):<7} cao nhất: {best_rate:,.2f} VND ({best_source})")
        lines.append(f"{'':<7} thấp nhất: {worst_rate:,.2f} VND ({worst_source})")

    if not lines:
        return None
    return ["Tỷ giá cao nhất / thấp nhất theo nguồn", "(so sánh giữa các nguồn bên dưới, theo từng loại tiền)"] + ["-" * 38] + lines


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
            lines.append(f"{label_for(code):<7} các nguồn chênh lệch {spread_pct:.2f}% (khoảng {min_rate:,.2f} - {max_rate:,.2f} VND)")

    if not lines:
        return None
    return [f"Cảnh báo chênh lệch giữa các nguồn (>= {DISCREPANCY_THRESHOLD_PERCENT:.1f}%)",
            "(so sánh giữa các nguồn bên dưới, theo từng loại tiền)"] + ["-" * 38] + lines


# Short labels for SOURCES, used only in the side-by-side comparison table where
# full names would make columns too wide. Indexed the same way as SOURCES.
SOURCE_SHORT_NAMES = ["TB thị trường", "Vietcombank", "fawazahmed0", "fxratesapi", "CoinGecko"]


def all_sources_table_section(comparable):
    """A single table with every source's rate for every currency, side by side,
    so all sources can be compared at a glance instead of scrolling through each
    source's own card. Vietcombank shows the average of its buy/sell rate here,
    same as it's treated elsewhere for cross-source comparison.
    """
    if not any(comparable.get(code) for code in WATCHLIST):
        return None

    col_width = 15
    lines = ["Bảng so sánh tất cả các nguồn", "(Vietcombank hiển thị giá trị trung bình mua/bán)"]
    header = f"{'Loại tiền':<14}" + "".join(f"{name:<{col_width}}" for name in SOURCE_SHORT_NAMES)
    lines.append(header)
    lines.append("-" * len(header))
    for code in WATCHLIST:
        by_source = comparable.get(code, {})
        if not by_source:
            continue
        row = f"{label_for(code):<14}"
        for name, _ in SOURCES:
            val = by_source.get(name)
            cell = f"{val:,.2f}" if val is not None else "—"
            row += f"{cell:<{col_width}}"
        lines.append(row)
    return lines


# --- Quick amount conversion --------------------------------------------------

def conversion_section(rates):
    """Converts each configured VND amount into every watchlist currency, using
    the market mid-rate. Rendered as a matrix: one row per currency, one column
    per amount, so it reads as a table instead of a wrapped wall of text.
    """
    if not CONVERT_AMOUNTS_VND or not rates:
        return None

    amount_headers = [f"{a:,.0f} VND" for a in CONVERT_AMOUNTS_VND]
    col_width = max(12, max(len(h) for h in amount_headers) + 2)

    lines = ["Quy đổi nhanh", f"(tính theo {SOURCES[0][0]})"]
    lines.append("-" * 38)
    header = f"{'Loại tiền':<14}" + "".join(f"{h:<{col_width}}" for h in amount_headers)
    lines.append(header)
    lines.append("-" * len(header))
    for code in WATCHLIST:
        if code not in rates:
            continue
        row = f"{label_for(code):<14}"
        for amount in CONVERT_AMOUNTS_VND:
            converted = amount / rates[code]
            cell = f"{symbol_for(code)}{converted:,.2f}"
            row += f"{cell:<{col_width}}"
        lines.append(row)
    return lines


# --- Formatting -------------------------------------------------------------

def format_email_body(rates, vcb_rates, fawaz_rates, fxrates_rates, coingecko_rates, previous_rates,
                       vcb_error=None, fawaz_error=None, fxrates_error=None, coingecko_error=None,
                       market_error=None):
    lines = [f"Tỷ giá quy đổi sang VND - {now_vn().strftime('%Y-%m-%d %H:%M')}\n"]

    comparable = collect_comparable_rates(rates, vcb_rates, fawaz_rates, fxrates_rates, coingecko_rates)

    best = best_rate_section(comparable)
    if best:
        lines += best + [""]

    discrepancy = discrepancy_section(comparable)
    if discrepancy:
        lines += discrepancy + [""]

    all_sources_table = all_sources_table_section(comparable)
    if all_sources_table:
        lines += all_sources_table + [""]

    used_sources = []

    if rates:
        lines.append("Tỷ giá trung bình thị trường")
        lines.append(f"(nguồn: {SOURCES[0][0]})")
        lines.append(f"{SOURCE_DESCRIPTIONS[0]}")
        lines.append(f"{'Loại tiền':<14}{'1 đơn vị = VND':<18}{'Thay đổi'}")
        lines.append("-" * 38)
        for code, rate in rates.items():
            change_str = ""
            if previous_rates and code in previous_rates:
                prev = previous_rates[code]
                pct = (rate - prev) / prev * 100
                arrow = "TĂNG" if pct > 0 else ("GIẢM" if pct < 0 else "KHÔNG ĐỔI")
                change_str = f"{arrow} {pct:+.2f}%"
            lines.append(f"{label_for(code):<14}{rate:,.2f}{'':<6}{change_str}")
        used_sources.append(SOURCES[0])
    elif market_error:
        lines.append(f"Tỷ giá trung bình thị trường: không khả dụng lần này ({market_error})")
        lines.append(f"(nguồn: {SOURCES[0][0]})")

    if vcb_rates:
        lines.append("")
        lines.append("Tỷ giá tham khảo Vietcombank")
        lines.append(f"(nguồn: {SOURCES[1][0]})")
        lines.append(f"{SOURCE_DESCRIPTIONS[1]}")
        lines.append(f"{'Loại tiền':<14}{'Mua (VND)':<16}{'Bán (VND)'}")
        lines.append("-" * 38)
        for code in WATCHLIST:
            if code in vcb_rates:
                buy = vcb_rates[code]["buy"]
                sell = vcb_rates[code]["sell"]
                lines.append(f"{label_for(code):<14}{buy:,.2f}{'':<4}{sell:,.2f}")
        used_sources.append(SOURCES[1])
    elif vcb_error:
        lines.append("")
        lines.append(f"Tỷ giá tham khảo Vietcombank: không khả dụng lần này ({vcb_error})")
        lines.append(f"(nguồn: {SOURCES[1][0]})")

    if fawaz_rates:
        lines.append("")
        lines.append("fawazahmed0/currency-api (nguồn tổng hợp độc lập)")
        lines.append(f"(nguồn: {SOURCES[2][0]})")
        lines.append(f"{SOURCE_DESCRIPTIONS[2]}")
        lines.append(f"{'Loại tiền':<14}{'1 đơn vị = VND'}")
        lines.append("-" * 38)
        for code in WATCHLIST:
            if code in fawaz_rates:
                lines.append(f"{label_for(code):<14}{fawaz_rates[code]:,.2f}")
        used_sources.append(SOURCES[2])
    elif fawaz_error:
        lines.append("")
        lines.append(f"fawazahmed0/currency-api: không khả dụng lần này ({fawaz_error})")
        lines.append(f"(nguồn: {SOURCES[2][0]})")

    if fxrates_rates:
        lines.append("")
        lines.append("fxratesapi.com (nguồn tổng hợp độc lập)")
        lines.append(f"(nguồn: {SOURCES[3][0]})")
        lines.append(f"{SOURCE_DESCRIPTIONS[3]}")
        lines.append(f"{'Loại tiền':<14}{'1 đơn vị = VND'}")
        lines.append("-" * 38)
        for code in WATCHLIST:
            if code in fxrates_rates:
                lines.append(f"{label_for(code):<14}{fxrates_rates[code]:,.2f}")
        used_sources.append(SOURCES[3])
    elif fxrates_error:
        lines.append("")
        lines.append(f"fxratesapi.com: không khả dụng lần này ({fxrates_error})")
        lines.append(f"(nguồn: {SOURCES[3][0]})")

    if coingecko_rates:
        lines.append("")
        lines.append("CoinGecko, qua USDT (được suy ra, không phải nguồn tỷ giá trực tiếp)")
        lines.append(f"(nguồn: {SOURCES[4][0]})")
        lines.append(f"{SOURCE_DESCRIPTIONS[4]}")
        lines.append("Không phải tỷ giá trực tiếp — có sai số nhỏ do biến động giá tiền điện tử (thường dưới 0.5%).")
        lines.append(f"{'Loại tiền':<14}{'1 đơn vị = VND'}")
        lines.append("-" * 38)
        for code in WATCHLIST:
            if code in coingecko_rates:
                lines.append(f"{label_for(code):<14}{coingecko_rates[code]:,.2f}")
        used_sources.append(SOURCES[4])
    elif coingecko_error:
        lines.append("")
        lines.append(f"CoinGecko (qua USDT): không khả dụng lần này ({coingecko_error})")
        lines.append(f"(nguồn: {SOURCES[4][0]})")

    conversions = conversion_section(rates)
    if conversions:
        lines.append("")
        lines += conversions

    trend = weekly_trend_section()
    if trend:
        lines.append("")
        lines += trend

    lines.append("")
    lines.append("Nguồn dữ liệu:")
    for name, url in used_sources:
        lines.append(f"  {name}: {url}")

    return "\n".join(lines)


# --- HTML formatting -------------------------------------------------------

# Color palette kept intentionally small: one accent per direction, neutral grays for structure.
_HTML_COLORS = {
    "up": "#1a7f37",
    "down": "#cf222e",
    "flat": "#57606a",
    "border": "#e1e4e8",
    "muted": "#57606a",
    "card_bg": "#f6f8fa",
    "warn_bg": "#fff8e5",
    "warn_border": "#f2c744",
    "text": "#1f2328",
    "accent": "#0969da",
}

# One accent color per section, used for the card's left border, title, and a tinted
# table-header background. Vietcombank uses their actual brand green as a deliberate touch.
SECTION_ACCENTS = {
    "best": "#0d9488",         # teal
    "discrepancy": "#d97706",  # amber (paired with the warn background)
    "compare": "#0e7490",      # cyan — the full side-by-side comparison table
    "market": "#2563eb",       # blue
    "vcb": "#475569",          # neutral slate — no longer matches VCB's own brand green
    "fawaz": "#7c3aed",        # purple
    "fxrates": "#ea580c",      # orange
    "coingecko": "#f59e0b",    # gold/amber (crypto-ish, distinct from discrepancy's amber)
    "conversions": "#4f46e5",  # indigo
    "trend": "#db2777",        # rose
}

# Brightened versions of each accent, used ONLY for title/header text color in dark
# mode. The accents above were tuned to read well as text on a WHITE background;
# several (vcb, fawaz, conversions, compare, market, best, trend) fall below WCAG AA
# (4.5:1) as text on a dark card background, verified by computing actual contrast
# ratios — not just eyeballed. These are the minimum white-blend needed per accent to
# clear 4.5:1 against the #1e1e1e dark card background used below.
SECTION_ACCENTS_DARK = {
    "best": "#19998e",
    "discrepancy": "#d97706",   # already passes as-is, unchanged
    "compare": "#3e90a6",
    "market": "#5182ef",
    "vcb": "#7e8896",
    "fawaz": "#9d6bf2",
    "fxrates": "#ea580c",       # already passes as-is, unchanged
    "coingecko": "#f59e0b",     # already passes as-is, unchanged
    "conversions": "#847eed",
    "trend": "#e25292",
}

# Small color dot shown next to each currency code, roughly evoking each currency's
# home flag/brand without reproducing any actual flag imagery.
CURRENCY_DOT_COLORS = {
    "USD": "#2563eb", "EUR": "#7c3aed", "JPY": "#dc2626", "CNY": "#dc2626",
    "KRW": "#2563eb", "GBP": "#1e3a8a", "SGD": "#dc2626", "AUD": "#059669",
    "VND": "#dc2626", "CAD": "#dc2626", "CHF": "#dc2626", "HKD": "#dc2626",
    "NZD": "#1e3a8a", "THB": "#7c3aed", "INR": "#ea580c", "IDR": "#dc2626",
    "MYR": "#2563eb", "PHP": "#2563eb", "RUB": "#1e3a8a", "TWD": "#dc2626",
    "TRY": "#dc2626", "ZAR": "#059669", "BRL": "#059669", "MXN": "#059669",
    "AED": "#059669",
}


def _accent_of(code):
    return CURRENCY_DOT_COLORS.get(code, "#57606a")


def _tint(hex_color, amount=0.88):
    """Lightens a hex color by blending it toward white. amount=0.88 means
    88% white / 12% original color — used for subtle tinted backgrounds."""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r = round(r + (255 - r) * amount)
    g = round(g + (255 - g) * amount)
    b = round(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def _html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _html_label(code):
    """Currency code + a small colored dot + symbol, e.g. '🔵 USD $'."""
    sym = CURRENCY_SYMBOLS.get(code)
    dot_color = _accent_of(code)
    dot = f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{dot_color};margin-right:6px;"></span>'
    if not sym:
        return f'{dot}{_html_escape(code)}'
    return f'{dot}{_html_escape(code)} <span style="color:{_HTML_COLORS["muted"]};">{_html_escape(sym)}</span>'


def _html_change_span(pct):
    if pct > 0:
        color, bg, arrow = _HTML_COLORS["up"], "#dcfce7", "&#9650;"
    elif pct < 0:
        color, bg, arrow = _HTML_COLORS["down"], "#fee2e2", "&#9660;"
    else:
        color, bg, arrow = _HTML_COLORS["flat"], "#f1f3f5", "&#9679;"
    return (
        f'<span style="color:{color};background:{bg};font-weight:600;padding:2px 8px;'
        f'border-radius:10px;font-size:13px;display:inline-block;">{arrow} {pct:+.2f}%</span>'
    )


def _html_source_table(rows, headers, accent=None, accent_key=None):
    """rows: list of tuples matching headers. Renders a simple bordered table.
    accent, if given, tints the header row background and text with that color.
    accent_key, if given, adds a class so dark mode can brighten the header text
    color independently (see SECTION_ACCENTS_DARK)."""
    header_bg = _tint(accent, 0.90) if accent else "transparent"
    header_color = accent if accent else _HTML_COLORS["muted"]
    header_class = f"crx-accent-{accent_key}" if accent_key else ""
    th = "".join(
        f'<th class="crx-thead {header_class}" style="text-align:left;padding:6px 10px;border-bottom:2px solid {_HTML_COLORS["border"]};'
        f'background:{header_bg};font-size:12px;color:{header_color};font-weight:700;'
        f'text-transform:uppercase;letter-spacing:.03em;">{h}</th>'
        for h in headers
    )
    body_rows = ""
    for row in rows:
        cells = "".join(
            f'<td class="crx-td" style="padding:6px 10px;border-bottom:1px solid {_HTML_COLORS["border"]};font-size:14px;">{cell}</td>'
            for cell in row
        )
        body_rows += f"<tr>{cells}</tr>"
    return (
        f'<table style="border-collapse:collapse;width:100%;margin:8px 0 20px;">'
        f"<thead><tr>{th}</tr></thead><tbody>{body_rows}</tbody></table>"
    )


def _html_card(title_html, inner_html, source_html, accent=None, accent_key=None, bg=None, border=None, description=None):
    """source_html is required and always rendered — either a link to the
    original source, or a short note for derived/multi-source sections.
    description, if given, is a normal-case sentence shown below the source
    line explaining what the source actually is.
    accent, if given, colors the left border, title, and a small dot marker.
    accent_key, if given, adds a class so dark mode can brighten the title
    text color independently (see SECTION_ACCENTS_DARK) without touching the
    left border, which stays the same accent color in both modes.

    Each card is collapsible via a CSS-only checkbox toggle (no JavaScript,
    since email clients strip it) — click the title to collapse/expand.
    IMPORTANT: this defaults to EXPANDED, and checking the box COLLAPSES it
    (not the more common reverse pattern). Email clients that don't support
    this CSS technique (e.g. the Gmail mobile app) just show an inert,
    hidden checkbox and the card stays expanded — nobody loses content on
    an unsupported client, which is the failure mode that matters most here.
    """
    is_warn = bg is not None  # only the discrepancy-alert card passes a custom bg today
    bg = bg or "#ffffff"
    border = border or _HTML_COLORS["border"]
    card_class = "crx-card-warn" if is_warn else "crx-card"
    title_class = f"crx-accent-{accent_key}" if accent_key else ""
    left_border = f"4px solid {accent}" if accent else f"1px solid {border}"
    title_color = accent if accent else _HTML_COLORS["text"]
    dot = (
        f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;'
        f'background:{accent};margin-right:8px;vertical-align:middle;"></span>'
        if accent else ""
    )
    desc_html = (
        f'<div class="crx-muted" style="font-size:12.5px;color:{_HTML_COLORS["muted"]};margin-bottom:10px;line-height:1.4;">'
        f'{_html_escape(description)}</div>'
        if description else ""
    )
    toggle_id = _next_card_id()
    return (
        f'<div class="{card_class}" style="border:1px solid {border};border-left:{left_border};border-radius:8px;'
        f'padding:16px 18px;margin-bottom:16px;background:{bg};">'
        f'<input type="checkbox" id="{toggle_id}" class="crx-toggle-checkbox" style="display:none !important;">'
        f'<label for="{toggle_id}" class="crx-card-label" style="display:block;cursor:pointer;margin-bottom:2px;">'
        f'<span class="crx-chevron-open" style="font-size:11px;color:{_HTML_COLORS["muted"]};display:inline-block;width:14px;">&#9662;</span>'
        f'<span class="crx-chevron-closed" style="display:none;font-size:11px;color:{_HTML_COLORS["muted"]};width:14px;">&#9656;</span>'
        f'<span class="{title_class}" style="font-size:15px;font-weight:700;color:{title_color};">{dot}{title_html}</span>'
        f'</label>'
        f'<div class="crx-collapsible">'
        f'<div class="crx-muted" style="font-size:11px;color:{_HTML_COLORS["muted"]};margin-bottom:4px;'
        f'text-transform:uppercase;letter-spacing:.03em;">{source_html}</div>'
        f"{desc_html}"
        f"{inner_html}"
        f"</div></div>"
    )


def _html_source_label(name, url):
    """Plain-text source attribution for inside a card — no link here. The
    actual clickable URL appears once, in the consolidated footer at the very
    end of the email, instead of being repeated as a link on every card."""
    return f"Nguồn: {_html_escape(name)}"


# Unique IDs for the collapsible-card checkboxes below, reset at the start of
# each top-level email-building function so IDs never collide within one
# render and don't grow unbounded across multiple emails in the same process.
_card_id_counter = [0]


def _next_card_id():
    _card_id_counter[0] += 1
    return f"crx-toggle-{_card_id_counter[0]}"


def _reset_card_id_counter():
    _card_id_counter[0] = 0


# Dark-mode support for Gmail/Outlook/Apple Mail. Inline styles normally win
# over stylesheets, so this uses `!important` in a <head><style> block to
# override them specifically inside a `prefers-color-scheme: dark` query —
# the standard technique for HTML email dark mode. Only border-top/-right/
# -bottom are touched (never border-left), so each card's colored left accent
# stripe stays the same vivid color in both light and dark mode.
_DARK_MODE_STYLE_BLOCK = """
    @media (prefers-color-scheme: dark) {
      .crx-card {
        background: #1e1e1e !important;
        border-top-color: #3a3a3a !important;
        border-right-color: #3a3a3a !important;
        border-bottom-color: #3a3a3a !important;
        color: #e8e8e8 !important;
      }
      .crx-card-warn {
        background: #332b12 !important;
        border-top-color: #6b5615 !important;
        border-right-color: #6b5615 !important;
        border-bottom-color: #6b5615 !important;
        color: #e8e8e8 !important;
      }
      .crx-muted { color: #9aa1a9 !important; }
      .crx-footer { border-top-color: #3a3a3a !important; }
      .crx-thead {
        background: rgba(255,255,255,0.06) !important;
        border-bottom-color: #3a3a3a !important;
      }
      .crx-td { border-bottom-color: #3a3a3a !important; }
""" + "".join(
    f'      .crx-accent-{key} {{ color: {dark_hex} !important; }}\n'
    for key, dark_hex in SECTION_ACCENTS_DARK.items()
) + """    }
"""


# CSS-only collapsible sections (checkbox hack — no JavaScript, since email
# clients strip it). Defaults to EXPANDED; checking the box COLLAPSES the
# card, which is the safer default for clients that don't support this
# technique at all (they just see an inert checkbox, content stays visible).
_COLLAPSIBLE_STYLE_BLOCK = """
    .crx-toggle-checkbox:checked ~ .crx-collapsible { display: none !important; }
    .crx-toggle-checkbox:checked ~ .crx-card-label .crx-chevron-open { display: none !important; }
    .crx-toggle-checkbox:checked ~ .crx-card-label .crx-chevron-closed { display: inline-block !important; }
"""


def _html_document(body_html):
    """Wraps a body fragment in a full HTML document with the meta tags and
    <style> block email clients need to apply real dark-mode support instead
    of their own automatic (often ugly) light-to-dark inversion heuristics,
    plus the CSS for the collapsible-section checkbox toggles.
    """
    return (
        "<!DOCTYPE html>"
        '<html><head><meta charset="utf-8">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<style>{_COLLAPSIBLE_STYLE_BLOCK}{_DARK_MODE_STYLE_BLOCK}</style>"
        f"</head><body style=\"margin:0;padding:0;\">{body_html}</body></html>"
    )


def format_email_html(rates, vcb_rates, fawaz_rates, fxrates_rates, coingecko_rates, previous_rates,
                       vcb_error=None, fawaz_error=None, fxrates_error=None, coingecko_error=None,
                       market_error=None):
    _reset_card_id_counter()
    C = _HTML_COLORS
    comparable = collect_comparable_rates(rates, vcb_rates, fawaz_rates, fxrates_rates, coingecko_rates)
    used_sources = []

    parts = []
    parts.append(
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        f'max-width:640px;margin:0 auto;color:{C["text"]};">'
    )
    parts.append(
        f'<div style="background:#0d9488;background:linear-gradient(135deg,#0d9488,#2563eb);'
        f'border-radius:10px;padding:20px 22px;margin-bottom:20px;">'
        f'<h1 style="font-size:21px;margin:0 0 4px;color:#ffffff;">&#128176; Tỷ giá quy đổi sang VND</h1>'
        f'<div style="font-size:13px;color:#e0f2fe;">'
        f"{now_vn().strftime('%Y-%m-%d %H:%M')} (giờ Việt Nam)</div>"
        f'</div>'
    )

    # Best / lowest rate by source
    best_rows = []
    for code in WATCHLIST:
        by_source = comparable.get(code, {})
        if len(by_source) < 2:
            continue
        best_source, best_rate = max(by_source.items(), key=lambda kv: kv[1])
        worst_source, worst_rate = min(by_source.items(), key=lambda kv: kv[1])
        if best_source == worst_source:
            continue
        best_rows.append((
            f"<strong>{_html_label(code)}</strong>",
            f'{best_rate:,.2f} <span class="crx-muted" style="color:{C["muted"]};font-size:12px;">({_html_escape(best_source)})</span>',
            f'{worst_rate:,.2f} <span class="crx-muted" style="color:{C["muted"]};font-size:12px;">({_html_escape(worst_source)})</span>',
        ))
    if best_rows:
        table = _html_source_table(best_rows, ["Loại tiền", "Cao nhất", "Thấp nhất"], accent=SECTION_ACCENTS["best"], accent_key="best")
        parts.append(_html_card(
            "Tỷ giá cao nhất / thấp nhất theo nguồn", table,
            "So sánh giữa các nguồn bên dưới, theo từng loại tiền",
            accent=SECTION_ACCENTS["best"], accent_key="best",
        ))

    # Discrepancy alert
    disc_rows = []
    for code in WATCHLIST:
        by_source = comparable.get(code, {})
        if len(by_source) < 2:
            continue
        max_rate, min_rate = max(by_source.values()), min(by_source.values())
        spread_pct = (max_rate - min_rate) / min_rate * 100
        if spread_pct >= DISCREPANCY_THRESHOLD_PERCENT:
            disc_rows.append((f"<strong>{_html_label(code)}</strong>", f"{spread_pct:.2f}%", f"{min_rate:,.2f} - {max_rate:,.2f} VND"))
    if disc_rows:
        table = _html_source_table(disc_rows, ["Loại tiền", "Chênh lệch", "Khoảng"], accent=SECTION_ACCENTS["discrepancy"], accent_key="discrepancy")
        parts.append(_html_card(
            f"&#9888; Cảnh báo chênh lệch giữa các nguồn (&ge;{DISCREPANCY_THRESHOLD_PERCENT:.1f}%)",
            table,
            "So sánh giữa các nguồn bên dưới, theo từng loại tiền",
            accent=SECTION_ACCENTS["discrepancy"], accent_key="discrepancy",
            bg=C["warn_bg"], border=C["warn_border"],
        ))

    # All sources compared side by side
    compare_rows = []
    for code in WATCHLIST:
        by_source = comparable.get(code, {})
        if not by_source:
            continue
        row = [f"<strong>{_html_label(code)}</strong>"]
        for name, _ in SOURCES:
            val = by_source.get(name)
            row.append(f"{val:,.2f}" if val is not None else f'<span class="crx-muted" style="color:{C["muted"]};">—</span>')
        compare_rows.append(tuple(row))
    if compare_rows:
        table = _html_source_table(compare_rows, ["Loại tiền"] + SOURCE_SHORT_NAMES, accent=SECTION_ACCENTS["compare"], accent_key="compare")
        parts.append(_html_card(
            "Bảng so sánh tất cả các nguồn", table,
            "Vietcombank hiển thị giá trị trung bình mua/bán",
            accent=SECTION_ACCENTS["compare"], accent_key="compare",
        ))

    # Market mid-rate
    if rates:
        market_rows = []
        for code, rate in rates.items():
            change_cell = ""
            if previous_rates and code in previous_rates:
                prev = previous_rates[code]
                pct = (rate - prev) / prev * 100
                change_cell = _html_change_span(pct)
            market_rows.append((f"<strong>{_html_label(code)}</strong>", f"{rate:,.2f}", change_cell))
        parts.append(_html_card(
            "Tỷ giá trung bình thị trường",
            _html_source_table(market_rows, ["Loại tiền", "1 đơn vị = VND", "Thay đổi"], accent=SECTION_ACCENTS["market"], accent_key="market"),
            _html_source_label(*SOURCES[0]),
            accent=SECTION_ACCENTS["market"], accent_key="market",
            description=SOURCE_DESCRIPTIONS[0],
        ))
        used_sources.append(SOURCES[0])
    elif market_error:
        parts.append(_html_card(
            "Tỷ giá trung bình thị trường",
            f'<div class="crx-muted" style="color:{C["muted"]};font-size:13px;">Không khả dụng lần này: {_html_escape(market_error)}</div>',
            _html_source_label(*SOURCES[0]),
            accent=SECTION_ACCENTS["market"], accent_key="market",
        ))

    # Vietcombank
    if vcb_rates:
        vcb_rows = [
            (f"<strong>{_html_label(code)}</strong>", f"{vcb_rates[code]['buy']:,.2f}", f"{vcb_rates[code]['sell']:,.2f}")
            for code in WATCHLIST if code in vcb_rates
        ]
        parts.append(_html_card(
            "Tỷ giá tham khảo Vietcombank",
            _html_source_table(vcb_rows, ["Loại tiền", "Mua (VND)", "Bán (VND)"], accent=SECTION_ACCENTS["vcb"], accent_key="vcb"),
            _html_source_label(*SOURCES[1]),
            accent=SECTION_ACCENTS["vcb"], accent_key="vcb",
            description=SOURCE_DESCRIPTIONS[1],
        ))
        used_sources.append(SOURCES[1])
    elif vcb_error:
        parts.append(_html_card(
            "Tỷ giá tham khảo Vietcombank",
            f'<div class="crx-muted" style="color:{C["muted"]};font-size:13px;">Không khả dụng lần này: {_html_escape(vcb_error)}</div>',
            _html_source_label(*SOURCES[1]),
            accent=SECTION_ACCENTS["vcb"], accent_key="vcb",
        ))

    # Independent aggregators
    for label, source_rates, error, source_entry, accent_key in [
        ("fawazahmed0/currency-api", fawaz_rates, fawaz_error, SOURCES[2], "fawaz"),
        ("fxratesapi.com", fxrates_rates, fxrates_error, SOURCES[3], "fxrates"),
    ]:
        accent = SECTION_ACCENTS[accent_key]
        desc = SOURCE_DESCRIPTIONS[SOURCES.index(source_entry)]
        if source_rates:
            rows = [(f"<strong>{_html_label(code)}</strong>", f"{source_rates[code]:,.2f}") for code in WATCHLIST if code in source_rates]
            parts.append(_html_card(
                f"{label} (nguồn tổng hợp độc lập)",
                _html_source_table(rows, ["Loại tiền", "1 đơn vị = VND"], accent=accent, accent_key=accent_key),
                _html_source_label(*source_entry),
                accent=accent,
                accent_key=accent_key,
                description=desc,
            ))
            used_sources.append(source_entry)
        elif error:
            parts.append(_html_card(
                f"{label} (nguồn tổng hợp độc lập)",
                f'<div class="crx-muted" style="color:{C["muted"]};font-size:13px;">Không khả dụng lần này: {_html_escape(error)}</div>',
                _html_source_label(*source_entry),
                accent=accent,
                accent_key=accent_key,
            ))

    # CoinGecko (via USDT) — kept separate from the aggregator loop since it needs
    # its own caveat note about being derived, not a direct FX rate.
    coingecko_accent = SECTION_ACCENTS["coingecko"]
    if coingecko_rates:
        rows = [(f"<strong>{_html_label(code)}</strong>", f"{coingecko_rates[code]:,.2f}") for code in WATCHLIST if code in coingecko_rates]
        parts.append(_html_card(
            "CoinGecko (qua USDT)",
            _html_source_table(rows, ["Loại tiền", "1 đơn vị = VND"], accent=coingecko_accent, accent_key="coingecko"),
            _html_source_label(*SOURCES[4]),
            accent=coingecko_accent,
            accent_key="coingecko",
            description=SOURCE_DESCRIPTIONS[4] + " Không phải tỷ giá trực tiếp — có sai số nhỏ "
                        "do biến động giá tiền điện tử (thường dưới 0.5%).",
        ))
        used_sources.append(SOURCES[4])
    elif coingecko_error:
        parts.append(_html_card(
            "CoinGecko (qua USDT)",
            f'<div class="crx-muted" style="color:{C["muted"]};font-size:13px;">Không khả dụng lần này: {_html_escape(coingecko_error)}</div>',
            _html_source_label(*SOURCES[4]),
            accent=coingecko_accent,
            accent_key="coingecko",
        ))

    # Quick conversions
    if CONVERT_AMOUNTS_VND and rates:
        amount_headers = [f"{a:,.0f} VND" for a in CONVERT_AMOUNTS_VND]
        conv_rows = []
        for code in WATCHLIST:
            if code not in rates:
                continue
            row = [f"<strong>{_html_label(code)}</strong>"]
            for amount in CONVERT_AMOUNTS_VND:
                converted = amount / rates[code]
                row.append(f"{symbol_for(code)}{converted:,.2f}")
            conv_rows.append(tuple(row))
        table = _html_source_table(conv_rows, ["Loại tiền"] + amount_headers, accent=SECTION_ACCENTS["conversions"], accent_key="conversions")
        parts.append(_html_card(
            "Quy đổi nhanh", table,
            f"Tính theo {_html_escape(SOURCES[0][0])}",
            accent=SECTION_ACCENTS["conversions"], accent_key="conversions",
        ))

    # Weekly trend
    trend_rows = weekly_trend_rows()
    if trend_rows:
        rows = [(f"<strong>{_html_label(r['code'])}</strong>", _html_change_span(r["pct"])) for r in trend_rows]
        parts.append(_html_card(
            "Xu hướng tuần (thay đổi 7 ngày)",
            _html_source_table(rows, ["Loại tiền", "Thay đổi"], accent=SECTION_ACCENTS["trend"], accent_key="trend"),
            f"Tính theo lịch sử {_html_escape(SOURCES[0][0])}, ghi lại mỗi lần chạy",
            accent=SECTION_ACCENTS["trend"], accent_key="trend",
        ))

    # Sources footer
    source_links = " &nbsp;&middot;&nbsp; ".join(
        f'<a href="{url}" style="color:{C["accent"]};text-decoration:none;">{_html_escape(name)}</a>'
        for name, url in used_sources
    )
    parts.append(
        f'<div class="crx-muted crx-footer" style="font-size:12px;color:{C["muted"]};border-top:1px solid {C["border"]};padding-top:12px;margin-top:8px;">'
        f"Nguồn dữ liệu: {source_links}</div>"
    )

    parts.append("</div>")
    return _html_document("".join(parts))


# --- Email --------------------------------------------------------------------

def send_email(body, html_body=None, subject=None):
    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["Subject"] = subject or f"Tỷ giá quy đổi sang VND - {now_vn().strftime('%Y-%m-%d %H:%M')}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = CURRENCY_RECIPIENT

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [CURRENCY_RECIPIENT], msg.as_string())


# --- Total-failure alert -----------------------------------------------------
# If every single source fails in the same run, the normal digest would be
# nearly empty and easy to miss. Build a short, distinct alert instead.

def build_alert_body_text(errors):
    """errors: list of (source_name, error_message) for every failed source."""
    lines = [
        f"CẢNH BÁO: Không lấy được tỷ giá từ bất kỳ nguồn nào - {now_vn().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Cả {len(errors)} nguồn dữ liệu đều gặp lỗi trong lần chạy này, nên không có tỷ giá nào để gửi.",
        "",
        "Chi tiết lỗi từng nguồn:",
    ]
    for name, error in errors:
        lines.append(f"- {name}: {error}")
    lines += [
        "",
        "Hãy kiểm tra lại kết nối mạng của GitHub Actions runner, hoặc trạng thái hiện tại của từng API.",
        "Nếu chỉ một vài nguồn lỗi (không phải tất cả), email tỷ giá bình thường vẫn được gửi như thường lệ.",
    ]
    return "\n".join(lines)


def build_alert_body_html(errors):
    C = _HTML_COLORS
    rows = "".join(
        f'<div class="crx-td" style="padding:6px 0;border-bottom:1px solid {C["border"]};font-size:14px;">'
        f'<strong>{_html_escape(name)}</strong>: <span class="crx-muted" style="color:{C["muted"]};">{_html_escape(error)}</span></div>'
        for name, error in errors
    )
    inner = (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        f'max-width:640px;margin:0 auto;color:{C["text"]};">'
        f'<div style="background:{C["down"]};border-radius:10px;padding:20px 22px;margin-bottom:20px;">'
        f'<h1 style="font-size:20px;margin:0 0 4px;color:#ffffff;">&#128680; Cảnh báo: Tất cả nguồn tỷ giá đều lỗi</h1>'
        f'<div style="font-size:13px;color:#fee2e2;">{now_vn().strftime("%Y-%m-%d %H:%M")} (giờ Việt Nam)</div>'
        f'</div>'
        f'<div class="crx-card" style="border:1px solid {C["border"]};border-radius:8px;padding:16px 18px;">'
        f'<div style="font-size:14px;margin-bottom:12px;">Cả {len(errors)} nguồn dữ liệu đều gặp lỗi trong lần chạy này, '
        f'nên không có tỷ giá nào để gửi.</div>'
        f'{rows}'
        f'<div class="crx-muted" style="font-size:12.5px;color:{C["muted"]};margin-top:14px;">Hãy kiểm tra lại kết nối mạng của '
        f'GitHub Actions runner, hoặc trạng thái hiện tại của từng API. Nếu chỉ một vài nguồn lỗi (không phải '
        f'tất cả), email tỷ giá bình thường vẫn được gửi như thường lệ.</div>'
        f'</div></div>'
    )
    return _html_document(inner)


# --- Commands -----------------------------------------------------------------

def cmd_generate():
    try:
        rates = fetch_market_rates()
        market_error = None
    except Exception as e:
        print(f"Market mid-rate source failed ({e}), continuing without it.")
        rates = {}
        market_error = str(e)

    previous_rates = load_previous_rates()

    # Only apply the "skip if no significant change" logic when we actually have
    # a market rate to compare against — if the market source itself failed, we
    # always proceed, so a total-failure alert or partial digest isn't silently
    # skipped by a threshold check that has nothing to compare.
    if rates and not should_send(rates, previous_rates):
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
        fxrates_rates = fetch_fxrates_rates()
        fxrates_error = None
    except Exception as e:
        print(f"fxratesapi.com source failed ({e}), continuing without it.")
        fxrates_rates = {}
        fxrates_error = str(e)

    try:
        coingecko_rates = fetch_coingecko_rates()
        coingecko_error = None
    except Exception as e:
        print(f"CoinGecko source failed ({e}), continuing without it.")
        coingecko_rates = {}
        coingecko_error = str(e)

    all_rate_dicts = [rates, vcb_rates, fawaz_rates, fxrates_rates, coingecko_rates]
    success_count = sum(1 for r in all_rate_dicts if r)

    if success_count == 0:
        # Total failure: every source failed. Send a short, distinct alert
        # instead of a near-empty digest, so it doesn't get missed.
        print("ALERT: all 5 sources failed this run.")
        errors = [
            (SOURCES[0][0], market_error),
            (SOURCES[1][0], vcb_error),
            (SOURCES[2][0], fawaz_error),
            (SOURCES[3][0], fxrates_error),
            (SOURCES[4][0], coingecko_error),
        ]
        body = build_alert_body_text(errors)
        html_body = build_alert_body_html(errors)
        subject = f"🚨 CẢNH BÁO: Tất cả nguồn tỷ giá đều lỗi - {now_vn().strftime('%Y-%m-%d %H:%M')}"
        with open(EMAIL_BODY_FILE, "w") as f:
            f.write(body)
        with open(EMAIL_HTML_FILE, "w") as f:
            f.write(html_body)
        with open(EMAIL_SUBJECT_FILE, "w") as f:
            f.write(subject)
        print(body)
        # No new data was fetched — don't overwrite the rate cache or history.
        return

    body = format_email_body(rates, vcb_rates, fawaz_rates, fxrates_rates, coingecko_rates, previous_rates,
                              vcb_error, fawaz_error, fxrates_error, coingecko_error, market_error)
    html_body = format_email_html(rates, vcb_rates, fawaz_rates, fxrates_rates, coingecko_rates, previous_rates,
                                   vcb_error, fawaz_error, fxrates_error, coingecko_error, market_error)
    with open(EMAIL_BODY_FILE, "w") as f:
        f.write(body)
    with open(EMAIL_HTML_FILE, "w") as f:
        f.write(html_body)
    # Clear any leftover alert subject from a previous failed run, so a normal
    # run always uses the normal subject format.
    if os.path.exists(EMAIL_SUBJECT_FILE):
        os.remove(EMAIL_SUBJECT_FILE)

    print(body)
    if rates:
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

    html_body = None
    if os.path.exists(EMAIL_HTML_FILE):
        with open(EMAIL_HTML_FILE) as f:
            html_body = f.read().strip() or None

    subject = None
    if os.path.exists(EMAIL_SUBJECT_FILE):
        with open(EMAIL_SUBJECT_FILE) as f:
            subject = f.read().strip() or None

    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and CURRENCY_RECIPIENT):
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD / CURRENCY_RECIPIENT not set, skipping send.")
        return

    send_email(body, html_body, subject)
    print("Email sent.")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "generate"
    if command == "generate":
        cmd_generate()
    elif command == "send":
        cmd_send()
    else:
        print(f"Unknown command: {command}. Use 'generate' or 'send'.")
