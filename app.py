import streamlit as st
import pandas as pd
import requests
import sqlite3
from datetime import datetime, timezone
import math

st.set_page_config(page_title="NOX AUTO TRADER 3.3", page_icon="📈", layout="wide")
st.title("📈 NOX AUTO TRADER 3.3")
st.caption("Crypto-first automated PAPER trading · 1-minute monitoring · 500 SEK · No leverage")
st.warning("PAPER TRADING ENDAST. Inga riktiga order eller pengar används. Första automatiska paper-versionen handlar endast krypto där pris- och valutaberäkningen kan kontrolleras. Aktier, forex och råvaror är fortsatt signalbevakning.")

# Risk and execution assumptions
START_CASH = 500.0
RISK_FRACTION = 0.01
MAX_OPEN = 2
MAX_NOTIONAL_FRACTION = 0.25
FEE_RATE = 0.001       # illustrative paper fee per side
STOP_ATR = 1.5
TARGET_R = 2.0
SIGNAL_THRESHOLD = 80
MIN_NOTIONAL_SEK = 20.0
MAX_HOLD_MINUTES = 90

DB = "nox_v33_paper.sqlite"
HEADERS = {"User-Agent": "Mozilla/5.0 NOX-Auto-Trader/3.3"}
OKX_URL = "https://www.okx.com/api/v5/market/candles"
BYBIT_URL = "https://api.bybit.com/v5/market/kline"
FX_URL = "https://open.er-api.com/v6/latest/USD"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

CRYPTO = [
    ("BTC-USDT","BTCUSDT"), ("ETH-USDT","ETHUSDT"),
    ("SOL-USDT","SOLUSDT"), ("BNB-USDT","BNBUSDT"),
    ("XRP-USDT","XRPUSDT")
]
OTHER = [
    ("AAPL","Aktie"),("MSFT","Aktie"),("NVDA","Aktie"),("AMZN","Aktie"),
    ("GOOGL","Aktie"),("META","Aktie"),("TSLA","Aktie"),("JPM","Aktie"),
    ("AVGO","Aktie"),("LLY","Aktie"),
    ("EURUSD=X","Forex"),("GBPUSD=X","Forex"),("USDJPY=X","Forex"),
    ("GC=F","Råvara"),("SI=F","Råvara"),("CL=F","Råvara")
]

