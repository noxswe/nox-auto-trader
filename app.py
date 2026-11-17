import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timezone
import time

st.set_page_config(page_title="NOX AUTO TRADER 3.2", page_icon="📈", layout="wide")
st.title("📈 NOX AUTO TRADER 3.2")
st.caption("Crypto-first balanced scalping · Live refresh each minute · Paper scanner only · 500 SEK · No leverage")
st.warning("SIMULERING ENDAST. Inga riktiga order. Prototypen visar signaler men öppnar inte affärer. Streamlit Cloud kan sova eller begränsa bakgrundskörning; minutuppdatering gäller bara medan app-sessionen är aktiv.")

HEADERS = {"User-Agent": "Mozilla/5.0 NOX-Auto-Trader/3.2"}
OKX_URL = "https://www.okx.com/api/v5/market/candles"
BYBIT_URL = "https://api.bybit.com/v5/market/kline"
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

def get_okx(inst):
    r=requests.get(OKX_URL,params={"instId":inst,"bar":"1m","limit":"100"},headers=HEADERS,timeout=8)
    r.raise_for_status()
    j=r.json()
    if j.get("code")!="0" or not j.get("data"):
        raise ValueError("OKX: "+str(j.get("msg","ingen data")))
    d=pd.DataFrame(j["data"],columns=["ts","open","high","low","close","volume","vol_ccy","vol_quote","confirm"])
    for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
    return d.sort_values("ts").dropna(subset=["close"]).reset_index(drop=True),"OKX"

def get_bybit(symbol):
    r=requests.get(BYBIT_URL,params={"category":"spot","symbol":symbol,"interval":"1","limit":100},headers=HEADERS,timeout=8)
    r.raise_for_status()
    j=r.json()
    if j.get("retCode")!=0 or not j.get("result",{}).get("list"):
        raise ValueError("Bybit: "+str(j.get("retMsg","ingen data")))
    d=pd.DataFrame(j["result"]["list"],columns=["ts","open","high","low","close","volume","turnover"])
    for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
    return d.sort_values("ts").dropna(subset=["close"]).reset_index(drop=True),"Bybit"

def get_crypto(inst,symbol):
    errors=[]
    for fn in (lambda:get_okx(inst),lambda:get_bybit(symbol)):
        try: return fn()
        except Exception as e: errors.append(str(e)[:80])
    raise RuntimeError("OKX och Bybit misslyckades: "+" | ".join(errors))

def get_yahoo(symbol):
    r=requests.get(YAHOO_URL+requests.utils.quote(symbol,safe=""),
        params={"range":"1d","interval":"1m"},headers=HEADERS,timeout=8)
    r.raise_for_status()
    chart=r.json().get("chart",{})
    if chart.get("error") or not chart.get("result"): raise ValueError("Yahoo saknar intradagsdata")
    result=chart["result"][0]
    q=result["indicators"]["quote"][0]
    ts=result.get("timestamp") or []
    d=pd.DataFrame({k:q.get(k) for k in ["open","high","low","close","volume"]})
    d["ts"]=ts[:len(d)] if ts else list(range(len(d)))
    d=d.dropna(subset=["close"]).reset_index(drop=True)
    if len(d)<35: raise ValueError("För få 1-minutsstaplar")
    return d,"Yahoo Finance"

def signal(d):
    c=d.close
    e9=c.ewm(span=9,adjust=False).mean()
    e21=c.ewm(span=21,adjust=False).mean()
    delta=c.diff()
    gain=delta.clip(lower=0).rolling(14).mean()
    loss=-delta.clip(upper=0).rolling(14).mean()
    rsi=100-100/(1+gain/loss.replace(0,float("nan")))
    vol=d.volume.fillna(0) if "volume" in d else pd.Series([0]*len(d))
    score=0
    if e9.iloc[-1]>e21.iloc[-1]: score+=40
    if c.iloc[-1]>e9.iloc[-1]: score+=20
    if pd.notna(rsi.iloc[-1]) and 50<=rsi.iloc[-1]<=68: score+=25
    if vol.iloc[-1]>=vol.tail(20).median(): score+=15
    candle=str(d.ts.iloc[-1]) if "ts" in d else "unknown"
    return float(c.iloc[-1]),int(score),float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None,candle

def scan_crypto():
    rows=[]
    for inst,symbol in CRYPTO:
        try:
            d,provider=get_crypto(inst,symbol)
            price,score,rsi,candle=signal(d)
            rows.append({"Instrument":symbol,"Marknad":"Krypto","Datakälla":provider,
                "Senaste pris":price,"Signal":score,"RSI":round(rsi,1) if rsi is not None else None,
                "Bedömning":"Stark signal" if score>=80 else "Avvakta","Senaste 1m-stapel":candle,"Datastatus":"OK"})
        except Exception as e:
            rows.append({"Instrument":symbol,"Marknad":"Krypto","Datakälla":"—","Senaste pris":None,
                "Signal":None,"RSI":None,"Bedömning":"—","Senaste 1m-stapel":"—","Datastatus":str(e)[:120]})
    return rows

