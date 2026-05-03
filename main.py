import os
import smtplib
import time
import requests
from datetime import datetime, time as dtime, timedelta, timezone
from email.message import EmailMessage

import pandas as pd
import streamlit as st
import yfinance as yf

# ─────────────────────────────────────────────
st.set_page_config(page_title="JS Forex Sentinel", layout="wide")

DEFAULT_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X",
    "GC=F", "BTC-USD", "^DJI", "^NDX", "CL=F"
]

REFRESH_SECONDS = 300
EMA_PERIOD = 200
WAT = timezone(timedelta(hours=1))

ALERT_COOLDOWN = 30

# ── YOUR EMAIL (from what you gave) ──────────
ALERT_EMAIL_TO = "jamesgoodwin9726@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USERNAME = "jamesgoodwin9726@gmail.com"
SMTP_PASSWORD = "rqnz bufc brah fipm"

# ── YOUR TELEGRAM ───────────────────────────
TELEGRAM_BOT_TOKEN = "8672396797:AAHuNM_ziWT_mEytIk6z3HqGxDyDRjCnnY"
TELEGRAM_CHAT_ID = "7248339399"

# ── STATES ───────────────────────────────────
if "last_alert" not in st.session_state:
    st.session_state.last_alert = {}

if "alerts" not in st.session_state:
    st.session_state.alerts = []

# ── DATA ─────────────────────────────────────

def get_data(pair):
    try:
        df = yf.download(pair, period="60d", interval="15m")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except:
        return pd.DataFrame()

# ── LOGIC ───────────────────────────────────

def trend(df):
    if len(df) < EMA_PERIOD:
        return "NO DATA"

    ema = df["Close"].ewm(span=EMA_PERIOD).mean()
    return "BULLISH" if df["Close"].iloc[-1] > ema.iloc[-1] else "BEARISH"

def fvg(df):
    if len(df) < 5:
        return None

    if df["Low"].iloc[-3] > df["High"].iloc[-1]:
        return "SELL"
    if df["High"].iloc[-3] < df["Low"].iloc[-1]:
        return "BUY"

    return None

# ── EMAIL ───────────────────────────────────

def send_email(pair, signal, price):
    msg = EmailMessage()
    msg["Subject"] = f"Signal {signal} {pair}"
    msg["From"] = SMTP_USERNAME
    msg["To"] = ALERT_EMAIL_TO
    msg.set_content(f"{signal} {pair} @ {price}")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USERNAME, SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except:
        return False

# ── TELEGRAM ────────────────────────────────

def send_telegram(pair, signal, price):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    text = f"""
🚨 SIGNAL
{signal} {pair}
Price: {price}
"""

    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text
        })
        return r.status_code == 200
    except:
        return False

# ── ALERT SYSTEM ────────────────────────────

def trigger(pair, signal, price):
    last = st.session_state.last_alert.get(pair)

    if last and time.time() - last < ALERT_COOLDOWN * 60:
        return

    send_email(pair, signal, price)
    send_telegram(pair, signal, price)

    st.session_state.last_alert[pair] = time.time()

    st.session_state.alerts.insert(0, {
        "Pair": pair,
        "Signal": signal,
        "Price": price,
        "Time": datetime.now().strftime("%H:%M:%S")
    })

# ── UI ───────────────────────────────────────

st.title("JS Forex Sentinel")

pairs = st.multiselect("Pairs", DEFAULT_PAIRS, default=DEFAULT_PAIRS)

table = []

for p in pairs:
    df = get_data(p)

    if df.empty:
        continue

    price = df["Close"].iloc[-1]
    tr = trend(df)
    sig = fvg(df)

    status = "WAIT"

    if sig == "BUY" and tr == "BULLISH":
        status = "BUY"
        trigger(p, "BUY", price)

    elif sig == "SELL" and tr == "BEARISH":
        status = "SELL"
        trigger(p, "SELL", price)

    table.append([p, price, tr, status])

df = pd.DataFrame(table, columns=["Pair", "Price", "Trend", "Signal"])

st.dataframe(df, use_container_width=True)

st.subheader("Alerts")
st.dataframe(pd.DataFrame(st.session_state.alerts),
             use_container_width=True)

time.sleep(REFRESH_SECONDS)
