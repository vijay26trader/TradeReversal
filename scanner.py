#!/usr/bin/env python3
"""
TradeReversal — Signal Scanner
Conditions: VWAP Deviation · RSI Divergence · Volume vs Avg · Signal Strength
Runs as a Render Cron Job every 2 minutes, Mon-Fri 9AM-6PM EST.
Writes to SQLite on Render persistent disk — Flask reads same disk, zero redeploys.
"""

import os
import sqlite3
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo
from contextlib import contextmanager

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

DB_FILE            = os.environ.get("DB_PATH", "data/signals.db")
EXTRA_TICKERS_ENV  = os.environ.get("EXTRA_TICKERS", "")
EXTRA_TICKERS_FILE = "custom_tickers.txt"

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT") or "587")

# ── Signal Thresholds ─────────────────────────────────────────────────────────
VWAP_DEV_THRESHOLD = 0.02   # 2% deviation from VWAP
RSI_PERIOD         = 14
RSI_OVERSOLD       = 35     # below = potential LONG
RSI_OVERBOUGHT     = 65     # above = potential SHORT
VOLUME_MULTIPLIER  = 1.2    # volume must be 1.2x the 20-bar avg
DIVERGENCE_WINDOW  = 5      # bars to look back for divergence
MIN_STRENGTH       = 1      # minimum confluence score to record (1-3)

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT    NOT NULL,
                scan_date     TEXT    NOT NULL,
                symbol        TEXT    NOT NULL,
                timeframe     TEXT    NOT NULL DEFAULT '15m',
                price         REAL    NOT NULL,
                vwap          REAL    NOT NULL,
                pct_from_vwap REAL    NOT NULL,
                rsi           REAL    NOT NULL,
                divergence    TEXT    NOT NULL,
                volume        INTEGER NOT NULL,
                vol_avg       INTEGER NOT NULL,
                vol_ratio     REAL    NOT NULL,
                signal        TEXT    NOT NULL,
                strength      INTEGER NOT NULL,
                stop_loss     REAL    NOT NULL,
                target        REAL    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'New'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date   ON signals(scan_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON signals(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signal ON signals(signal)")
        conn.commit()
    log.info(f"DB ready → {DB_FILE}")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def is_duplicate(conn, symbol: str, signal: str, window_start: str) -> bool:
    row = conn.execute("""
        SELECT id FROM signals
        WHERE symbol=? AND signal=? AND timestamp>=?
        LIMIT 1
    """, (symbol, signal, window_start)).fetchone()
    return row is not None


def insert_signal(conn, r: dict):
    conn.execute("""
        INSERT INTO signals
            (timestamp, scan_date, symbol, timeframe, price, vwap, pct_from_vwap,
             rsi, divergence, volume, vol_avg, vol_ratio,
             signal, strength, stop_loss, target, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        r["timestamp"], r["scan_date"], r["symbol"], r["timeframe"],
        r["price"], r["vwap"], r["pct_from_vwap"],
        r["rsi"], r["divergence"], r["volume"], r["vol_avg"], r["vol_ratio"],
        r["signal"], r["strength"], r["stop_loss"], r["target"], r["status"]
    ))
    conn.commit()

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
    """
    Bullish: price makes lower low, RSI makes higher low  → reversal UP
    Bearish: price makes higher high, RSI makes lower high → reversal DOWN
    """
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
            log.warning(f"{symbol} [{interval}]: insufficient data")
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

        # ── Conditions ────────────────────────────────────────────────────
        vwap_ok   = abs(pct_from_vwap) >= VWAP_DEV_THRESHOLD
        volume_ok = volume >= vol_avg * VOLUME_MULTIPLIER
        div_ok    = divergence != "None"

        long_signal  = (pct_from_vwap <= -VWAP_DEV_THRESHOLD and
                        rsi_val <= RSI_OVERSOLD and
                        divergence == "Bullish")

        short_signal = (pct_from_vwap >= VWAP_DEV_THRESHOLD and
                        rsi_val >= RSI_OVERBOUGHT and
                        divergence == "Bearish")

        if not (long_signal or short_signal):
            return None

        signal_type = "LONG" if long_signal else "SHORT"

        # ── Confluence Score (0-3) ─────────────────────────────────────────
        strength = sum([vwap_ok, volume_ok, div_ok])
        if strength < MIN_STRENGTH:
            return None

        # ── Risk Levels ───────────────────────────────────────────────────
        atr  = float((df["High"] - df["Low"]).iloc[-14:].mean())
        stop = round(price - atr if long_signal else price + atr, 2)
        now  = datetime.now(EST)

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
        log.error(f"{symbol} [{interval}]: {e}")
        return None

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(signals: list[dict]):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]) or not signals:
        return

    subject = f"🔔 TradeReversal: {len(signals)} Signal(s) — {datetime.now(EST).strftime('%H:%M EST')}"
    rows = ""
    for r in signals:
        color = "#3fb950" if r["signal"] == "LONG" else "#f85149"
        rows += f"""<tr>
          <td style="padding:7px;border-bottom:1px solid #30363d">{r['timestamp']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d"><b>{r['symbol']}</b></td>
          <td style="padding:7px;border-bottom:1px solid #30363d">{r['timeframe']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">${r['price']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">${r['vwap']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">{r['pct_from_vwap']:+.2f}%</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">{r['rsi']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">{r['divergence']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d;color:{color}"><b>{r['signal']}</b></td>
          <td style="padding:7px;border-bottom:1px solid #30363d">{"★" * r['strength']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">${r['stop_loss']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">${r['target']}</td>
        </tr>"""

    body = f"""<html><body style="font-family:Segoe UI,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px">
    <h2 style="color:#f0f6fc">🔔 TradeReversal Signal Alert</h2>
    <p style="color:#8b949e">{len(signals)} signal(s) detected · {datetime.now(EST).strftime('%Y-%m-%d %H:%M EST')}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:14px">
      <thead><tr style="background:#161b22">
        <th style="padding:7px;text-align:left;color:#8b949e">Time</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Symbol</th>
        <th style="padding:7px;text-align:left;color:#8b949e">TF</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Price</th>
        <th style="padding:7px;text-align:left;color:#8b949e">VWAP</th>
        <th style="padding:7px;text-align:left;color:#8b949e">% Dev</th>
        <th style="padding:7px;text-align:left;color:#8b949e">RSI</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Divergence</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Signal</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Strength</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Stop</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Target</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="margin-top:16px;font-size:11px;color:#8b949e">⚠️ Informational only. Not financial advice.</p>
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
    log.info(f"TradeReversal Scanner | DB → {DB_FILE}")

    init_db()

    if not is_market_hours():
        log.info(f"Outside market hours ({datetime.now(EST).strftime('%H:%M EST %A')}). Exiting.")
        return

    tickers      = load_tickers()
    window_start = (datetime.now(EST) - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S EST")
    new_signals  = []

    log.info(f"Scanning {len(tickers)} tickers on 15m: {', '.join(tickers)}")

    with get_db() as conn:
        for symbol in tickers:
            log.info(f"→ {symbol}")
            result = analyse_ticker(symbol, interval="15m")

            if not result:
                log.info(f"  — No signal")
                continue

            if is_duplicate(conn, symbol, result["signal"], window_start):
                log.info(f"  ⏭ Duplicate — skipped")
                continue

            insert_signal(conn, result)
            new_signals.append(result)
            log.info(
                f"  ✅ {result['signal']} | "
                f"VWAP dev={result['pct_from_vwap']:+.2f}% | "
                f"RSI={result['rsi']} | "
                f"Vol={result['vol_ratio']}x | "
                f"Div={result['divergence']} | "
                f"Strength={'★' * result['strength']}"
            )

    if new_signals:
        log.info(f"📧 Sending email — {len(new_signals)} signal(s)")
        send_email(new_signals)
    else:
        log.info("No new signals this run.")

    log.info("Scan complete.")


if __name__ == "__main__":
    main()
