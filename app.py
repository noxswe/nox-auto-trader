import json, time
from pathlib import Path
from datetime import datetime
import requests
import numpy as np
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

APP_DIR = Path(__file__).resolve().parent
STATE_FILE = APP_DIR / "paper_state_v2.json"
START_CASH = 500.0
RISK_FRACTION = 0.01
MAX_POSITIONS = 4
MIN_SIGNAL_SCORE = 80
SIM_FEE = 0.001
MIN_TRADE_SEK = 20.0
SCAN_MS = 15 * 60 * 1000

st.set_page_config(page_title="NOX AUTO TRADER 2.0", page_icon="📈", layout="wide")

def now():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=20, headers={"User-Agent":"Mozilla/5.0 NOX-AUTO-TRADER/2.0"})
    r.raise_for_status()
    return r.json()

def usdsek():
    data=get_json("https://open.er-api.com/v6/latest/USD")
    rate=float(data["rates"]["SEK"])
    if not 5 < rate < 20: raise ValueError("Ogiltig USD/SEK-kurs")
    return rate

def top_crypto():
    data=get_json("https://api.coinpaprika.com/v1/tickers", {"quotes":"USD"})
    stable={"USDT","USDC","DAI","FDUSD","USDE","USDS","USDD","TUSD","PYUSD","USDP","EURC"}
    rows=[x for x in data if isinstance(x.get("rank"),int) and x["rank"]>0 and x.get("symbol","").upper() not in stable]
    rows.sort(key=lambda x:x["rank"])
    return [(x["symbol"].upper(),x["name"],"crypto", "binance") for x in rows[:10]]

def yahoo_history(symbol, range_="1y", interval="1d"):
    data=get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                  {"range":range_,"interval":interval,"events":"history"})
    result=data["chart"]["result"][0]
    q=result["indicators"]["quote"][0]
    df=pd.DataFrame({"close":q["close"],"high":q["high"],"low":q["low"],"volume":q.get("volume")})
    df=df.dropna(subset=["close"]).reset_index(drop=True)
    for c in ["close","high","low"]:
        df[c]=pd.to_numeric(df[c],errors="coerce")
    df["volume"]=pd.to_numeric(df["volume"],errors="coerce").fillna(0)
    return df.dropna(subset=["close"])

def crypto_history(symbol):
    raw=get_json("https://api.binance.com/api/v3/klines",
                 {"symbol":symbol+"USDT","interval":"1d","limit":250})
    if not isinstance(raw,list) or len(raw)<205: raise ValueError("Otillräcklig historik eller handelspar saknas")
    df=pd.DataFrame(raw,columns=["t","o","h","l","c","v","ct","qv","n","tb","tq","i"])
    return pd.DataFrame({"close":pd.to_numeric(df.c),"high":pd.to_numeric(df.h),
                         "low":pd.to_numeric(df.l),"volume":pd.to_numeric(df.v)})

def indicators(df):
    d=df.copy()
    c=d.close
    d["ema20"]=c.ewm(span=20,adjust=False).mean()
    d["ema50"]=c.ewm(span=50,adjust=False).mean()
    d["ema200"]=c.ewm(span=200,adjust=False).mean()
    delta=c.diff()
    gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    d["rsi"]=100-100/(1+gain/loss.replace(0,np.nan))
    m=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    d["macd"]=m; d["macds"]=m.ewm(span=9,adjust=False).mean()
    d["volavg"]=d.volume.rolling(20).mean()
    tr=pd.concat([d.high-d.low,(d.high-c.shift()).abs(),(d.low-c.shift()).abs()],axis=1).max(axis=1)
    d["atr"]=tr.rolling(14).mean()
    return d.dropna()

