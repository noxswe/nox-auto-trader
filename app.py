import streamlit as st
import pandas as pd
import requests
import sqlite3
from datetime import datetime, timezone

st.set_page_config(page_title="NOX AUTO TRADER 3.0", page_icon="📈", layout="wide")
st.title("📈 NOX AUTO TRADER 3.0")
st.caption("Balanced scalping · Paper trading · 500 SEK · No leverage")
st.warning("SIMULERING ENDAST. Ingen riktig handel. Detta är en experimentell prototyp, inte en bevisat lönsam strategi. Gratis Streamlit garanterar inte kontinuerlig bakgrundsdrift.")

DB="nox_v3_paper.db"
conn=sqlite3.connect(DB, check_same_thread=False)
conn.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), cash REAL, realized REAL)")
conn.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, market TEXT, entry REAL, exit REAL, qty REAL, pnl_sek REAL, status TEXT, opened TEXT, closed TEXT, reason TEXT)")
if conn.execute("SELECT COUNT(*) FROM account").fetchone()[0]==0:
    conn.execute("INSERT INTO account VALUES (1,500,0)")
conn.commit()

BINANCE="https://api.binance.com/api/v3/klines"
YAHOO="https://query1.finance.yahoo.com/v8/finance/chart/"
ASSETS=[
("BTCUSDT","Krypto","binance"),("ETHUSDT","Krypto","binance"),("SOLUSDT","Krypto","binance"),("BNBUSDT","Krypto","binance"),("XRPUSDT","Krypto","binance"),
("AAPL","Aktie","yahoo"),("MSFT","Aktie","yahoo"),("NVDA","Aktie","yahoo"),("AMZN","Aktie","yahoo"),("GOOGL","Aktie","yahoo"),("META","Aktie","yahoo"),("TSLA","Aktie","yahoo"),("JPM","Aktie","yahoo"),("AVGO","Aktie","yahoo"),("LLY","Aktie","yahoo"),
("EURUSD=X","Forex","yahoo"),("GBPUSD=X","Forex","yahoo"),("USDJPY=X","Forex","yahoo"),
("GC=F","Råvara","yahoo"),("SI=F","Råvara","yahoo"),("CL=F","Råvara","yahoo")]

def get_data(symbol, source):
    if source=="binance":
        r=requests.get(BINANCE,params={"symbol":symbol,"interval":"1m","limit":100},timeout=12)
        r.raise_for_status()
        d=pd.DataFrame(r.json(),columns=["t","o","h","l","c","v","ct","q","n","tb","tq","ig"])
        d["close"]=pd.to_numeric(d["c"],errors="coerce")
        d["volume"]=pd.to_numeric(d["v"],errors="coerce")
        return d.dropna(subset=["close"])
    r=requests.get(YAHOO+requests.utils.quote(symbol,safe=""),params={"range":"1d","interval":"1m"},headers={"User-Agent":"Mozilla/5.0"},timeout=12)
    r.raise_for_status()
    result=r.json()["chart"]["result"]
    if not result: raise ValueError("Ingen intradagsdata")
    q=result[0]["indicators"]["quote"][0]
    d=pd.DataFrame({"close":q.get("close"),"volume":q.get("volume")}).dropna(subset=["close"])
    if len(d)<35: raise ValueError("För få 1-minutsstaplar")
    return d

def signal(d):
    c=d.close
    e9=c.ewm(span=9,adjust=False).mean()
    e21=c.ewm(span=21,adjust=False).mean()
    delta=c.diff()
    gain=delta.clip(lower=0).rolling(14).mean()
    loss=-delta.clip(upper=0).rolling(14).mean()
    rsi=100-100/(1+gain/loss.replace(0,float("nan")))
    vol=d.volume.fillna(0)
    score=0
    if e9.iloc[-1]>e21.iloc[-1]: score+=40
    if c.iloc[-1]>e9.iloc[-1]: score+=20
    if 50<=rsi.iloc[-1]<=68: score+=25
    if vol.iloc[-1]>=vol.tail(20).median(): score+=15
    return float(c.iloc[-1]),int(score),float(rsi.iloc[-1])

cash,realized=conn.execute("SELECT cash,realized FROM account WHERE id=1").fetchone()
trades=pd.read_sql_query("SELECT * FROM trades WHERE status='OPEN'",conn)
a,b,c,d=st.columns(4)
a.metric("Kontanter",f"{cash:.2f} kr")
b.metric("Realiserat resultat",f"{realized:+.2f} kr")
c.metric("Öppna positioner",len(trades))
d.metric("Riskbudget", "5,00 kr / affär")
st.info("Scalpingprofil: 1-minutsdata, EMA 9/21, RSI 14 och volymfilter. Balanserad signalgräns: 80/100. Appen skannar endast när du trycker på knappen; den är inte en 24/7-bakgrundsbot.")

if st.button("🔎 Skanna alla marknader nu",type="primary"):
    results=[]
    bar=st.progress(0)
    for i,(symbol,market,source) in enumerate(ASSETS):
        try:
            data=get_data(symbol,source)
            price,score,rsi=signal(data)
            status="Signal" if score>=80 else "Avvakta"
            results.append({"Instrument":symbol,"Marknad":market,"Senaste pris":price,"Signal":score,"RSI":round(rsi,1),"Bedömning":status,"Datastatus":"OK"})
        except Exception as e:
            results.append({"Instrument":symbol,"Marknad":market,"Senaste pris":None,"Signal":None,"RSI":None,"Bedömning":"—","Datastatus":str(e)[:65]})
        bar.progress((i+1)/len(ASSETS))
    st.session_state["scan"]=pd.DataFrame(results)
    st.session_state["scan_time"]=datetime.now().strftime("%Y-%m-%d %H:%M:%S")

if "scan" in st.session_state:
    st.caption("Senaste skanning: "+st.session_state["scan_time"])
    st.dataframe(st.session_state["scan"],use_container_width=True,hide_index=True)
else:
    st.write("Ingen skanning genomförd ännu.")

st.subheader("Paper trading")
st.write("Denna första 3.0-version skannar och rangordnar signaler men öppnar inte automatiska positioner ännu. Det är avsiktligt: korrekt SEK-konvertering, spread, avgifter, stop-loss och risk per instrument måste verifieras innan automatiska scalping-affärer aktiveras.")
st.subheader("Historik")
hist=pd.read_sql_query("SELECT * FROM trades ORDER BY id DESC LIMIT 100",conn)
if hist.empty: st.write("Ingen handelshistorik ännu.")
else: st.dataframe(hist,use_container_width=True,hide_index=True)
st.caption("Datakällor: Binance public 1m candles för krypto; Yahoo Finance inofficiella 1m chart endpoint för övriga marknader. Data kan saknas, vara fördröjd eller blockeras. SQLite på gratis molnlagring är inte garanterat beständig och kan rensas vid omstart/deploy.")
