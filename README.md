# Currency Exchange Rates -> VND -> Email (runs on GitHub Actions, no local computer needed)

This repo emails you daily exchange rates for a watchlist of currencies
(USD, EUR, JPY, CNY, KRW, GBP, SGD, AUD by default) converted to VND,
automatically, using GitHub's free scheduled-workflow runners. Nothing
needs to run on your own machine.

Each email shows a table of "1 unit = X VND" for every currency on your
watchlist, plus the % change since the last run.

## Where the data comes from

Rates come from <https://www.exchangerate-api.com/> (via the free
`open.er-api.com` endpoint, no API key required).

## One-time setup (~5 minutes)

1. **Create a GitHub account** if you don't have one: <https://github.com/join>

2. **Create a new repository**
   - Click "+" (top right) -> "New repository"
   - Name it anything, e.g. `currency-rate-emailer`
   - Set it to **Private** (recommended, keeps your workflow config private)
   - Click "Create repository"

3. **Upload these files** to the repo (drag-and-drop works fine via the
   GitHub web UI: "Add file" -> "Upload files"), keeping the folder structure:
   - `currency_rate_emailer.py`
   - `requirements.txt`
   - `.github/workflows/send-currency-rate.yml`

4. **Create a Gmail App Password** (your normal Gmail password won't work):
   - Turn on 2-Step Verification: <https://myaccount.google.com/signinoptions/two-step-verification>
   - Then create an app password: <https://myaccount.google.com/apppasswords>
   - Choose "Mail" as the app, copy the 16-character password it gives you.

5. **Add your secrets to the repo** (keeps your email/password out of the code):
   - In your repo: Settings -> Secrets and variables -> Actions -> "New repository secret"
   - Add three secrets:
     * `GMAIL_ADDRESS` = your Gmail address
     * `GMAIL_APP_PASSWORD` = the 16-character app password from step 4
     * `CURRENCY_RECIPIENT` = the email address that should receive the update

6. **Test it manually**
   - Go to the "Actions" tab in your repo
   - Click "Send Currency Rate" on the left
   - Click "Run workflow" -> "Run workflow" (green button)
   - Wait ~10-15 seconds, refresh, click into the run to see logs / confirm success
   - Check the recipient inbox for the email

That's it — from now on it runs automatically on the schedule below, with
no computer of yours needing to be on.

## Changing the schedule

Open `.github/workflows/send-currency-rate.yml` and edit this line:

```
- cron: "0 1 * * *"
```

Cron format is `minute hour day month weekday`, always in **UTC**. Examples:

- `0 1 * * *` -> once a day at 08:00 Vietnam time (current setting)
- `0 */6 * * *` -> every 6 hours
- `0 1,9 * * *` -> twice a day (8am and 4pm Vietnam time)

A handy converter: <https://crontab.guru>

## Changing the watchlist

Edit the `WATCHLIST` environment variable in the workflow file, or the
`DEFAULT_WATCHLIST` list near the top of `currency_rate_emailer.py`. Use
any 3-letter currency codes supported by exchangerate-api.com.

## Only emailing on rate changes

By default every scheduled run sends an email regardless of movement. To
only get emailed when a rate moves meaningfully, set the
`ALERT_THRESHOLD_PERCENT` environment variable in the workflow's
"Generate email" step, e.g. `"1.5"` to only send when some currency has
moved 1.5% or more since the last run.

## Running locally instead

```
pip install -r requirements.txt
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export CURRENCY_RECIPIENT="you@gmail.com"
python currency_rate_emailer.py generate
python currency_rate_emailer.py send
```

Schedule it yourself with cron (`crontab -e`):

```
0 8 * * * cd /path/to/currency-rate-emailer && /usr/bin/python3 currency_rate_emailer.py generate && /usr/bin/python3 currency_rate_emailer.py send >> currency_emailer.log 2>&1
```

## Notes

- GitHub Actions free tier includes 2,000 minutes/month for private repos —
  this job takes a few seconds a run, so it's effectively free even at a
  frequent cadence.
- You can also trigger it manually anytime via the "Run workflow" button.
- If the run fails, check the Actions tab -> the failed run -> logs. Common
  causes: a secret is missing/misspelled, or the Gmail app password was
  revoked.
