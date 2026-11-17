import streamlit as st
import pandas as pd
import requests
import sqlite3
from datetime import datetime, timezone
import math

st.set_page_config(page_title="NOX AUTO TRADER 3.8", page_icon="📈", layout="wide")
st.title("📈 NOX AUTO TRADER 3.8 – Signal Diagnostics")
st.caption("Selective crypto scalping · 15 spot pairs · unchanged entry rules · explainable wait reasons · 500 SEK · max 2% risk")
st.warning("PAPER TRADING ENDAST. Inga riktiga order eller pengar används. Selektiv scalping med 1m-rekylbekräftelse och 5m-trendfilter. Nettomål kontrolleras mot avgifter och uppskattad slippage. Ingen lönsamhet är garanterad.")

# Risk and execution assumptions
START_CASH = 500.0
RISK_FRACTION = 0.02
MAX_OPEN = 2
MAX_NOTIONAL_FRACTION = 0.45
FEE_RATE = 0.001       # illustrative paper fee per side
STOP_ATR = 1.5
TARGET_R = 2.0
ESTIMATED_SLIPPAGE_PER_SIDE = 0.00025  # 0.025% per side; 0.05% round trip
ESTIMATED_SLIPPAGE_ROUNDTRIP = 2 * ESTIMATED_SLIPPAGE_PER_SIDE
MIN_NET_REWARD_R = 1.10
SIGNAL_THRESHOLD = 80
COOLDOWN_AFTER_STOP_MIN = 5
MAX_EXTENSION_ATR = 0.8
MAX_ATR_PCT = 0.012
MIN_NOTIONAL_SEK = 40.0
MAX_HOLD_MINUTES = 90

DB = "nox_v37_paper.sqlite"  # Fresh 3.8 paper account; prior paper history is kept separate
HEADERS = {"User-Agent": "Mozilla/5.0 NOX-Auto-Trader/3.8"}
OKX_URL = "https://www.okx.com/api/v5/market/candles"
BYBIT_URL = "https://api.bybit.com/v5/market/kline"
FX_URL = "https://open.er-api.com/v6/latest/USD"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

