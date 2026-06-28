# 🔔 TradeReversal — Signal Scanner on Render

Intraday reversal signal scanner with a live Flask dashboard.
**Zero redeploys on data updates.** Scanner POSTs signals to the web service via HTTP.

## Architecture

```
Render Cron Job (every 2 mins, Mon-Fri 9AM-6PM EST)
        ↓
  scanner.py detects signals
        ↓
  POST /api/ingest → tradereversal-web (with secret key)
        ↓
  Flask stores signals in SQLite on /data (persistent disk)
        ↓
  Dashboard reads same disk on every page load ✅
```

## Conditions Checked

| Condition | Threshold |
|---|---|
| VWAP Deviation | ≥ 2% from session VWAP |
| RSI (14) | ≤ 35 oversold (LONG) / ≥ 65 overbought (SHORT) |
| RSI Divergence | Bullish (LONG) / Bearish (SHORT) |
| Volume vs Avg | ≥ 1.2× 20-bar average |
| Signal Strength | 1–3 confluence score |

## Project Structure

```
TradeReversal/
├── app.py                  # Flask: dashboard + /api/ingest endpoint
├── scanner.py              # Cron Job: scans + POSTs to /api/ingest
├── requirements.txt
├── render.yaml             # Render Blueprint — creates both services
├── custom_tickers.txt      # Add extra tickers here
└── templates/
    └── dashboard.html      # Live dashboard UI
```

## Deploy to Render

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "init TradeReversal"
git remote add origin https://github.com/YOUR_USERNAME/TradeReversal.git
git push -u origin main
```

### 2. Create Render Blueprint
1. Go to https://render.com → **New + → Blueprint**
2. Connect your **TradeReversal** GitHub repo
3. Render reads `render.yaml` → creates 2 services:
   - `tradereversal-web` — Flask dashboard + ingest API (with /data disk)
   - `tradereversal-scanner` — Cron Job (no disk — POSTs to web service)
4. `INGEST_SECRET` is auto-generated and shared between both services

### 3. Add Email Secrets
On Render → **tradereversal-scanner** → Environment:

| Key | Value |
|---|---|
| `EMAIL_SENDER` | your Gmail |
| `EMAIL_PASSWORD` | Gmail App Password |
| `EMAIL_RECEIVER` | alert destination email |

### 4. Add Custom Tickers (optional)
Edit `custom_tickers.txt`, one ticker per line.
Or set `EXTRA_TICKERS` env var on the scanner: `NFLX,AMD,PLTR`

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Live dashboard |
| `/api/ingest` | POST | Receive signals from scanner (secret required) |
| `/api/signals` | GET | JSON signal data |
| `/api/stats` | GET | JSON summary stats |
| `/health` | GET | Health check |

## Disclaimer
For informational purposes only. Not financial advice.
