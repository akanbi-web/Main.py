import os
import smtplib
import time
from datetime import datetime, time as dtime, timedelta, timezone
from email.message import EmailMessage

import pandas as pd
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="JS Forex Sentinel", layout="wide")

DEFAULT_PAIRS = [
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "GC=F",
    "BTC-USD",
    "^DJI",
    "^NDX",
    "CL=F",
]
AVAILABLE_PAIRS = list(DEFAULT_PAIRS)

PAIR_LABELS = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "USDJPY=X": "USDJPY",
    "GC=F":     "XAUUSD (Gold)",
    "BTC-USD":  "BTC-USD",
    "^DJI":     "US30 (DJI)",
    "^NDX":     "Nasdaq 100",
    "CL=F":     "Crude Oil",
}

REFRESH_SECONDS = 60
EMA_PERIOD = 200
WAT = timezone(timedelta(hours=1))

PAIR_WINDOWS_WAT = {
    "EURUSD=X": (dtime(13, 0), dtime(17, 0)),
    "GBPUSD=X": (dtime(13, 0), dtime(17, 0)),
    "USDJPY=X": (dtime(13, 0), dtime(16, 0)),
    "GC=F":     (dtime(14, 0), dtime(18, 0)),
    "BTC-USD":  (dtime(14, 0), dtime(21, 0)),
    "^DJI":     (dtime(14, 30), dtime(18, 0)),
    "^NDX":     (dtime(14, 30), dtime(18, 0)),
    "CL=F":     (dtime(14, 0), dtime(17, 0)),
}

ALERT_COOLDOWN_MINUTES = 30

ALERT_EMAIL_TO  = os.environ.get("jamesgoodwin9726@gmail.com", "")
SMTP_HOST       = os.environ.get("Smtp.gmail.com", "")
SMTP_PORT       = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USERNAME   = os.environ.get("jamesgoodwin9726@gmail.com", "")
SMTP_PASSWORD   = os.environ.get("rqnz bufc brah fipm", "")
SMTP_CONFIGURED = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and ALERT_EMAIL_TO)

if "last_alert" not in st.session_state:
    st.session_state["last_alert"] = {}
if "alert_log" not in st.session_state:
    st.session_state["alert_log"] = []

# ── helpers ──────────────────────────────────────────────────────────────────