def db_connect():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS account(
        id INTEGER PRIMARY KEY CHECK(id=1), cash REAL NOT NULL, realized REAL NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, qty REAL NOT NULL,
        entry_usdt REAL NOT NULL, stop_usdt REAL NOT NULL, target_usdt REAL NOT NULL,
        entry_sek REAL NOT NULL, stop_sek REAL NOT NULL, target_sek REAL NOT NULL,
        risk_sek REAL NOT NULL, notional_sek REAL NOT NULL, entry_fee_sek REAL NOT NULL,
        opened_utc TEXT NOT NULL, entry_candle TEXT NOT NULL, status TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT,
        entry_sek REAL, exit_sek REAL, qty REAL, pnl_sek REAL, fees_sek REAL,
        risk_sek REAL, opened_utc TEXT, closed_utc TEXT, reason TEXT, signal_score INTEGER)""")
    if c.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0:
        c.execute("INSERT INTO account(id,cash,realized) VALUES(1,?,0)", (START_CASH,))
    c.commit()
    return c

conn = db_connect()

@st.cache_data(ttl=55, show_spinner=False)
def usd_sek_rate():
    r = requests.get(FX_URL, headers=HEADERS, timeout=8)
    r.raise_for_status()
    j = r.json()
    rate = float(j["rates"]["SEK"])
    if not 5 < rate < 25:
        raise ValueError("Orimlig USD/SEK-kurs")
    return rate

def get_okx(inst):
    r = requests.get(OKX_URL, params={"instId":inst,"bar":"1m","limit":"100"},
                     headers=HEADERS, timeout=8)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "0" or not j.get("data"):
        raise ValueError("OKX: " + str(j.get("msg","ingen data")))
    d = pd.DataFrame(j["data"], columns=["ts","open","high","low","close","volume","vol_ccy","vol_quote","confirm"])
    for col in ["open","high","low","close","volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.sort_values("ts").dropna(subset=["close"]).reset_index(drop=True)
    # Use only confirmed/closed candles to avoid acting on a moving candle.
    closed = d[d["confirm"].astype(str) == "1"].copy()
    if len(closed) < 35:
        raise ValueError("För få stängda 1m-staplar från OKX")
    return closed.reset_index(drop=True), "OKX"

def get_bybit(symbol):
    r = requests.get(BYBIT_URL, params={"category":"spot","symbol":symbol,"interval":"1","limit":100},
                     headers=HEADERS, timeout=8)
    r.raise_for_status()
    j = r.json()
    if j.get("retCode") != 0 or not j.get("result",{}).get("list"):
        raise ValueError("Bybit: " + str(j.get("retMsg","ingen data")))
    d = pd.DataFrame(j["result"]["list"], columns=["ts","open","high","low","close","volume","turnover"])
    for col in ["open","high","low","close","volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.sort_values("ts").dropna(subset=["close"]).reset_index(drop=True)
    # Exclude current, potentially incomplete minute.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    d = d[pd.to_numeric(d["ts"], errors="coerce") < (now_ms // 60000) * 60000]
    if len(d) < 35:
        raise ValueError("För få stängda 1m-staplar från Bybit")
    return d.reset_index(drop=True), "Bybit"

def get_crypto(inst, symbol):
    errs=[]
    for fn in (lambda:get_okx(inst), lambda:get_bybit(symbol)):
        try:
            return fn()
        except Exception as e:
            errs.append(str(e)[:90])
    raise RuntimeError("OKX/Bybit: " + " | ".join(errs))

def get_yahoo(symbol):
    r = requests.get(YAHOO_URL + requests.utils.quote(symbol,safe=""),
        params={"range":"1d","interval":"1m"}, headers=HEADERS, timeout=8)
    r.raise_for_status()
    chart = r.json().get("chart",{})
    if chart.get("error") or not chart.get("result"):
        raise ValueError("Yahoo saknar intradagsdata")
    result = chart["result"][0]
    q = result["indicators"]["quote"][0]
    d = pd.DataFrame({k:q.get(k) for k in ["open","high","low","close","volume"]})
    d["ts"] = result.get("timestamp", list(range(len(d))))[:len(d)]
    d = d.dropna(subset=["close"]).reset_index(drop=True)
    if len(d) < 35:
        raise ValueError("För få 1m-staplar")
    return d, "Yahoo Finance"

def analyze(d):
    c=d["close"]
    e9=c.ewm(span=9,adjust=False).mean()
    e21=c.ewm(span=21,adjust=False).mean()
    delta=c.diff()
    gain=delta.clip(lower=0).rolling(14).mean()
    loss=-delta.clip(upper=0).rolling(14).mean()
    rs=gain/loss.replace(0,float("nan"))
    rsi=100-100/(1+rs)
    tr=pd.concat([(d["high"]-d["low"]),(d["high"]-c.shift()).abs(),
                  (d["low"]-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.rolling(14).mean()
    vol=d["volume"].fillna(0) if "volume" in d else pd.Series([0]*len(d))
    score=0
    if e9.iloc[-1] > e21.iloc[-1]: score += 40
    if c.iloc[-1] > e9.iloc[-1]: score += 20
    if pd.notna(rsi.iloc[-1]) and 50 <= rsi.iloc[-1] <= 68: score += 25
    if vol.iloc[-1] >= vol.tail(20).median(): score += 15
    return float(c.iloc[-1]), int(score), float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None, float(atr.iloc[-1]), str(d.iloc[-1]["ts"])

def account():
    return conn.execute("SELECT cash,realized FROM account WHERE id=1").fetchone()

def open_position(symbol, price, atr, fx, score, candle):
    if conn.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0] >= MAX_OPEN:
        return "Max antal öppna positioner"
    if conn.execute("SELECT COUNT(*) FROM positions WHERE symbol=? AND status='OPEN'",(symbol,)).fetchone()[0]:
        return "Position finns redan"
    # One attempt per confirmed candle per symbol, including previously closed trades.
    if conn.execute("SELECT COUNT(*) FROM trades WHERE symbol=? AND opened_utc LIKE ?",(symbol, candle_prefix(candle))).fetchone()[0]:
        return "Redan handlad denna minut"
    if conn.execute("SELECT COUNT(*) FROM positions WHERE symbol=? AND entry_candle=?",(symbol,candle)).fetchone()[0]:
        return "Redan öppnad på denna stapel"

    cash, realized = account()
    # Conservative account equity estimate: cash plus entry value of open positions.
    open_cost = conn.execute("SELECT COALESCE(SUM(notional_sek+entry_fee_sek),0) FROM positions WHERE status='OPEN'").fetchone()[0]
    equity = cash + open_cost
    risk_budget = equity * RISK_FRACTION
    stop_distance = max(1.5 * atr, price * 0.002)
    stop = price - stop_distance
    target = price + 2.0 * stop_distance
    entry_sek = price * fx
    stop_sek = stop * fx
    target_sek = target * fx
    risk_per_unit_sek = stop_distance * fx
    qty_by_risk = risk_budget / risk_per_unit_sek
    max_notional = min(cash * MAX_NOTIONAL_FRACTION, equity * MAX_NOTIONAL_FRACTION)
    qty_by_cash = max_notional / entry_sek
    qty = min(qty_by_risk, qty_by_cash)
    notional_sek = qty * entry_sek
    entry_fee = notional_sek * FEE_RATE
    if notional_sek < MIN_NOTIONAL_SEK:
        return "Position för liten efter riskbegränsning"
    if notional_sek + entry_fee > cash:
        return "Otillräckliga kontanter"
    actual_risk = qty * risk_per_unit_sek
    if actual_risk > risk_budget + 0.000001:
        return "Riskkontroll stoppade ordern"
    now=datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE account SET cash=cash-? WHERE id=1",(notional_sek+entry_fee,))
    conn.execute("""INSERT INTO positions(symbol,qty,entry_usdt,stop_usdt,target_usdt,
        entry_sek,stop_sek,target_sek,risk_sek,notional_sek,entry_fee_sek,opened_utc,
        entry_candle,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')""",
        (symbol,qty,price,stop,target,entry_sek,stop_sek,target_sek,actual_risk,
         notional_sek,entry_fee,now,candle))
    conn.commit()
    return f"Paper BUY {symbol} · risk {actual_risk:.2f} kr"

