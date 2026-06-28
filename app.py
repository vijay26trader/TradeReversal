#!/usr/bin/env python3
"""
TradeReversal — Flask Dashboard + Ingest API
Receives signals from the scanner cron job via POST /api/ingest.
Stores them in SQLite on the persistent disk.
Serves the live dashboard — zero redeploys on data updates.
"""

import os
import hmac
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, jsonify, request, abort

app = Flask(__name__)

EST           = ZoneInfo("America/New_York")
DB_FILE       = os.environ.get("DB_PATH", "data/signals.db")
INGEST_SECRET = os.environ.get("INGEST_SECRET", "")

# ── DB Setup ──────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    conn = get_db()
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
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def db_exists() -> bool:
    return os.path.exists(DB_FILE)

# ── DB Queries ────────────────────────────────────────────────────────────────

def fetch_signals(days=30, symbol="", signal="", strength="", timeframe="") -> list[dict]:
    if not db_exists():
        return []
    since  = (datetime.now(EST) - timedelta(days=days)).strftime("%Y-%m-%d")
    query  = "SELECT * FROM signals WHERE scan_date >= ?"
    params = [since]
    if symbol:
        query += " AND symbol=?";    params.append(symbol.upper())
    if signal:
        query += " AND signal=?";    params.append(signal.upper())
    if strength:
        query += " AND strength=?";  params.append(int(strength))
    if timeframe:
        query += " AND timeframe=?"; params.append(timeframe)
    query += " ORDER BY id DESC LIMIT 1000"
    conn  = get_db()
    rows  = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_stats() -> dict:
    empty = {
        "today": 0, "total": 0, "long_today": 0, "short_today": 0,
        "high_strength": 0, "top_symbol": "—", "last_scan": "No scans yet"
    }
    if not db_exists():
        return empty
    today = datetime.now(EST).strftime("%Y-%m-%d")
    conn  = get_db()

    def q(sql, p=()):
        return conn.execute(sql, p).fetchone()[0]

    stats = {
        "today":         q("SELECT COUNT(*) FROM signals WHERE scan_date=?", (today,)),
        "total":         q("SELECT COUNT(*) FROM signals"),
        "long_today":    q("SELECT COUNT(*) FROM signals WHERE signal='LONG'  AND scan_date=?", (today,)),
        "short_today":   q("SELECT COUNT(*) FROM signals WHERE signal='SHORT' AND scan_date=?", (today,)),
        "high_strength": q("SELECT COUNT(*) FROM signals WHERE strength=3 AND scan_date=?", (today,)),
    }
    row = conn.execute("""
        SELECT symbol, COUNT(*) cnt FROM signals
        WHERE scan_date >= date('now','-7 days')
        GROUP BY symbol ORDER BY cnt DESC LIMIT 1
    """).fetchone()
    stats["top_symbol"] = f"{row['symbol']} ({row['cnt']})" if row else "—"

    last = conn.execute("SELECT timestamp FROM signals ORDER BY id DESC LIMIT 1").fetchone()
    stats["last_scan"] = last["timestamp"] if last else "No scans yet"
    conn.close()
    return stats


def fetch_symbols() -> list[str]:
    if not db_exists():
        return []
    conn  = get_db()
    rows  = conn.execute("SELECT DISTINCT symbol FROM signals ORDER BY symbol").fetchall()
    conn.close()
    return [r["symbol"] for r in rows]

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    symbol    = request.args.get("symbol", "")
    signal    = request.args.get("signal", "")
    strength  = request.args.get("strength", "")
    timeframe = request.args.get("timeframe", "")
    days      = int(request.args.get("days", 30))

    return render_template("dashboard.html",
        results  = fetch_signals(days, symbol, signal, strength, timeframe),
        stats    = fetch_stats(),
        symbols  = fetch_symbols(),
        now      = datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST"),
        filters  = {
            "symbol": symbol, "signal": signal,
            "strength": strength, "timeframe": timeframe, "days": days
        }
    )


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Receive signals from the scanner cron job and save to SQLite."""
    # Verify secret
    token = request.headers.get("X-Ingest-Secret", "")
    if not INGEST_SECRET or not hmac.compare_digest(token, INGEST_SECRET):
        abort(403)

    signals = request.get_json(silent=True)
    if not signals or not isinstance(signals, list):
        abort(400)

    conn     = get_db()
    inserted = 0
    window   = (datetime.now(EST) - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S EST")

    for r in signals:
        # Duplicate check within 3-min window
        exists = conn.execute("""
            SELECT id FROM signals
            WHERE symbol=? AND signal=? AND timestamp>=?
            LIMIT 1
        """, (r["symbol"], r["signal"], window)).fetchone()
        if exists:
            continue

        conn.execute("""
            INSERT INTO signals
                (timestamp, scan_date, symbol, timeframe,
                 price, vwap, pct_from_vwap, rsi, divergence,
                 volume, vol_avg, vol_ratio,
                 signal, strength, stop_loss, target, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            r["timestamp"], r["scan_date"], r["symbol"], r.get("timeframe", "15m"),
            r["price"], r["vwap"], r["pct_from_vwap"], r["rsi"], r["divergence"],
            r["volume"], r["vol_avg"], r["vol_ratio"],
            r["signal"], r["strength"], r["stop_loss"], r["target"],
            r.get("status", "New")
        ))
        inserted += 1

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "inserted": inserted, "received": len(signals)})


@app.route("/api/signals")
def api_signals():
    days = int(request.args.get("days", 7))
    data = fetch_signals(
        days,
        request.args.get("symbol", ""),
        request.args.get("signal", ""),
        request.args.get("strength", ""),
        request.args.get("timeframe", "")
    )
    return jsonify({"count": len(data), "signals": data})


@app.route("/api/stats")
def api_stats():
    return jsonify(fetch_stats())


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "db":     db_exists(),
        "time":   datetime.now(EST).isoformat()
    })


# ── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
