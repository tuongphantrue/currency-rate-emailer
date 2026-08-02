# Currency Rate Emailer

Currency Rate Emailer collects exchange rates from multiple independent sources, compares them in Vietnamese dong (VND), and sends a styled email digest through Gmail. It is designed to run automatically with GitHub Actions, so no local computer needs to stay online.

The included workflow runs every 30 minutes. GitHub Actions may start scheduled jobs later than the exact cron time.

## Features

- Compares rates from five independent sources
- Tracks a configurable currency watchlist
- Shows the highest and lowest reported rate for each currency
- Flags large discrepancies between sources
- Includes a side-by-side all-sources comparison table
- Converts common VND amounts into every watched currency
- Records rate history and includes a weekly trend summary
- Sends both plain-text and responsive HTML email content
- Continues with a partial digest when individual sources fail
- Sends a distinct alert when every source fails
- Can suppress email when no rate has moved beyond a configured threshold

## Data sources

| Source | Data used | Notes |
| --- | --- | --- |
| [ExchangeRate-API](https://www.exchangerate-api.com/) | Market mid-rates | Uses the free `open.er-api.com` endpoint without an API key |
| [Vietcombank](https://www.vietcombank.com.vn/) | Official reference buy and sell rates | Uses Vietcombank's public XML exchange-rate feed |
| [fawazahmed0/currency-api](https://github.com/fawazahmed0/exchange-api) | Aggregated market rates | Uses jsDelivr with a Pages-hosted fallback |
| [FXRatesAPI](https://fxratesapi.com/) | Market rates | Uses the free latest-rates endpoint without an API key |
| [CoinGecko](https://www.coingecko.com/) | USDT-derived cross-rates | Uses Tether as a USD-pegged proxy, so small crypto-market differences are expected |

Each source is fetched independently. If one source is unavailable, the remaining results are still included in the digest.

## Default watchlist

```text
USD, EUR, JPY, CNY, KRW, GBP, SGD, AUD, CAD, CHF, HKD, THB, INR
```

All displayed values represent the number of VND for one unit of the selected currency.

## GitHub Actions setup

1. Fork or clone this repository.
2. Open the repository on GitHub and go to **Settings > Secrets and variables > Actions**.
3. Add these repository secrets:

   | Secret | Purpose |
   | --- | --- |
   | `GMAIL_ADDRESS` | Gmail address used to send the digest |
   | `GMAIL_APP_PASSWORD` | Gmail App Password used for SMTP authentication |
   | `CURRENCY_RECIPIENT` | Address that receives the digest |

4. Open the **Actions** tab and enable workflows if GitHub asks you to do so.
5. Run **Send Currency Rate** manually with **Run workflow**, or wait for the scheduled run.

The workflow performs two commands:

```powershell
python currency_rate_emailer.py generate
python currency_rate_emailer.py send
```

After a successful data fetch, it commits changes to `last_rates.json` and `rate_history.csv`. Those files provide comparisons with the previous run and the historical data used for trend summaries.

## Local usage

Python 3.11 or newer is recommended.

```powershell
git clone https://github.com/tuongphantrue/currency-rate-emailer.git
cd currency-rate-emailer
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set the email environment variables when you want to send a message:

```powershell
$env:GMAIL_ADDRESS = "sender@gmail.com"
$env:GMAIL_APP_PASSWORD = "your-gmail-app-password"
$env:CURRENCY_RECIPIENT = "recipient@example.com"
```

Generate the digest, inspect it if desired, and then send it:

```powershell
python currency_rate_emailer.py generate
python currency_rate_emailer.py send
```

Running the script without a command is equivalent to `generate`.

## Configuration

The script reads the following optional environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `WATCHLIST` | `USD,EUR,JPY,CNY,KRW,GBP,SGD,AUD,CAD,CHF,HKD,THB,INR` | Comma-separated ISO currency codes |
| `ALERT_THRESHOLD_PERCENT` | unset | Only produce a normal digest when at least one market rate changes by this percentage; unset means always produce it |
| `DISCREPANCY_THRESHOLD_PERCENT` | `1.0` | Flag a currency when the spread between sources reaches this percentage |
| `CONVERT_AMOUNTS_VND` | `1000000,5000000,10000000` | Comma-separated VND amounts for the quick-conversion table |

For local runs, set these variables in the shell before `generate`. For GitHub Actions, expose the desired values to the **Generate email** step. The checked-in workflow intentionally leaves `ALERT_THRESHOLD_PERCENT` empty, so every run with usable data can generate a digest.

Example local customization:

```powershell
$env:WATCHLIST = "USD,EUR,JPY,SGD"
$env:DISCREPANCY_THRESHOLD_PERCENT = "1.5"
$env:CONVERT_AMOUNTS_VND = "1000000,10000000"
python currency_rate_emailer.py generate
```

## Generated and state files

| File | Purpose |
| --- | --- |
| `email_body.txt` | Generated plain-text email body |
| `email_body.html` | Generated HTML email body |
| `email_subject.txt` | Custom subject used for an all-sources-failed alert |
| `last_rates.json` | Most recently stored market rates |
| `rate_history.csv` | Historical market rates used for weekly trends |

When the alert threshold is enabled and no significant change is found, `email_body.txt` is emptied and the `send` command exits without sending anything.

## Failure behavior

- If one or more sources fail, the email identifies the failed sources and includes data from those that succeeded.
- If all five sources fail, the script creates a dedicated failure alert and does not overwrite the rate cache or history.
- If the Gmail environment variables are missing, the `send` command exits with an error without attempting SMTP delivery, so automation cannot report a false success.
- Network requests use retries and backoff for rate limits and common transient server errors.

## Security

Credentials are read only from environment variables or GitHub Actions secrets. Do not place a Gmail password or App Password directly in the Python file, workflow, state files, or commit history.