def candle_prefix(candle):
    # For millisecond timestamps, match the minute prefix.
    try:
        dt=datetime.fromtimestamp(int(candle)/1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M%") 
    except Exception:
        return "%"

def close_position(pos, price, fx, reason):
    (pid,symbol,qty,entry,stop,target,entry_sek,stop_sek,target_sek,risk,notional,entry_fee,opened,candle)=pos
    exit_sek=price*fx
    exit_value=qty*exit_sek
    exit_fee=exit_value*FEE_RATE
    gross=(exit_sek-entry_sek)*qty
    pnl=gross-entry_fee-exit_fee
    cash,realized=account()
    conn.execute("UPDATE account SET cash=cash+?,realized=realized+? WHERE id=1",
                 (exit_value-exit_fee,pnl))
    conn.execute("UPDATE positions SET status='CLOSED' WHERE id=?",(pid,))
    conn.execute("""INSERT INTO trades(symbol,side,entry_sek,exit_sek,qty,pnl_sek,fees_sek,
        risk_sek,opened_utc,closed_utc,reason,signal_score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (symbol,"LONG",entry_sek,exit_sek,qty,pnl,entry_fee+exit_fee,risk,opened,
         datetime.now(timezone.utc).isoformat(),reason,None))
    conn.commit()

def manage_positions(fx):
    open_rows=conn.execute("""SELECT id,symbol,qty,entry_usdt,stop_usdt,target_usdt,
        entry_sek,stop_sek,target_sek,risk_sek,notional_sek,entry_fee_sek,opened_utc,entry_candle
        FROM positions WHERE status='OPEN'""").fetchall()
    events=[]
    for p in open_rows:
        symbol=p[1]
        try:
            d,provider=get_crypto(symbol.replace("USDT","-USDT"),symbol)
            price=float(d.close.iloc[-1])
            opened=datetime.fromisoformat(p[12])
            age=(datetime.now(timezone.utc)-opened).total_seconds()/60
            reason=None
            if price <= p[4]: reason="Stop-loss"
            elif price >= p[5]: reason="Take-profit"
            elif age >= MAX_HOLD_MINUTES: reason="Tidsgräns 90 min"
            if reason:
                close_position(p,price,fx,reason)
                events.append(f"Stängde {symbol}: {reason}")
        except Exception as e:
            events.append(f"Kunde inte uppdatera {symbol}: {str(e)[:70]}")
    return events

def scan_cycle():
    events=[]
    try:
        fx=usd_sek_rate()
    except Exception as e:
        return [], [f"USD/SEK-kurs saknas: {str(e)[:90]}"], None
    events += manage_positions(fx)
    rows=[]
    for inst,symbol in CRYPTO:
        try:
            d,provider=get_crypto(inst,symbol)
            price,score,rsi,atr,candle=analyze(d)
            action="Avvakta"
            if score >= SIGNAL_THRESHOLD:
                action=open_position(symbol,price,atr,fx,score,candle)
                if action.startswith("Paper BUY"):
                    events.append(action)
                elif action not in ("Position finns redan","Redan handlad denna minut","Redan öppnad på denna stapel"):
                    events.append(f"{symbol}: {action}")
            rows.append({"Instrument":symbol,"Källa":provider,"Pris (USDT)":price,
                "Signal":score,"RSI":round(rsi,1) if rsi is not None else None,
                "Senaste stängda 1m-stapel":candle,"Åtgärd":action,"Data":"OK"})
        except Exception as e:
            rows.append({"Instrument":symbol,"Källa":"—","Pris (USDT)":None,"Signal":None,
                "RSI":None,"Senaste stängda 1m-stapel":"—","Åtgärd":"—","Data":str(e)[:120]})
    return rows,events,fx

# Sidebar controls
with st.sidebar:
    st.header("Inställningar")
    st.metric("Startkapital", "500 kr")
    st.metric("Risk per affär", "1 % av aktuellt eget kapital")
    st.metric("Max öppna positioner", MAX_OPEN)
    st.metric("Simulerad avgift", "0,10 % per sida")
    auto=st.toggle("Automatisk paper trading varje minut", value=True)
    st.caption("Krypto spot LONG-only. Ingen hävstång. Högst 25 % av kapitalet i en position.")
    st.caption("Appen måste vara aktiv. Streamlit Cloud garanterar inte 24/7-drift.")

cash,realized=account()
open_df=pd.read_sql_query("SELECT symbol,qty,entry_sek,stop_sek,target_sek,risk_sek,opened_utc FROM positions WHERE status='OPEN'",conn)
open_cost=float(conn.execute("SELECT COALESCE(SUM(notional_sek+entry_fee_sek),0) FROM positions WHERE status='OPEN'").fetchone()[0])
equity=cash+open_cost
c1,c2,c3,c4=st.columns(4)
c1.metric("Kontanter",f"{cash:.2f} kr")
c2.metric("Portföljvärde (förenklat)",f"{equity:.2f} kr")
c3.metric("Öppna paper-positioner",len(open_df))
c4.metric("Realiserat resultat",f"{realized:+.2f} kr")

st.info("Automatisk paper trading är aktiverad som standard. Nya köp kräver signal ≥80/100, bekräftad stängd 1m-stapel, max 1 % riskbudget och tillgängligt kapital. Endast krypto öppnar positioner i denna första version.")
if "last_rows" not in st.session_state: st.session_state.last_rows=[]
if "last_events" not in st.session_state: st.session_state.last_events=[]
if "last_cycle" not in st.session_state: st.session_state.last_cycle="Ingen cykel ännu"

def run_cycle():
    rows,events,fx=scan_cycle()
    st.session_state.last_rows=rows
    st.session_state.last_events=events
    st.session_state.last_cycle=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    st.session_state.last_fx=fx

if auto:
    @st.fragment(run_every="60s")
    def live_engine():
        run_cycle()
        st.caption(f"Senaste automatikcykel: {st.session_state.last_cycle} · USD/SEK: {st.session_state.get('last_fx')}")
        if st.session_state.last_events:
            for e in st.session_state.last_events[-8:]:
                st.write("• "+e)
        if st.session_state.last_rows:
            st.subheader("Krypto – automatisk paper trading")
            st.dataframe(pd.DataFrame(st.session_state.last_rows),use_container_width=True,hide_index=True)
        current=pd.read_sql_query("SELECT symbol,qty,entry_sek,stop_sek,target_sek,risk_sek,opened_utc FROM positions WHERE status='OPEN'",conn)
        st.subheader("Öppna paper-positioner")
        if current.empty: st.write("Inga öppna positioner. NOX väntar på kvalificerade signaler.")
        else: st.dataframe(current,use_container_width=True,hide_index=True)
        history=pd.read_sql_query("SELECT symbol,entry_sek,exit_sek,pnl_sek,fees_sek,reason,closed_utc FROM trades ORDER BY id DESC LIMIT 30",conn)
        st.subheader("Senaste stängda paper-affärer")
        if history.empty: st.write("Ingen stängd affär ännu.")
        else: st.dataframe(history,use_container_width=True,hide_index=True)
    live_engine()
else:
    if st.button("▶ Kör en paper trading-cykel",type="primary"):
        run_cycle()
    if st.session_state.last_rows:
        st.dataframe(pd.DataFrame(st.session_state.last_rows),use_container_width=True,hide_index=True)
    st.write("Automatiken är pausad. Aktivera reglaget för minutvis bevakning.")

st.subheader("Övriga marknader – endast signalbevakning")
st.write("Aktier, forex och råvaror är ännu inte kopplade till automatisk paper-exekvering. Vi aktiverar dem först när valutakonvertering, kontraktsstorlekar och handelsvillkor har verifierats.")

st.caption("VIKTIGT: SQLite-filen ligger på Streamlit-appens filsystem och kan försvinna vid omstart/omdeploy. Detta är en testmiljö, inte en beständig handelsjournal. Avgift 0,10 % per sida är ett förenklat antagande; spread/slippage modelleras inte. Testa under uppsikt innan du drar slutsatser.")