def analyze(df):
    d=indicators(df)
    if len(d)<205: raise ValueError("Behöver minst 205 dagsstaplar")
    x=d.iloc[-2] # last completed candle
    checks=[
      ("Pris över EMA200",x.close>x.ema200,20),
      ("EMA20 över EMA50",x.ema20>x.ema50,15),
      ("RSI 48–70",48<=x.rsi<=70,15),
      ("MACD positiv",x.macd>x.macds,15),
      ("Volym över snitt",x.volume>x.volavg*1.05,10),
      ("Positivt momentum",x.close>d.iloc[-3].close,10),
      ("ATR inom rimlig nivå",0.002< x.atr/x.close <0.08,15)
    ]
    score=sum(p for _,ok,p in checks if ok)
    mandatory=checks[0][1] and checks[1][1] and checks[3][1] and checks[6][1]
    return {"score":int(score),"price":float(x.close),"atr":float(x.atr),
            "rsi":float(x.rsi),"mandatory":bool(mandatory),
            "stop":float(x.close-2*x.atr),"target":float(x.close+3*x.atr),
            "checks":checks}

def load_state():
    if STATE_FILE.exists():
        try:
            s=json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if s.get("version")==2: return s
        except Exception: pass
    return {"version":2,"cash":START_CASH,"positions":{},"trades":[],"last_scan":None,"start_cash":START_CASH}

def save(): STATE_FILE.write_text(json.dumps(st.session_state.paper,indent=2),encoding="utf-8")

def instruments():
    out=[]
    errors=[]
    try: out.extend(top_crypto())
    except Exception as e: errors.append("Krypto-ranking: "+str(e)[:120])
    stocks=["AAPL","MSFT","NVDA","AMZN","GOOGL","META","BRK-B","LLY","AVGO","JPM",
            "TSLA","WMT","XOM","V","MA","COST","ORCL","NFLX","JNJ","HD"]
    out.extend([(x,x,"stocks","yahoo") for x in stocks])
    out.extend([("EURUSD=X","EUR/USD","forex","yahoo"),("GBPUSD=X","GBP/USD","forex","yahoo"),
                ("USDJPY=X","USD/JPY","forex","yahoo")])
    out.extend([("GC=F","Gold futures","commodities","yahoo"),("SI=F","Silver futures","commodities","yahoo"),
                ("CL=F","WTI crude futures","commodities","yahoo")])
    return out,errors

def fetch_instrument(item, fx):
    symbol,name,market,provider=item
    df=crypto_history(symbol) if market=="crypto" else yahoo_history(symbol)
    a=analyze(df)
    # All displayed portfolio prices are SEK-equivalent. Forex prices are converted as USD quote approximations.
    if market=="crypto":
        a["price_sek"]=a["price"]*fx
        a["stop_sek"]=a["stop"]*fx
        a["target_sek"]=a["target"]*fx
    elif market=="forex":
        # Yahoo FX symbols are quote currency per base; approximate notional SEK per base unit.
        quote="JPY" if symbol=="USDJPY=X" else "USD"
        mult=(fx/ a["price"]) if quote=="JPY" else fx
        a["price_sek"]=a["price"]*mult
        a["stop_sek"]=a["stop"]*mult
        a["target_sek"]=a["target"]*mult
    else:
        a["price_sek"]=a["price"]*fx
        a["stop_sek"]=a["stop"]*fx
        a["target_sek"]=a["target"]*fx
    a["market"]=market
    a["name"]=name
    a["symbol"]=symbol
    return a

def close_pos(sym, price, reason):
    s=st.session_state.paper; p=s["positions"].get(sym)
    if not p:return
    gross=p["qty"]*price; fee=gross*SIM_FEE; proceeds=gross-fee
    pnl=proceeds-p["cost"]
    s["cash"]+=proceeds
    s["trades"].append({"time":now(),"market":p["market"],"symbol":sym,"side":"SELL",
                        "price_sek":price,"value_sek":gross,"fee_sek":fee,"pnl_sek":pnl,"reason":reason})
    del s["positions"][sym]

