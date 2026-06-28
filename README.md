# 🔔 TradeReversal — Signal Scanner

Intraday reversal signal scanner with a live Flask dashboard on Render.
**Zero redeploys on data updates** — scanner and dashboard share a persistent disk.

## Conditions Checked

| Condition | Threshold |
|---|---|
| VWAP Deviation | ≥ 2% from session VWAP |
| RSI (14) | ≤ 35 oversold (LONG) / ≥ 65 overbought (SHORT) |
| RSI Divergence | Bullish divergence (LONG) / Bearish divergence (SHORT) |
| Volume vs Avg | ≥ 1.2× the 20-bar average |
| Signal Strength | 1–3 (confluence of above conditions) |

## Architecture

```
Render Cron Job — every 2 mins, Mon-Fri 9AM-6PM EST
        ↓
  scanner.py writes → /data/signals.db (persistent disk)
        ↓
Flask Web Service reads same /data disk on every page load
        ↓
  Live dashboard — always current, zero redeploys ✅
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
3. Render reads `render.yaml` → creates 2 services automatically:
   - `tradereversal-web` — Flask dashboard (Web Service)
   - `tradereversal-scanner` — Scanner (Cron Job, every 2 mins)
4. Both share `reversal-storage` persistent disk at `/data`

### 3. Add Email Secrets
On Render → tradereversal-scanner → **Environment**:

| Key | Value |
|---|---|
| `EMAIL_SENDER` | your Gmail |
| `EMAIL_PASSWORD` | Gmail App Password |
| `EMAIL_RECEIVER` | alert destination email |

### 4. Add Custom Tickers (optional)
Edit `custom_tickers.txt`, one ticker per line. Or set `EXTRA_TICKERS` env var: `NFLX,AMD,PLTR`

## Project Structure

```
TradeReversal/
├── app.py                  # Flask web server
├── scanner.py              # Signal scanner (Render Cron Job)
├── requirements.txt
├── render.yaml             # Render Blueprint config
├── custom_tickers.txt      # Extra tickers
└── templates/
    └── dashboard.html      # Live dashboard UI
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `/` | Live dashboard |
| `/api/signals?days=7&signal=LONG&strength=3` | JSON signals |
| `/api/stats` | JSON summary stats |
| `/health` | Health check |

## Disclaimer
For informational purposes only. Not financial advice.
