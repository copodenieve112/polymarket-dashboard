import warnings
warnings.filterwarnings("ignore")

import json
import time
from datetime import datetime
from pathlib import Path
from typing import List

import streamlit as st
import streamlit.components.v1 as components

from clock import now_utc
from data_feed import fetch_markets
from engine import DemoEngine, INITIAL_CAP
from models import Market
from strategy import ENTRY_WINDOW, SIGNAL_THRESHOLD, KELLY_FRACTION, MAX_RISK_PCT

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="polybot · dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
  body { background: #0d0b08 !important; }
  .stApp { background: #0d0b08; }
  .block-container { padding: 0 !important; max-width: 100% !important; }
  #MainMenu, footer, header { visibility: hidden; }
  .stDeployButton { display: none; }
  iframe { border: none !important; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "engine" not in st.session_state:
    st.session_state.engine = DemoEngine()
if "price_history" not in st.session_state:
    st.session_state.price_history = {}
if "prev_updated_at" not in st.session_state:
    st.session_state.prev_updated_at = {}

TEMPLATE_PATH = Path(__file__).parent / "dashboard_template.html"
MAX_HISTORY   = 30


def update_price_history(markets: List[Market]):
    for m in markets:
        key  = f"{m.coin}_{m.window_label}"
        hist = st.session_state.price_history.get(key, [])
        hist.append(round(m.price_yes, 4))
        st.session_state.price_history[key] = hist[-MAX_HISTORY:]


def build_js_data(engine: DemoEngine, markets: List[Market]) -> tuple:
    portfolio      = engine.portfolio
    market_map_q   = {m.question: m for m in markets}
    now_utc_dt     = now_utc()
    now_date       = now_utc_dt.date().isoformat()
    open_questions = {t.question for t in portfolio.open_trades}

    # Closed trades — full fields for trade log
    trades_data = []
    for t in portfolio.closed_trades:
        resolved = t.resolved_at or t.timestamp
        try:
            ts_ms = int(datetime.fromisoformat(resolved).timestamp() * 1000)
        except Exception:
            ts_ms = int(now_utc_dt.timestamp() * 1000)
        try:
            entry_ts = int(datetime.fromisoformat(t.timestamp).timestamp() * 1000)
        except Exception:
            entry_ts = ts_ms
        cost = t.entry_price * t.shares
        trades_data.append({
            "id":       t.id,
            "asset":    t.coin,
            "tf":       t.window,
            "dir":      "UP" if t.direction == "YES" else "DOWN",
            "stake":    round(cost, 2),
            "shares":   t.shares,
            "won":      t.status == "won",
            "payout":   t.pnl,
            "edge":     round(t.net_edge_est / max(cost, 1), 3),
            "sbc":      30,
            "ts":       ts_ms,       # resolution / exit time
            "entry_ts": entry_ts,    # open time
            "entry_p":  t.entry_price,
            "exit_p":   t.exit_price,
            "status":   t.status,
        })

    # Open positions
    live_data = []
    for i, t in enumerate(portfolio.open_trades):
        try:
            end_time  = datetime.fromisoformat(t.end_time_iso)
            secs_left = max(0, int((end_time - now_utc_dt).total_seconds()))
        except Exception:
            secs_left = 0
        m         = market_map_q.get(t.question)
        cur_odds  = (m.price_yes if t.direction == "YES" else m.price_no) if m else t.entry_price
        unrealized = round((cur_odds - t.entry_price) * t.shares, 2)
        live_data.append({
            "id":            i,
            "asset":         t.coin,
            "tf":            t.window,
            "dir":           "UP" if t.direction == "YES" else "DOWN",
            "stake":         round(t.entry_price * t.shares, 2),
            "secsLeft":      secs_left,
            "curOdds":       round(cur_odds, 3),
            "entOdds":       t.entry_price,
            "unrealizedPnl": unrealized,
        })

    # All markets for the grid
    now_ms = int(now_utc_dt.timestamp() * 1000)
    markets_data = []
    for m in markets:
        key       = f"{m.coin}_{m.window_label}"
        sparkline = st.session_state.price_history.get(key, [m.price_yes])

        # Stale detection: warn if API's updatedAt hasn't changed since last cycle
        prev_upd = st.session_state.prev_updated_at.get(key, 0)
        if prev_upd and prev_upd == m.updated_at_ms and m.has_real_price:
            age_s = (now_ms - m.updated_at_ms) / 1000
            if age_s > 60:
                print(f"[WARN] {m.coin} {m.window_label}: datos sin cambio por {age_s:.0f}s")
        st.session_state.prev_updated_at[key] = m.updated_at_ms

        markets_data.append({
            "coin":            m.coin,
            "tf":              m.window_label,
            "price_yes":       round(m.price_yes, 4),
            "price_no":        round(m.price_no, 4),
            "time_left_secs":  int(max(0, m.time_left_seconds)),
            "time_left_str":   m.time_left,
            "has_real_price":  m.has_real_price,
            "last_fetched_ms": m.last_fetched_ms,
            "updated_at_ms":   m.updated_at_ms,
            "sparkline":       sparkline,
            "active_bet":      m.question in open_questions,
            "volume":          round(m.volume, 0),
            "in_entry_window": m.time_left_seconds <= ENTRY_WINDOW.get(m.window_label, (20, 90))[1],
        })

    # KPI
    total_pnl   = portfolio.total_pnl
    today_pnl   = sum(
        t.pnl for t in portfolio.closed_trades
        if (t.resolved_at or t.timestamp or "")[:10] == now_date
    )
    total_stake = sum(t.entry_price * t.shares for t in portfolio.closed_trades)
    wins        = sum(1 for t in portfolio.closed_trades if t.status == "won")
    n           = len(portfolio.closed_trades)
    win_rate    = (wins / n * 100) if n else 0.0
    roi         = (total_pnl / total_stake * 100) if total_stake else 0.0
    n_fresh     = sum(1 for m in markets if m.has_real_price)

    kpi = {
        "total_pnl":        round(total_pnl, 2),
        "today_pnl":        round(today_pnl, 2),
        "win_rate":         round(win_rate, 1),
        "roi":              round(roi, 1),
        "avg_sbc":           ENTRY_WINDOW["5m"][1],
        "wins":              wins,
        "capital_initial":   INITIAL_CAP,
        "capital_current":   round(portfolio.current_capital, 2),
        "n_markets":         len(markets),
        "n_fresh":           n_fresh,
        "fetch_ts":          int(now_utc_dt.timestamp() * 1000),
        "entry_window_5m":   ENTRY_WINDOW["5m"],
        "entry_window_15m":  ENTRY_WINDOW["15m"],
        "entry_window_1h":   ENTRY_WINDOW["1h"],
        "signal_threshold":  SIGNAL_THRESHOLD,
        "kelly_fraction":    KELLY_FRACTION,
        "max_risk_pct":      MAX_RISK_PCT,
    }

    return trades_data, live_data, kpi, markets_data


def render_dashboard(engine: DemoEngine, markets: List[Market]):
    trades_data, live_data, kpi, markets_data = build_js_data(engine, markets)

    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = html.replace("__ASSETS__",  json.dumps(["BTC","ETH","SOL","XRP","BNB","DOGE","HYPE"]))
    html = html.replace("__TRADES__",  json.dumps(trades_data))
    html = html.replace("__LIVE__",    json.dumps(live_data))
    html = html.replace("__KPI__",     json.dumps(kpi))
    html = html.replace("__MARKETS__", json.dumps(markets_data))

    components.html(html, height=2100, scrolling=True)


# ── Sidebar ───────────────────────────────────────────────────────────────────
engine = st.session_state.engine
p      = engine.portfolio

with st.sidebar:
    st.markdown("### polybot")
    if st.button("Reiniciar Demo", use_container_width=True):
        engine.reset()
        st.session_state.engine        = engine
        st.session_state.price_history = {}
        st.rerun()

    st.caption(f"Inicio: {p.started_at[11:19] if p.started_at else '—'} UTC")
    st.caption(f"Trades: {len(p.trades)} / 50")
    st.caption(f"Runtime: {p.runtime_hours:.1f}h / 24h")
    st.caption(f"Capital: ${p.current_capital:.2f}")
    st.caption(f"Señal: |score|≥{SIGNAL_THRESHOLD} | Kelly {KELLY_FRACTION}× | cap {MAX_RISK_PCT*100:.0f}%")

    if p.is_demo_finished:
        reason = "50 trades" if len(p.trades) >= 50 else "24h"
        st.warning(f"Demo finalizado ({reason})")

    # Recent trade log in sidebar
    closed = p.closed_trades
    if closed:
        st.divider()
        st.caption(f"**Últimos trades** ({len(closed)} total)")
        for t in list(reversed(closed))[:10]:
            sign = "+" if t.pnl >= 0 else ""
            icon = "✓" if t.status == "won" else "✗"
            st.caption(f"{icon} {t.coin} {t.window} {t.direction} {sign}${t.pnl:.2f}")

# ── Main loop ─────────────────────────────────────────────────────────────────
markets = fetch_markets()
update_price_history(markets)

if not p.is_demo_finished:
    engine.run_cycle(markets)

render_dashboard(engine, markets)

time.sleep(5)
st.rerun()