def scan():
    fx=usdsek()
    items,errs=instruments()
    results=[]
    for item in items:
        try:
            a=fetch_instrument(item,fx)
            results.append(a)
        except Exception as e:
            results.append({"symbol":item[0],"name":item[1],"market":item[2],
                            "status":"Data saknas","error":str(e)[:110],"score":0})
    s=st.session_state.paper
    # Manage open positions on fresh data.
    lookup={x["symbol"]:x for x in results}
    for sym in list(s["positions"]):
        a=lookup.get(sym); p=s["positions"][sym]
        if not a or "price_sek" not in a: continue
        px=a["price_sek"]; p["last_price"]=px
        if px<=p["stop"]: close_pos(sym,px,"Stop-loss (simulerad)")
        elif px>=p["target"]: close_pos(sym,px,"Målkurs (simulerad)")
        elif a["score"]<50: close_pos(sym,px,"Signal försvagad (simulerad)")
    # One shared portfolio; only highest-scoring eligible instruments can open.
    equity=s["cash"]+sum(p["qty"]*p["last_price"] for p in s["positions"].values())
    for a in sorted(results,key=lambda z:z.get("score",0),reverse=True):
        if len(s["positions"])>=MAX_POSITIONS: break
        if a.get("status")=="Data saknas" or a.get("score",0)<MIN_SIGNAL_SCORE or not a.get("mandatory"): continue
        sym=a["symbol"]
        if sym in s["positions"]: continue
        px=a["price_sek"]; stop=a["stop_sek"]
        stop_dist=max(px-stop,px*0.01)
        risk_budget=equity*RISK_FRACTION
        qty=min(risk_budget/stop_dist,(s["cash"]*0.95)/(px*(1+SIM_FEE)))
        gross=qty*px; fee=gross*SIM_FEE; total=gross+fee
        if gross<MIN_TRADE_SEK or total>s["cash"]: continue
        s["cash"]-=total
        s["positions"][sym]={"qty":qty,"entry":px,"last_price":px,"stop":stop,
             "target":a["target_sek"],"cost":total,"market":a["market"],"opened":now()}
        s["trades"].append({"time":now(),"market":a["market"],"symbol":sym,"side":"BUY",
             "price_sek":px,"value_sek":gross,"fee_sek":fee,"pnl_sek":0,
             "reason":f"Signal {a['score']}/100"})
    st.session_state.results=results
    st.session_state.data_errors=errs
    s["last_scan"]=now(); save()

if "paper" not in st.session_state: st.session_state.paper=load_state()
if "results" not in st.session_state: st.session_state.results=[]
if "data_errors" not in st.session_state: st.session_state.data_errors=[]

st.title("📈 NOX AUTO TRADER 2.0")
st.caption("Multi-Market Edition · Windows · Paper trading · Ingen börsnycklar · Inga riktiga order")
st.warning("SIMULERING ENDAST. Alla fyra marknader bevakas med publika datakällor där de fungerar. Data kan vara fördröjd eller saknas. Ingen livehandel är möjlig i denna version.")

with st.sidebar:
    st.header("Inställningar")
    st.write("**Simulerat startkapital:** 500 kr")
    st.write("**Risk per trade:** 1 % (5 kr vid start)")
    st.write("**Signalgräns:** 80/100")
    st.write("**Max öppna positioner:** 4")
    st.write("**Simulerad avgift:** 0,10 % per sida")
    st.write("**Skanning:** manuellt eller var 15:e minut")
    auto=st.toggle("Automatisk skanning var 15:e minut",False)
    st.caption("Datorn och appen måste vara igång. Automatisk skanning följer inte marknaden när datorn är avstängd.")
    if st.button("Nollställ paper-konto"):
        st.session_state.paper={"version":2,"cash":START_CASH,"positions":{},"trades":[],"last_scan":None,"start_cash":START_CASH}
        st.session_state.results=[]; save(); st.rerun()

if st.button("🔄 Skanna alla marknader nu",type="primary"):
    with st.spinner("Analyserar krypto, aktier, forex och råvaror..."):
        try: scan()
        except Exception as e: st.error(f"Skanningen kunde inte slutföras: {e}")

if auto:
    tick=st_autorefresh(interval=SCAN_MS,key="nox_v2_refresh")
    if tick>0:
        with st.spinner("Automatisk marknadsskanning..."):
            try: scan()
            except Exception as e: st.error(f"Automatisk skanning misslyckades: {e}")