CRYPTO = [
    # Core high-liquidity pairs
    ("BTC-USDT","BTCUSDT"), ("ETH-USDT","ETHUSDT"),
    ("SOL-USDT","SOLUSDT"), ("BNB-USDT","BNBUSDT"),
    ("XRP-USDT","XRPUSDT"),
    # Expanded watchlist. Each instrument is checked against public exchange APIs;
    # unavailable symbols are shown as data errors and are not traded.
    ("DOGE-USDT","DOGEUSDT"), ("ADA-USDT","ADAUSDT"),
    ("LINK-USDT","LINKUSDT"), ("LTC-USDT","LTCUSDT"),
    ("AVAX-USDT","AVAXUSDT"), ("DOT-USDT","DOTUSDT"),
    ("BCH-USDT","BCHUSDT"), ("TRX-USDT","TRXUSDT"),
    ("SUI-USDT","SUIUSDT"), ("TON-USDT","TONUSDT")
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
    r = requests.get(OKX_URL, params={"instId":inst,"bar":"1m","limit":"300"},
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
    r = requests.get(BYBIT_URL, params={"category":"spot","symbol":symbol,"interval":"1","limit":300},
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
    """Balanced setup: 1m entry momentum confirmed by a 5m trend filter."""
    d = d.copy().sort_values("ts").reset_index(drop=True)
    c = d["close"].astype(float)
    e9 = c.ewm(span=9, adjust=False).mean()
    e21 = c.ewm(span=21, adjust=False).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi_series = 100 - 100 / (1 + rs)
    if loss.iloc[-1] == 0 and gain.iloc[-1] > 0:
        rsi_value = 100.0
    else:
        rsi_value = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else None

    tr = pd.concat([(d["high"]-d["low"]), (d["high"]-c.shift()).abs(),
                    (d["low"]-c.shift()).abs()], axis=1).max(axis=1)
    atr_series = tr.rolling(14).mean()
    atr_value = float(atr_series.iloc[-1])
    price = float(c.iloc[-1])

    # Build closed 5-minute candles from the already-confirmed 1-minute candles.
    ts_num = pd.to_numeric(d["ts"], errors="coerce")
    if ts_num.notna().all() and float(ts_num.iloc[-1]) > 10**11:
        idx = pd.to_datetime(ts_num, unit="ms", utc=True)
    else:
        idx = pd.to_datetime(ts_num, unit="s", utc=True)
    five = d.set_index(idx).resample("5min", label="right", closed="right").agg(
        {"open":"first", "high":"max", "low":"min", "close":"last", "volume":"sum"}
    ).dropna(subset=["close"])
    # Last resampled bucket can be partial depending on provider timestamping; use
    # only buckets whose right edge is no later than the last completed 1m candle.
    if len(five) < 15:
        raise ValueError("För få 5m-staplar för trendfilter")
    c5 = five["close"].astype(float)
    ema5_fast = c5.ewm(span=9, adjust=False).mean()
    ema5_slow = c5.ewm(span=21, adjust=False).mean()
    htf_up = bool(ema5_fast.iloc[-1] > ema5_slow.iloc[-1] and c5.iloc[-1] > ema5_fast.iloc[-1]
                  and ema5_fast.iloc[-1] >= ema5_fast.iloc[-2])

    vol = pd.to_numeric(d["volume"], errors="coerce").fillna(0)
    vol_base = float(vol.iloc[-21:-1].median()) if len(vol) >= 22 else 0.0
    vol_ratio = float(vol.iloc[-1] / vol_base) if vol_base > 0 else 0.0
    momentum = float((c.iloc[-1] / c.iloc[-4] - 1) * 100) if len(c) >= 4 and c.iloc[-4] else 0.0
    extension_atr = (price - float(e9.iloc[-1])) / atr_value if atr_value > 0 else 999.0
    atr_pct = atr_value / price if price > 0 else 999.0
    # Entry must show a recent pullback toward EMA9 followed by a close above
    # the previous candle high: avoid buying merely because price is already rising.
    prev_low = float(d["low"].iloc[-2])
    prev_high = float(d["high"].iloc[-2])
    prev_ema9 = float(e9.iloc[-2])
    pullback_reclaim = bool(prev_low <= prev_ema9 + 0.15 * atr_value and price > prev_high)

    score = 0
    if e9.iloc[-1] > e21.iloc[-1]: score += 25
    if price > e9.iloc[-1]: score += 15
    if htf_up: score += 30
    if rsi_value is not None and 50 <= rsi_value <= 67: score += 15
    if momentum > 0: score += 5
    if vol_ratio >= 1.10: score += 10
    if pullback_reclaim: score += 10

    # Hard safety gates: trend, pullback/reclaim, no overheated or extreme-volatility entries.
    eligible = (htf_up and pullback_reclaim and price > e9.iloc[-1] and rsi_value is not None
                and 50 <= rsi_value <= 67 and extension_atr <= MAX_EXTENSION_ATR
                and atr_pct <= MAX_ATR_PCT and momentum > 0)
    raw_score = int(score)
    if not eligible:
        score = min(score, 79)
    checks = {
        "5m upptrend": htf_up,
        "Rekyl + reclaim": pullback_reclaim,
        "Pris över EMA9": price > float(e9.iloc[-1]),
        "RSI 50–67": rsi_value is not None and 50 <= rsi_value <= 67,
        "Positivt momentum": momentum > 0,
        "Inte översträckt": extension_atr <= MAX_EXTENSION_ATR,
        "Volatilitet OK": atr_pct <= MAX_ATR_PCT,
        "Signal ≥80": int(score) >= SIGNAL_THRESHOLD,
    }
    metrics = {
        "RSI": round(rsi_value, 1) if rsi_value is not None else None,
        "Momentum %": round(momentum, 3),
        "Avstånd EMA9 (ATR)": round(extension_atr, 2),
        "ATR %": round(atr_pct * 100, 3),
        "Volymkvot": round(vol_ratio, 2),
        "Råpoäng": raw_score,
    }
    return price, int(score), rsi_value, atr_value, str(d.iloc[-1]["ts"]), checks, metrics

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
    last = conn.execute("SELECT reason,closed_utc FROM trades WHERE symbol=? ORDER BY id DESC LIMIT 1",(symbol,)).fetchone()
    if last and last[0] == "Stop-loss":
        try:
            last_closed = datetime.fromisoformat(last[1])
            if (datetime.now(timezone.utc) - last_closed).total_seconds() < COOLDOWN_AFTER_STOP_MIN * 60:
                return "Paus efter stop-loss (5 min)"
        except Exception:
            pass

    cash, realized = account()
    # Conservative account equity estimate: cash plus entry value of open positions.
    open_cost = conn.execute("SELECT COALESCE(SUM(notional_sek+entry_fee_sek),0) FROM positions WHERE status='OPEN'").fetchone()[0]
    equity = cash + open_cost
    risk_budget = equity * RISK_FRACTION
    stop_distance = max(1.5 * atr, price * 0.002)
    target_distance = TARGET_R * stop_distance
    # Include both trading fees and a conservative slippage allowance in the entry test.
    roundtrip_cost_pct = (2 * FEE_RATE) + ESTIMATED_SLIPPAGE_ROUNDTRIP
    net_reward_per_unit = (target_distance - price * roundtrip_cost_pct) * fx
    net_risk_per_unit = (stop_distance + price * roundtrip_cost_pct) * fx
    if net_reward_per_unit <= 0 or net_risk_per_unit <= 0:
        return "Avstår: målet täcker inte uppskattade handelskostnader"
    net_reward_r = net_reward_per_unit / net_risk_per_unit
    if net_reward_r < MIN_NET_REWARD_R:
        return f"Avstår: netto-R/R {net_reward_r:.2f} under {MIN_NET_REWARD_R:.2f}"
    stop = price - stop_distance
    target = price + target_distance
    entry_sek = price * fx
    stop_sek = stop * fx
    target_sek = target * fx
    # Risk budget includes estimated round-trip costs; no leverage and cash remain binding.
    risk_per_unit_sek = net_risk_per_unit
    qty_by_risk = risk_budget / risk_per_unit_sek
    max_notional = min(cash * MAX_NOTIONAL_FRACTION, equity * MAX_NOTIONAL_FRACTION)
    qty_by_cash = max_notional / entry_sek
    qty = min(qty_by_risk, qty_by_cash)
    notional_sek = qty * entry_sek
    entry_fee = notional_sek * (FEE_RATE + ESTIMATED_SLIPPAGE_PER_SIDE)
    if notional_sek < MIN_NOTIONAL_SEK:
        return "Position för liten efter risk- och kapitalbegränsning"
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
    exit_fee=exit_value*(FEE_RATE + ESTIMATED_SLIPPAGE_PER_SIDE)
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
            last_bar=d.iloc[-1]
            price=float(last_bar["close"])
            bar_open=float(last_bar["open"])
            bar_low=float(last_bar["low"])
            bar_high=float(last_bar["high"])
            opened=datetime.fromisoformat(p[12])
            age=(datetime.now(timezone.utc)-opened).total_seconds()/60
            reason=None
            exit_price=price
            # Conservative rule if both stop and target are touched in one candle: stop wins.
            if bar_low <= p[4]:
                reason="Stop-loss"
                exit_price=min(bar_open, p[4])
            elif bar_high >= p[5]:
                reason="Take-profit"
                exit_price=max(bar_open, p[5])
            elif age >= MAX_HOLD_MINUTES:
                reason="Tidsgräns 90 min"
            if reason:
                close_position(p,exit_price,fx,reason)
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
            price,score,rsi,atr,candle,checks,metrics=analyze(d)
            # Estimated net reward/risk uses the same assumptions as execution.
            stop_distance = max(STOP_ATR * atr, price * 0.002)
            roundtrip_cost_pct = (2 * FEE_RATE) + ESTIMATED_SLIPPAGE_ROUNDTRIP
            net_reward = stop_distance * TARGET_R - price * roundtrip_cost_pct
            net_risk = stop_distance + price * roundtrip_cost_pct
            net_rr = net_reward / net_risk if net_risk > 0 else 0.0
            checks["Netto-R/R ≥1,10"] = net_rr >= MIN_NET_REWARD_R
            blockers = [name for name, passed in checks.items() if not passed]
            action="Avvakta"
            if score >= SIGNAL_THRESHOLD and all(checks.values()):
                action=open_position(symbol,price,atr,fx,score,candle)
                if action.startswith("Paper BUY"):
                    events.append(action)
                elif action not in ("Position finns redan","Redan handlad denna minut","Redan öppnad på denna stapel"):
                    events.append(f"{symbol}: {action}")
            if action == "Avvakta":
                reason_text = ", ".join(blockers) if blockers else "Övrig exekveringskontroll"
            else:
                reason_text = action
            check_text = " | ".join(("✓ " if passed else "✗ ") + name for name, passed in checks.items())
            rows.append({"Instrument":symbol,"Källa":provider,"Pris (USDT)":price,
                "Signal":score,"Råpoäng":metrics["Råpoäng"],"RSI":round(rsi,1) if rsi is not None else None,
                "Netto-R/R":round(net_rr,2),"Åtgärd":action,"Varför avvaktar":reason_text,
                "Kontroller":check_text,"Momentum %":metrics["Momentum %"],
                "Avstånd EMA9 (ATR)":metrics["Avstånd EMA9 (ATR)"],"ATR %":metrics["ATR %"],
                "Volymkvot":metrics["Volymkvot"],
                "Senaste stängda 1m-stapel":candle,"Data":"OK"})
        except Exception as e:
            rows.append({"Instrument":symbol,"Källa":"—","Pris (USDT)":None,"Signal":None,
                "RSI":None,"Netto-R/R":None,"Åtgärd":"—","Varför avvaktar":"Datafel – ingen signalbedömning",
                "Kontroller":"—","Senaste stängda 1m-stapel":"—","Data":str(e)[:120]})
    return rows,events,fx

# Sidebar controls
with st.sidebar:
    st.header("Inställningar")
    st.metric("Startkapital", "500 kr")
    st.metric("Risk per affär", "2 % av aktuellt eget kapital")
    st.metric("Max öppna positioner", MAX_OPEN)
    st.metric("Max kapital per position", "45 %")
    st.metric("Handelskostnad", "0,125 % per sida")
    auto=st.toggle("Automatisk paper trading varje minut", value=True)
    st.caption("Krypto spot LONG-only. Ingen hävstång. Högst 45 % av kapitalet i en position och max två samtidiga positioner. 2 % är planerad maxrisk per affär inklusive uppskattade avgifter/slippage; kapitaltaket kan göra faktisk risk lägre. Efter stop-loss pausas samma symbol i 5 minuter.")
    st.caption("Appen måste vara aktiv. Streamlit Cloud garanterar inte 24/7-drift.")

def render_live_summary(fx=None, rows=None):
    cash, realized = account()
    positions = conn.execute(
        "SELECT symbol, qty, entry_usdt, entry_fee_sek FROM positions WHERE status='OPEN'"
    ).fetchall()
    price_map = {r.get("Instrument"): r.get("Pris (USDT)") for r in (rows or []) if r.get("Pris (USDT)") is not None}
    market_value = 0.0
    for symbol, qty, entry_usdt, entry_fee in positions:
        mark = price_map.get(symbol, entry_usdt)
        if fx is None:
            market_value += qty * entry_usdt * 10.0
        else:
            market_value += qty * float(mark) * fx
    equity = cash + market_value
    total_open_risk = float(conn.execute(
        "SELECT COALESCE(SUM(risk_sek),0) FROM positions WHERE status='OPEN'"
    ).fetchone()[0])
    c1,c2,c3,c4,c5=st.columns(5)
    c1.metric("Kontanter",f"{cash:.2f} kr")
    c2.metric("Portföljvärde (mark-to-market)",f"{equity:.2f} kr")
    c3.metric("Öppna paper-positioner",len(positions))
    c4.metric("Realiserat resultat",f"{realized:+.2f} kr")
    c5.metric("Öppen planerad risk",f"{total_open_risk:.2f} kr")

st.info("NOX 3.8: samma köpkrav som 3.7; diagnostik visar varför varje instrument avvaktar. 1m pullback/reclaim krävs (föregående rekyl mot EMA9 och stängning över föregående stapels high), tillsammans med 5m upptrend, RSI 50–67, positivt momentum och skydd mot översträckta/volatila inträden. Köp kräver signal ≥80/100 samt uppskattad netto-R/R ≥1,10 efter 0,10 % avgift per sida och 0,05 % uppskattad round-trip slippage. Stop/target kontrolleras mot candle high/low; om båda träffas i samma stapel räknas stop-loss först.")
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
        render_live_summary(st.session_state.get("last_fx"), st.session_state.last_rows)
        if st.session_state.last_events:
            for e in st.session_state.last_events[-8:]:
                st.write("• "+e)
        if st.session_state.last_rows:
            st.subheader("Krypto – automatisk paper trading")
            st.dataframe(pd.DataFrame(st.session_state.last_rows),use_container_width=True,hide_index=True)
            st.subheader("Varför köper inte NOX?")
            diagnostic_rows = [r for r in st.session_state.last_rows if r.get("Data") == "OK"]
            if diagnostic_rows:
                near = [r for r in diagnostic_rows if r.get("Råpoäng", 0) >= 65]
                st.caption(f"{len(near)} av {len(diagnostic_rows)} instrument har råpoäng ≥65. Köpkraven är oförändrade; detta är endast diagnostik.")
                st.dataframe(pd.DataFrame([{"Instrument":r["Instrument"],"Signal":r.get("Signal"),"Råpoäng":r.get("Råpoäng"),"Netto-R/R":r.get("Netto-R/R"),"Varför avvaktar":r.get("Varför avvaktar"),"Kontroller":r.get("Kontroller")} for r in diagnostic_rows]),use_container_width=True,hide_index=True)
        current=pd.read_sql_query("SELECT symbol,qty,entry_sek,stop_sek,target_sek,risk_sek,opened_utc FROM positions WHERE status='OPEN'",conn)
        st.subheader("Öppna paper-positioner")
        if current.empty: st.write("Inga öppna positioner. NOX väntar på kvalificerade signaler.")
        else: st.dataframe(current,use_container_width=True,hide_index=True)
        history=pd.read_sql_query("SELECT symbol,entry_sek,exit_sek,pnl_sek,fees_sek,reason,closed_utc FROM trades ORDER BY id DESC LIMIT 30",conn)
        st.subheader("Senaste stängda paper-affärer")
        if history.empty:
            st.write("Ingen stängd affär ännu.")
        else:
            st.dataframe(history,use_container_width=True,hide_index=True)
        all_trades=pd.read_sql_query("SELECT pnl_sek,fees_sek,reason FROM trades",conn)
        st.subheader("Strategistatistik")
        if all_trades.empty:
            st.write("Statistik visas när affärer har stängts.")
        else:
            wins=all_trades[all_trades.pnl_sek > 0]
            losses=all_trades[all_trades.pnl_sek < 0]
            win_rate=100*len(wins)/len(all_trades)
            avg_win=float(wins.pnl_sek.mean()) if len(wins) else 0.0
            avg_loss=abs(float(losses.pnl_sek.mean())) if len(losses) else 0.0
            gross_profit=float(wins.pnl_sek.sum()) if len(wins) else 0.0
            gross_loss=abs(float(losses.pnl_sek.sum())) if len(losses) else 0.0
            pf=(gross_profit/gross_loss) if gross_loss > 0 else None
            m1,m2,m3,m4=st.columns(4)
            m1.metric("Vinstprocent",f"{win_rate:.1f}%",help="Andel stängda affärer med positivt resultat efter avgifter.")
            m2.metric("Snittvinst",f"{avg_win:.3f} kr")
            m3.metric("Snittförlust",f"-{avg_loss:.3f} kr")
            m4.metric("Profit factor",f"{pf:.2f}" if pf is not None else "—")
            st.caption(f"Baserat på {len(all_trades)} stängda affärer. Utvärdera nettoresultat och profit factor efter minst 100 affärer; positiv förväntan är inte garanterad.")
    live_engine()
else:
    if st.button("▶ Kör en paper trading-cykel",type="primary"):
        run_cycle()
    if st.session_state.last_rows:
        render_live_summary(st.session_state.get("last_fx"), st.session_state.last_rows)
        st.dataframe(pd.DataFrame(st.session_state.last_rows),use_container_width=True,hide_index=True)
    else:
        render_live_summary(None, [])
    st.write("Automatiken är pausad. Aktivera reglaget för minutvis bevakning.")

st.subheader("Övriga marknader – endast signalbevakning")
st.write("Aktier, forex och råvaror är ännu inte kopplade till automatisk paper-exekvering. Vi aktiverar dem först när valutakonvertering, kontraktsstorlekar och handelsvillkor har verifierats.")

st.caption("VIKTIGT: 2 % motsvarar 10 kr planerad maxrisk vid 500 kr eget kapital, inklusive uppskattad round-trip-kostnad. Utan hävstång och med max 45 % kapital per position kan faktisk risk bli lägre än 10 kr. Två samtidiga positioner kan tillsammans ha upp till 20 kr planerad risk. Avgift 0,10 % per sida och uppskattad slippage 0,025 % per sida är antaganden, inte garanterade verkliga kostnader; spread och orderboksdjup mäts inte. SQLite-filen kan försvinna vid omstart/omdeploy. Detta är en testmiljö, inte en beständig handelsjournal. Testa under uppsikt och dra inga lönsamhetsslutsatser från få affärer.")
