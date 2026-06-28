#!/usr/bin/env python3
"""
TradeReversal — Signal Scanner (Render Cron Job)
Conditions: VWAP Deviation · RSI Divergence · Volume vs Avg · Signal Strength
Runs every 2 minutes Mon-Fri 9AM-6PM EST.
POSTs signals to the web service /api/ingest endpoint — no disk access needed.
"""

import os
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
import pandas as pd
import numpy as np

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
EST          = ZoneInfo("America/New_York")
MARKET_OPEN  = 9
MARKET_CLOSE = 18

DEFAULT_TICKERS = [
    "SPY", "AAPL", "MSFT", "NVDA", "AMZN",
    "GOOGL", "META", "TSLA", "JPM", "V"
]

EXTRA_TICKERS_ENV  = os.environ.get("EXTRA_TICKERS", "")
EXTRA_TICKERS_FILE = "custom_tickers.txt"

INGEST_URL    = os.environ.get("INGEST_URL", "")
INGEST_SECRET = os.environ.get("INGEST_SECRET", "")

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT") or "587")

# ── Signal Thresholds ─────────────────────────────────────────────────────────
VWAP_DEV_THRESHOLD = 0.02
RSI_PERIOD         = 14
RSI_OVERSOLD       = 35
RSI_OVERBOUGHT     = 65
VOLUME_MULTIPLIER  = 1.2
DIVERGENCE_WINDOW  = 5

# ── Market Helpers ────────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    now = datetime.now(EST)
    return now.weekday() < 5 and MARKET_OPEN <= now.hour < MARKET_CLOSE


def load_tickers() -> list[str]:
    tickers = list(DEFAULT_TICKERS)
    if EXTRA_TICKERS_ENV:
        tickers += [t.strip().upper() for t in EXTRA_TICKERS_ENV.split(",") if t.strip()]
    if os.path.exists(EXTRA_TICKERS_FILE):
        with open(EXTRA_TICKERS_FILE) as f:
            tickers += [t.strip().upper() for t in f if t.strip() and not t.startswith("#")]
    return list(dict.fromkeys(tickers))

# ── Indicators ────────────────────────────────────────────────────────────────

def compute_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    df         = df.copy()
    df["date"] = df.index.date
    typical    = (df["High"] + df["Low"] + df["Close"]) / 3
    df["tpv"]  = typical * df["Volume"]
    parts      = []
    for _, grp in df.groupby("date"):
        parts.append(grp["tpv"].cumsum() / grp["Volume"].cumsum())
    return pd.concat(parts).reindex(df.index)


def detect_divergence(price: pd.Series, rsi: pd.Series, window: int = DIVERGENCE_WINDOW) -> str:
    if len(price) < window + 1:
        return "None"
    p = price.iloc[-window:]
    r = rsi.iloc[-window:]
    if p.iloc[-1] <= p.min() and r.iloc[-1] > r.min():
        return "Bullish"
    if p.iloc[-1] >= p.max() and r.iloc[-1] < r.max():
        return "Bearish"
    return "None"

# ── Core Scanner ──────────────────────────────────────────────────────────────

def analyse_ticker(symbol: str, interval: str = "15m") -> dict | None:
    try:
        df = yf.download(
            symbol, period="5d", interval=interval,
            progress=False, auto_adjust=True
        )
        if df.empty or len(df) < RSI_PERIOD + DIVERGENCE_WINDOW + 5:
            log.warning(f"{symbol}: insufficient data")
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.index   = pd.to_datetime(df.index)
        df["VWAP"] = compute_vwap(df)
        df["RSI"]  = compute_rsi(df["Close"])

        last          = df.iloc[-1]
        price         = float(last["Close"])
        vwap_val      = float(last["VWAP"])
        rsi_val       = float(last["RSI"])
        volume        = float(last["Volume"])
        vol_avg       = float(df["Volume"].iloc[-20:].mean())
        pct_from_vwap = (price - vwap_val) / vwap_val
        divergence    = detect_divergence(df["Close"], df["RSI"])

        vwap_ok   = abs(pct_from_vwap) >= VWAP_DEV_THRESHOLD
        volume_ok = volume >= vol_avg * VOLUME_MULTIPLIER
        div_ok    = divergence != "None"

        long_signal  = (pct_from_vwap <= -VWAP_DEV_THRESHOLD and
                        rsi_val <= RSI_OVERSOLD and
                        divergence == "Bullish")
        short_signal = (pct_from_vwap >=  VWAP_DEV_THRESHOLD and
                        rsi_val >= RSI_OVERBOUGHT and
                        divergence == "Bearish")

        if not (long_signal or short_signal):
            return None

        signal_type = "LONG" if long_signal else "SHORT"
        strength    = sum([vwap_ok, volume_ok, div_ok])
        atr         = float((df["High"] - df["Low"]).iloc[-14:].mean())
        stop        = round(price - atr if long_signal else price + atr, 2)
        now         = datetime.now(EST)

        return {
            "timestamp":      now.strftime("%Y-%m-%d %H:%M:%S EST"),
            "scan_date":      now.strftime("%Y-%m-%d"),
            "symbol":         symbol,
            "timeframe":      interval,
            "price":          round(price, 2),
            "vwap":           round(vwap_val, 2),
            "pct_from_vwap":  round(pct_from_vwap * 100, 2),
            "rsi":            round(rsi_val, 1),
            "divergence":     divergence,
            "volume":         int(volume),
            "vol_avg":        int(vol_avg),
            "vol_ratio":      round(volume / vol_avg, 2),
            "signal":         signal_type,
            "strength":       strength,
            "stop_loss":      stop,
            "target":         round(vwap_val, 2),
            "status":         "New",
        }

    except Exception as e:
        log.error(f"{symbol}: {e}")
        return None