def scan_other():
    rows=[]
    for symbol,market in OTHER:
        try:
            d,provider=get_yahoo(symbol)
            price,score,rsi,candle=signal(d)
            rows.append({"Instrument":symbol,"Marknad":market,"Datakälla":provider,
                "Senaste pris":price,"Signal":score,"RSI":round(rsi,1) if rsi is not None else None,
                "Bedömning":"Stark signal" if score>=80 else "Avvakta","Senaste 1m-stapel":candle,"Datastatus":"OK"})
        except Exception as e:
            rows.append({"Instrument":symbol,"Marknad":market,"Datakälla":"—","Senaste pris":None,
                "Signal":None,"RSI":None,"Bedömning":"—","Senaste 1m-stapel":"—","Datastatus":str(e)[:120]})
    return rows

if "scan_data" not in st.session_state: st.session_state.scan_data=[]
if "last_crypto_minute" not in st.session_state: st.session_state.last_crypto_minute=""
if "last_other_minute" not in st.session_state: st.session_state.last_other_minute=""
if "last_scan_display" not in st.session_state: st.session_state.last_scan_display="Ingen skanning ännu"

with st.sidebar:
    st.header("Inställningar")
    st.metric("Simulerat startkapital","500 kr")
    st.metric("Riskbudget (mål)","5 kr per affär")
    st.caption("Ingen hävstång · Inga riktiga order")
    auto=st.toggle("Automatisk uppdatering varje minut",value=True)
    st.caption("Krypto skannas varje minut. Aktier, forex och råvaror skannas var femte minut.")
    st.caption("Uppdatering fungerar medan app-sessionen är aktiv. Molnet kan försätta appen i viloläge.")

def render_scan():
    now=datetime.now(timezone.utc)
    minute=now.strftime("%Y-%m-%d %H:%M")
    do_crypto=(st.session_state.last_crypto_minute!=minute)
    # Non-crypto markets are refreshed every fifth minute to limit provider requests.
    do_other=(now.minute%5==0 and st.session_state.last_other_minute!=minute)
    if do_crypto:
        st.session_state.scan_data=[r for r in st.session_state.scan_data if r.get("Marknad")!="Krypto"]+scan_crypto()
        st.session_state.last_crypto_minute=minute
    if do_other:
        st.session_state.scan_data=[r for r in st.session_state.scan_data if r.get("Marknad")=="Krypto"]+scan_other()
        st.session_state.last_other_minute=minute
    st.session_state.last_scan_display=now.strftime("%Y-%m-%d %H:%M:%S UTC")
    df=pd.DataFrame(st.session_state.scan_data)
    if not df.empty:
        st.subheader("Krypto – huvudmarknad (uppdateras varje minut)")
        st.dataframe(df[df["Marknad"]=="Krypto"],use_container_width=True,hide_index=True)
        st.subheader("Övriga marknader (uppdateras var femte minut)")
        st.dataframe(df[df["Marknad"]!="Krypto"],use_container_width=True,hide_index=True)
        ok=int(((df["Marknad"]=="Krypto")&(df["Datastatus"]=="OK")).sum())
        st.metric("Kryptoinstrument med fungerande data",f"{ok}/5")
    st.caption("Senaste uppdateringscykel: "+st.session_state.last_scan_display)

st.info("Marknadsprioritet: Krypto → Aktier → Forex → Råvaror. Varje ny minut analyseras bara den senaste tillgängliga 1-minutsstapeln. Denna version visar signaler men öppnar inga paper trades automatiskt.")
if auto:
    @st.fragment(run_every="60s")
    def live_panel():
        render_scan()
    live_panel()
else:
    if st.button("🔎 Skanna nu",type="primary"):
        render_scan()
    elif st.session_state.scan_data:
        df=pd.DataFrame(st.session_state.scan_data)
        st.dataframe(df,use_container_width=True,hide_index=True)
    else:
        st.info("Slå på automatisk uppdatering eller tryck på Skanna nu.")

st.subheader("Paper trading")
st.write("Automatiska paper trades är avstängda tills positionsstorlek i SEK, valutakonvertering, spread, avgifter och stop-loss har verifierats. Signaluppdatering är inte samma sak som orderexekvering.")
st.caption("Datakällor: OKX publika 1m candles med Bybit som reserv för krypto; Yahoo Finances inofficiella 1m chart endpoint för övriga marknader. Data kan saknas/fördröjas. Streamlit Cloud är inte en garanterad 24/7-bakgrundstjänst.")