def fetch_candles(pair: str, interval: str) -> pd.DataFrame:
    period = "60d" if interval == "1h" else "30d"
    try:
        df = yf.download(
            tickers=pair, period=period, interval=interval,
            progress=False, auto_adjust=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except Exception:
        return pd.DataFrame()


def fetch_intraday(pair: str) -> pd.DataFrame:
    try:
        df = yf.download(
            tickers=pair, period="1d", interval="5m",
            progress=False, auto_adjust=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except Exception:
        return pd.DataFrame()


def in_pair_window(pair: str, now_wat: datetime) -> bool:
    window = PAIR_WINDOWS_WAT.get(pair)
    if not window:
        return False
    return window[0] <= now_wat.time() < window[1]


def window_label(pair: str) -> str:
    w = PAIR_WINDOWS_WAT.get(pair)
    return f"{w[0].strftime('%H:%M')}–{w[1].strftime('%H:%M')} WAT" if w else "—"


def compute_trend(df: pd.DataFrame) -> tuple[str, float | None]:
    if len(df) < EMA_PERIOD:
        return "Insufficient Data", None
    ema = df["Close"].ewm(span=EMA_PERIOD, adjust=False).mean()
    ema_val = float(ema.iloc[-1])
    price   = float(df["Close"].iloc[-1])
    return ("Verified Bullish 🟢", ema_val) if price > ema_val else ("Verified Bearish 🔴", ema_val)


def detect_fvg_raw(df: pd.DataFrame):
    if len(df) < 3:
        return "Wait", None, None
    recent = df.tail(5)
    if len(recent) < 3:
        return "Wait", None, None
    n_high  = float(recent["High"].iloc[-1])
    n_low   = float(recent["Low"].iloc[-1])
    n2_high = float(recent["High"].iloc[-3])
    n2_low  = float(recent["Low"].iloc[-3])
    sig_time = recent.index[-1].to_pydatetime()
    if sig_time.tzinfo is None:
        sig_time = sig_time.replace(tzinfo=timezone.utc)
    if n2_low > n_high:
        return "SELL FVG", {"top": n2_low, "bottom": n_high}, sig_time
    if n2_high < n_low:
        return "BUY FVG",  {"top": n_low,  "bottom": n2_high}, sig_time
    return "Wait", None, sig_time


def compute_trade_levels(signal: str, zone: dict, df: pd.DataFrame):
    if zone is None:
        return None
    top, bottom = zone["top"], zone["bottom"]
    midpoint    = (top + bottom) / 2.0
    prev_high   = float(df.tail(20)["High"].max())
    prev_low    = float(df.tail(20)["Low"].min())
    if signal == "BUY FVG":
        entry, stop = midpoint, bottom
        risk = entry - stop
        if risk <= 0:
            return None
        return entry, stop, max(entry + 2 * risk, prev_high)
    if signal == "SELL FVG":
        entry, stop = midpoint, top
        risk = stop - entry
        if risk <= 0:
            return None
        return entry, stop, min(entry - 2 * risk, prev_low)
    return None



def is_signal_fresh(sig_time: datetime | None, interval: str) -> bool:
    if sig_time is None:
        return False
    max_age = timedelta(minutes=45) if interval == "15m" else timedelta(hours=3)
    return (datetime.now(timezone.utc) - sig_time) <= max_age


def can_alert(pair: str, signal: str) -> bool:
    last = st.session_state["last_alert"].get(pair)
    if not last:
        return True
    if last["signal"] != signal:
        return True
    return (datetime.now(timezone.utc) - last["time"]) >= timedelta(minutes=ALERT_COOLDOWN_MINUTES)


def record_alert(pair: str, signal: str, ok: bool, detail: str) -> None:
    st.session_state["last_alert"][pair] = {"signal": signal, "time": datetime.now(timezone.utc)}
    st.session_state["alert_log"].insert(0, {
        "Time (WAT)": datetime.now(WAT).strftime("%Y-%m-%d %H:%M:%S"),
        "Pair": pair, "Signal": signal,
        "Result": "✅ Sent" if ok else f"⚠️ {detail}",
    })
    st.session_state["alert_log"] = st.session_state["alert_log"][:30]


def send_email(pair: str, signal: str, entry: float, stop: float, tp: float,
               rr: float, sig_time: datetime, decimals: int) -> tuple[bool, str]:
    if not SMTP_CONFIGURED:
        return False, "SMTP not configured"
    direction = "BUY" if "BUY" in signal else "SELL"
    subject = f"[Forex Sentinel] {direction} {pair} @ {entry:.{decimals}f}"
    body = (
        f"High-quality FVG setup detected.\n\n"
        f"Pair:        {pair}\n"
        f"Signal:      {direction}\n"
        f"Entry:       {entry:.{decimals}f}\n"
        f"Stop Loss:   {stop:.{decimals}f}\n"
        f"Take Profit: {tp:.{decimals}f}\n"
        f"Risk/Reward: {rr:.2f}R\n"
        f"Signal Time: {sig_time.astimezone(WAT).strftime('%Y-%m-%d %H:%M WAT')}\n"
        f"Sent At:     {datetime.now(WAT).strftime('%Y-%m-%d %H:%M WAT')}\n\n"
        f"All filters passed:\n"
        f"  • Pair trading window active\n"
        f"  • 200 EMA trend confirmed\n"
        f"  • Signal is fresh\n"
    )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = SMTP_USERNAME
    msg["To"]      = ALERT_EMAIL_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
            srv.starttls()
            srv.login(SMTP_USERNAME, SMTP_PASSWORD)
            srv.send_message(msg)
        return True, "sent"
    except Exception as exc:
        return False, str(exc)


def send_test_email() -> tuple[bool, str]:
    if not SMTP_CONFIGURED:
        return False, "SMTP credentials missing"
    msg = EmailMessage()
    msg["Subject"] = "[Forex Sentinel] ✅ Test Email — SMTP is working"
    msg["From"]    = SMTP_USERNAME
    msg["To"]      = ALERT_EMAIL_TO
    msg.set_content(
        f"This is a test email from your JS Forex Sentinel bot.\n\n"
        f"Sent at: {datetime.now(WAT).strftime('%Y-%m-%d %H:%M WAT')}\n\n"
        f"If you received this, your email alerts are configured correctly!"
    )
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
            srv.starttls()
            srv.login(SMTP_USERNAME, SMTP_PASSWORD)
            srv.send_message(msg)
        return True, "sent"
    except Exception as exc:
        return False, str(exc)


def fmt(value: float | None, decimals: int) -> str:
    return "—" if value is None else f"{value:.{decimals}f}"


def price_decimals(pair: str, price: float) -> int:
    if pair.endswith("JPY=X"):  return 3
    if pair.endswith("=X"):     return 5
    if price >= 1000:           return 2
    if price >= 10:             return 2
    return 4


def style_status(val: str):
    if val == "BUY FVG":  return "background-color:#16a34a;color:white;font-weight:bold;"
    if val == "SELL FVG": return "background-color:#dc2626;color:white;font-weight:bold;"
    if "SLEEPING" in val: return "background-color:#1f2937;color:#9ca3af;"
    return ""


def style_trend(val: str):
    if "Bullish" in val: return "color:#16a34a;font-weight:bold;"
    if "Bearish" in val: return "color:#dc2626;font-weight:bold;"
    return "color:#9ca3af;"


# ── sidebar ──────────────────────────────────────────────────────────────────

st.title("JS Forex Sentinel")

with st.sidebar:
    st.header("Settings")
    selected_pairs = st.multiselect("Pairs", options=AVAILABLE_PAIRS, default=DEFAULT_PAIRS)
    timeframe  = st.selectbox("Timeframe", options=["15m", "1h"], index=0)
    chart_pair = st.selectbox(
        "Chart Pair",
        options=selected_pairs if selected_pairs else DEFAULT_PAIRS,
    )
    st.caption(f"Auto-refresh every {REFRESH_SECONDS}s | Windows in WAT (UTC+1)")

    st.divider()
    if SMTP_CONFIGURED:
        st.success(f"Email → {ALERT_EMAIL_TO}")
        if st.button("📧 Send Test Email"):
            with st.spinner("Sending…"):
                ok, detail = send_test_email()
            if ok:
                st.success("Test email sent! Check your inbox.")
                record_alert("TEST", "TEST", ok, detail)
            else:
                st.error(f"Failed: {detail}")
    else:
        st.warning("Email alerts disabled — SMTP secrets missing")

# ── placeholders ─────────────────────────────────────────────────────────────

status_ph   = st.empty()
table_ph    = st.empty()
chart_hdr   = st.empty()
chart_ph    = st.empty()
alerts_hdr  = st.empty()
alerts_ph   = st.empty()

# ── main loop ────────────────────────────────────────────────────────────────

while True:
    pairs     = selected_pairs if selected_pairs else DEFAULT_PAIRS
    now_utc   = datetime.now(timezone.utc)
    now_wat   = now_utc.astimezone(WAT)

    rows = []
    for pair in pairs:
        in_window = in_pair_window(pair, now_wat)
        candles   = fetch_candles(pair, timeframe)

        label = PAIR_LABELS.get(pair, pair)
        if candles.empty:
            rows.append({
                "Pair": label, "Window (WAT)": window_label(pair),
                "Price": "—", "Trend (200 EMA)": "No data",
                "Status": "Wait", "Alert Filter": "No data",
                "Entry": "—", "Stop Loss": "—", "Take Profit": "—", "R:R": "—",
            })
            continue

        price    = float(candles["Close"].iloc[-1])
        decimals = price_decimals(pair, price)
        trend, _ = compute_trend(candles)
        raw_sig, zone, sig_time = detect_fvg_raw(candles)

        # apply 200 EMA trend filter
        if trend == "Insufficient Data":
            trend_sig = "Wait"
        elif raw_sig == "BUY FVG"  and "Bullish" in trend: trend_sig = "BUY FVG"
        elif raw_sig == "SELL FVG" and "Bearish" in trend: trend_sig = "SELL FVG"
        else: trend_sig = "Wait"

        # display status
        status = "💤 SLEEPING (Out of Window)" if not in_window else trend_sig

        entry_s = sl_s = tp_s = rr_s = "—"
        filter_reason = "—"
        levels = None

        if trend_sig in ("BUY FVG", "SELL FVG") and zone:
            levels = compute_trade_levels(trend_sig, zone, candles)

        if levels:
            entry, sl, tp = levels
            risk   = abs(entry - sl)
            reward = abs(tp - entry)
            rr_val = reward / risk if risk > 0 else 0
            entry_s = fmt(entry, decimals)
            sl_s    = fmt(sl, decimals)
            tp_s    = fmt(tp, decimals)
            rr_s    = f"{rr_val:.2f}R"

            # determine alert eligibility and reason
            if not in_window:
                filter_reason = "Outside window"
            elif not is_signal_fresh(sig_time, timeframe):
                filter_reason = "Signal too old"
            elif not can_alert(label, trend_sig):
                filter_reason = f"Cooldown ({ALERT_COOLDOWN_MINUTES}min)"
            else:
                filter_reason = "✅ All filters passed"
                ok, detail = send_email(label, trend_sig, entry, sl, tp, rr_val,
                                        sig_time or now_utc, decimals)
                record_alert(label, trend_sig, ok, detail)
                filter_reason = "✅ Alert sent!" if ok else f"⚠️ Email error: {detail}"
        elif trend_sig == "Wait":
            filter_reason = "No FVG / trend mismatch"
        elif not in_window:
            filter_reason = "Outside window"

        rows.append({
            "Pair": label, "Window (WAT)": window_label(pair),
            "Price": fmt(price, decimals), "Trend (200 EMA)": trend,
            "Status": status, "Alert Filter": filter_reason,
            "Entry": entry_s, "Stop Loss": sl_s, "Take Profit": tp_s, "R:R": rr_s,
        })

    df_table = pd.DataFrame(rows, columns=[
        "Pair", "Window (WAT)", "Price", "Trend (200 EMA)",
        "Status", "Alert Filter", "Entry", "Stop Loss", "Take Profit", "R:R",
    ])

    with status_ph.container():
        st.caption(
            f"Last update: {now_wat.strftime('%Y-%m-%d %H:%M:%S WAT')} "
            f"({now_utc.strftime('%H:%M UTC')}) | "
            f"Timeframe: {timeframe} | Pairs: {len(pairs)}"
        )

    with table_ph.container():
        styled = (
            df_table.style
            .map(style_status, subset=["Status"])
            .map(style_trend,  subset=["Trend (200 EMA)"])
        )
        st.dataframe(styled, width="stretch", hide_index=True)

    with chart_hdr.container():
        st.subheader(f"{chart_pair} — Last 1 Day")

    with chart_ph.container():
        intraday = fetch_intraday(chart_pair)
        if intraday.empty:
            st.info("No chart data available.")
        else:
            st.line_chart(intraday["Close"], height=320)

    with alerts_hdr.container():
        st.subheader("Recent Alerts")

    with alerts_ph.container():
        if st.session_state["alert_log"]:
            st.dataframe(
                pd.DataFrame(st.session_state["alert_log"]),
                width="stretch", hide_index=True,
            )
        else:
            st.caption("No alerts sent yet — check the Alert Filter column above to see what's blocking each pair.")

    time.sleep(REFRESH_SECONDS)
    st.rerun()