# ── Ingest ────────────────────────────────────────────────────────────────────

def post_signals(signals: list[dict]):
    """POST signals to the Flask web service for storage."""
    if not signals:
        return
    if not INGEST_URL:
        log.warning("INGEST_URL not set — skipping ingest")
        return
    try:
        resp = requests.post(
            INGEST_URL,
            json=signals,
            headers={"X-Ingest-Secret": INGEST_SECRET},
            timeout=30
        )
        resp.raise_for_status()
        log.info(f"Ingest OK: {resp.json()}")
    except Exception as e:
        log.error(f"Ingest failed: {e}")

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(signals: list[dict]):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]) or not signals:
        return

    subject = f"🔔 TradeReversal: {len(signals)} Signal(s) — {datetime.now(EST).strftime('%H:%M EST')}"
    rows = ""
    for r in signals:
        color = "#34d399" if r["signal"] == "LONG" else "#f87171"
        rows += f"""<tr>
          <td style="padding:7px;border-bottom:1px solid #1e2340">{r['timestamp']}</td>
          <td style="padding:7px;border-bottom:1px solid #1e2340"><b>{r['symbol']}</b></td>
          <td style="padding:7px;border-bottom:1px solid #1e2340">{r['timeframe']}</td>
          <td style="padding:7px;border-bottom:1px solid #1e2340">${r['price']}</td>
          <td style="padding:7px;border-bottom:1px solid #1e2340">${r['vwap']}</td>
          <td style="padding:7px;border-bottom:1px solid #1e2340">{r['pct_from_vwap']:+.2f}%</td>
          <td style="padding:7px;border-bottom:1px solid #1e2340">{r['rsi']}</td>
          <td style="padding:7px;border-bottom:1px solid #1e2340">{r['divergence']}</td>
          <td style="padding:7px;border-bottom:1px solid #1e2340;color:{color}"><b>{r['signal']}</b></td>
          <td style="padding:7px;border-bottom:1px solid #1e2340">{"★" * r['strength']}</td>
          <td style="padding:7px;border-bottom:1px solid #1e2340">${r['stop_loss']}</td>
          <td style="padding:7px;border-bottom:1px solid #1e2340">${r['target']}</td>
        </tr>"""

    body = f"""<html><body style="font-family:Segoe UI,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px">
    <h2 style="color:#f0f6fc">🔔 TradeReversal Signal Alert</h2>
    <p style="color:#8b949e">{len(signals)} signal(s) · {datetime.now(EST).strftime('%Y-%m-%d %H:%M EST')}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:14px">
      <thead><tr style="background:#0f1320">
        <th style="padding:7px;text-align:left;color:#6b7280">Time</th>
        <th style="padding:7px;text-align:left;color:#6b7280">Symbol</th>
        <th style="padding:7px;text-align:left;color:#6b7280">TF</th>
        <th style="padding:7px;text-align:left;color:#6b7280">Price</th>
        <th style="padding:7px;text-align:left;color:#6b7280">VWAP</th>
        <th style="padding:7px;text-align:left;color:#6b7280">% Dev</th>
        <th style="padding:7px;text-align:left;color:#6b7280">RSI</th>
        <th style="padding:7px;text-align:left;color:#6b7280">Divergence</th>
        <th style="padding:7px;text-align:left;color:#6b7280">Signal</th>
        <th style="padding:7px;text-align:left;color:#6b7280">Strength</th>
        <th style="padding:7px;text-align:left;color:#6b7280">Stop</th>
        <th style="padding:7px;text-align:left;color:#6b7280">Target</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="margin-top:16px;font-size:11px;color:#6b7280">⚠️ Informational only. Not financial advice.</p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        log.info(f"Email sent → {EMAIL_RECEIVER}")
    except Exception as e:
        log.error(f"Email failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("TradeReversal Scanner starting")

    if not is_market_hours():
        log.info(f"Outside market hours ({datetime.now(EST).strftime('%H:%M EST %A')}). Exiting.")
        return

    tickers     = load_tickers()
    new_signals = []

    log.info(f"Scanning {len(tickers)} tickers: {', '.join(tickers)}")

    for symbol in tickers:
        log.info(f"→ {symbol}")
        result = analyse_ticker(symbol, interval="15m")
        if not result:
            log.info("  — No signal")
            continue
        new_signals.append(result)
        log.info(
            f"  ✅ {result['signal']} | "
            f"VWAP dev={result['pct_from_vwap']:+.2f}% | "
            f"RSI={result['rsi']} | "
            f"Vol={result['vol_ratio']}x | "
            f"Div={result['divergence']} | "
            f"Strength={'★' * result['strength']}"
        )

    post_signals(new_signals)

    if new_signals:
        send_email(new_signals)

    log.info(f"Scan complete — {len(new_signals)} signal(s) sent.")


if __name__ == "__main__":
    main()