s=st.session_state.paper
pos=s["positions"]
value=sum(p["qty"]*p["last_price"] for p in pos.values())
equity=s["cash"]+value
realized=sum(t.get("pnl_sek",0) for t in s["trades"] if t["side"]=="SELL")
c=st.columns(4)
c[0].metric("Portföljvärde",f"{equity:,.2f} kr",f"{equity-s['start_cash']:+,.2f} kr")
c[1].metric("Kontanter",f"{s['cash']:,.2f} kr")
c[2].metric("Öppna positioner",len(pos))
c[3].metric("Realiserat resultat",f"{realized:+,.2f} kr")

st.subheader("Marknadsöversikt")
if st.session_state.results:
    df=pd.DataFrame([{"Marknad":x["market"].capitalize(),"Instrument":x["name"],
        "Symbol":x["symbol"],"Signal":x.get("score",0),
        "Pris (SEK-ekv.)":round(x["price_sek"],4) if "price_sek" in x else "—",
        "Status":"Analyserad" if "price_sek" in x else "Data saknas"} for x in st.session_state.results])
    st.dataframe(df.sort_values(["Marknad","Signal"],ascending=[True,False]),use_container_width=True,hide_index=True)
else:
    st.info("Ingen skanning ännu. Klicka på 'Skanna alla marknader nu'.")

st.subheader("Marknadsstatus")
if st.session_state.results:
    for market in ["crypto","stocks","forex","commodities"]:
        subset=[x for x in st.session_state.results if x["market"]==market]
        ok=sum("price_sek" in x for x in subset)
        st.write(f"**{market.capitalize()}:** {ok}/{len(subset)} instrument med fungerande data")

st.subheader("Öppna simulerade positioner")
if pos:
    st.dataframe(pd.DataFrame([{"Marknad":p["market"],"Symbol":k,"Inköp (SEK)":p["entry"],
       "Senaste (SEK)":p["last_price"],"Stop-loss (SEK)":p["stop"],"Målkurs (SEK)":p["target"],
       "Orealiserat resultat (kr)":p["qty"]*p["last_price"]-p["cost"]} for k,p in pos.items()]),
       use_container_width=True,hide_index=True)
else: st.write("Inga öppna positioner. Boten väntar på kvalificerade signaler.")

st.subheader("Senaste simulerade affärer")
if s["trades"]: st.dataframe(pd.DataFrame(s["trades"][-30:][::-1]),use_container_width=True,hide_index=True)
else: st.write("Inga trades ännu.")

st.subheader("Varför avstår boten?")
if st.session_state.results:
    for a in sorted(st.session_state.results,key=lambda z:z.get("score",0),reverse=True):
        if "price_sek" in a and (a["score"]<MIN_SIGNAL_SCORE or not a["mandatory"]):
            failed=[n for n,ok,p in a["checks"] if not ok]
            st.write(f"**{a['name']} ({a['score']}/100):** "+(", ".join(failed) if failed else "Signalgränsen är inte uppnådd."))
if st.session_state.data_errors:
    with st.expander("Datakällor och fel"):
        for e in st.session_state.data_errors: st.write(e)
if st.session_state.results:
    failed=[x for x in st.session_state.results if "price_sek" not in x]
    if failed:
        with st.expander(f"Instrument utan data ({len(failed)})"):
            for x in failed: st.write(f"**{x['market']} – {x['symbol']}:** {x.get('error','Okänt fel')}")
st.caption(f"Senaste skanning: {s['last_scan'] or 'Inte startad'}")
st.caption("Data: CoinPaprika, Binance public market data, Yahoo Finance chart endpoint och öppen USD/SEK-referenskurs. Yahoo Finance-gränssnittet är inofficiellt och kan ändras eller begränsas.")
st.caption("En simulering är inte bevis på lönsamhet. Avgifter, spread, slippage, utdelningar, futures-rollover och exekvering kan skilja sig från verklig handel. Ingen automatisk riktig handel finns i denna version.")
