import requests
import time
import os
import json
import pickle
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from statistics import mean, stdev
from threading import Thread
from flask import Flask

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Rome")
except Exception:
    TZ = None

def adesso():
    return datetime.now(TZ) if TZ else datetime.now()

# ---------------------------------------------------------
# CONFIGURAZIONE
# ---------------------------------------------------------
def _env(nome):
    return os.environ.get(nome, "").strip().strip('"').strip("'").strip()

TELEGRAM_TOKEN     = _env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = _env("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = _env("TWELVEDATA_API_KEY")

SYMBOLS             = ["EUR/USD", "GBP/USD"]
SALDO_INIZIALE      = 100.0
RISCHIO_BASE        = 0.02
SESSIONE_START      = 9
SESSIONE_END        = 22
SPREAD_BUFFER       = 1.5
SOGLIA_APPROVAZIONE = 7
MONITOR_MIN         = 1
TIMEOUT_SEGNALE_SEC = 300
SESSIONI_OTTIMALI   = [(9,11),(14,16),(16,18)]

FILE_STORICO  = "storico_saldo.txt"
FILE_STATO    = "stato_bot.json"
FILE_SETTIMANA = "storico_settimana.json"   # NUOVO

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False

# ---------------------------------------------------------
# PERSISTENZA STATO
# ---------------------------------------------------------
def carica_stato():
    default = {"saldo_virtuale": 107.89,
               "stats": {"vinti":18,"persi":13,"pareggi":0,"totali":31}}
    if os.path.exists(FILE_STATO):
        try:
            with open(FILE_STATO,"r") as f: return json.load(f)
        except Exception: pass
    return default

def salva_stato():
    try:
        with open(FILE_STATO,"w") as f:
            json.dump({"saldo_virtuale":saldo_virtuale,"stats":stats},f)
    except Exception as e:
        print("Errore salvataggio: {}".format(e),flush=True)

_stato         = carica_stato()
saldo_virtuale = _stato["saldo_virtuale"]
stats          = _stato["stats"]
last_update_id       = -1
ultimo_heartbeat_ora = -1
macd_memoria         = {}
pausa_bot_fino       = None

trade_attivo = {
    "aperto":False,"symbol":None,"direction":None,"entrata":None,
    "sl":None,"tp":None,"be_fatto":False,"ora_entrata":None,
    "atr":0.0015,"size":0.01,"in_attesa_risultato":False,"step":None
}
segnale_in_attesa = {
    "attivo":False,"timestamp_generazione":None,"data_trade":None
}

if not os.path.exists(FILE_STORICO):
    with open(FILE_STORICO,"w") as f: f.write("100.0\n108.58\n")

# ---------------------------------------------------------
# FLASK
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    ora = adesso().strftime("%H:%M:%S")
    wr  = (stats["vinti"]/stats["totali"]*100) if stats["totali"]>0 else 0
    return ("FOREX AGENT ONLINE\nOra: {}\nSaldo: {:.2f} EUR\n"
            "Win Rate: {:.1f}%\nTrade: {}\nTrade aperto: {}".format(
            ora,saldo_virtuale,wr,stats["totali"],
            trade_attivo["symbol"] if trade_attivo["aperto"] else "Nessuno")),200

@app.route('/test')
def test_tg():
    righe=[]
    righe.append("TELEGRAM_TOKEN   : {}".format(
        "MANCANTE" if not TELEGRAM_TOKEN else
        "presente, {} char, inizia con {}...".format(len(TELEGRAM_TOKEN),TELEGRAM_TOKEN[:6])))
    righe.append("TELEGRAM_CHAT_ID : {}".format(TELEGRAM_CHAT_ID or "MANCANTE"))
    righe.append("Thread bot vivo  : {}".format(bot_thread is not None and bot_thread.is_alive()))
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        righe.append("\nERRORE: imposta le variabili d'ambiente su Render")
        return "\n".join(righe),200
    base="https://api.telegram.org/bot{}".format(TELEGRAM_TOKEN)
    passi=[
        ("getMe",         lambda: requests.get(base+"/getMe",timeout=10)),
        ("getWebhookInfo",lambda: requests.get(base+"/getWebhookInfo",timeout=10)),
        ("getChat",       lambda: requests.get(base+"/getChat",params={"chat_id":TELEGRAM_CHAT_ID},timeout=10)),
        ("sendMessage",   lambda: requests.post(base+"/sendMessage",
                          data={"chat_id":TELEGRAM_CHAT_ID,"text":"test dal bot"},timeout=10)),
    ]
    for nome,fn in passi:
        try:
            r=fn()
            righe.append("\n[{}] {} {}".format(nome,r.status_code,r.text[:400]))
        except Exception as e:
            righe.append("\n[{}] ERRORE rete: {}".format(nome,e))
    return "\n".join(righe),200

def run_flask():
    port=int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0",port=port)

# ---------------------------------------------------------
# MODELLO ML
# ---------------------------------------------------------
ML_MODEL=None; ML_FEATURES=None

def carica_modello():
    global ML_MODEL,ML_FEATURES
    try:
        with open("forex_model.pkl","rb") as f:
            data=pickle.load(f)
            ML_MODEL=data["model"]; ML_FEATURES=data["features"]
        print("Modello ML caricato",flush=True); return True
    except Exception as e:
        print("Modello ML non disponibile: {}".format(e),flush=True); return False

def predici_ml(closes,highs,lows,opens):
    if ML_MODEL is None or len(closes)<30: return None,0.0
    try:
        c=pd.Series(closes);h=pd.Series(highs);l=pd.Series(lows);o=pd.Series(opens)
        ema8=c.ewm(span=8,adjust=False).mean()
        ema21=c.ewm(span=21,adjust=False).mean()
        ema50=c.ewm(span=50,adjust=False).mean()
        ema200=c.ewm(span=200,adjust=False).mean() if len(closes)>=200 else ema50
        d=c.diff()
        rsi=100-100/(1+d.clip(lower=0).rolling(14).mean()/((-d.clip(upper=0)).rolling(14).mean()+1e-10))
        rsi_f=100-100/(1+d.clip(lower=0).rolling(7).mean()/((-d.clip(upper=0)).rolling(7).mean()+1e-10))
        ml_s=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
        macd_h=ml_s-ml_s.ewm(span=9,adjust=False).mean()
        ma20=c.rolling(20).mean();sd20=c.rolling(20).std()
        bu=ma20+2*sd20;bl=ma20-2*sd20
        bb_w=(bu-bl)/(ma20+1e-10)*100;bb_pos=(c-bl)/(bu-bl+1e-10)
        bb_sq=bb_w.rolling(20).min()/(bb_w+1e-10)
        tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
        a14=tr.rolling(14).mean();a7=tr.rolling(7).mean();a30=a14.rolling(30).mean()
        high50=h.rolling(50).max();low50=l.rolling(50).min()
        pos50=(c-low50)/(high50-low50+1e-10)
        vel3=c.diff(3)/(a14+1e-10);vel10=c.diff(10)/(a14+1e-10)
        up=d.clip(lower=0).rolling(10).sum();dn=(-d.clip(upper=0)).rolling(10).sum()
        udr=up/(dn+1e-10)
        low14=l.rolling(14).min();high14=h.rolling(14).max()
        stk=(c-low14)/(high14-low14+1e-10)*100
        feat_map={
            'rsi':float(rsi.iloc[-1]),'rsi_f':float(rsi_f.iloc[-1]),
            'macd_h':float(macd_h.iloc[-1]),'bb_w':float(bb_w.iloc[-1]),
            'bb_pos':float(bb_pos.iloc[-1]),'bb_sq':float(bb_sq.iloc[-1]),
            'p_ema50':float((c-ema50).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'p_ema200':float((c-ema200).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'p_ema8':float((c-ema8).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'p_ema21':float((c-ema21).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'ema_sp':float((ema50-ema200).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'ema821':float((ema8-ema21).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'body':float((c-o).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'crange':float((h-l).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'ush':float((h-pd.concat([c,o],axis=1).max(axis=1)).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'lsh':float((pd.concat([c,o],axis=1).min(axis=1)-l).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'mom3':float(c.pct_change(3).iloc[-1]*100),
            'mom5':float(c.pct_change(5).iloc[-1]*100),
            'mom10':float(c.pct_change(10).iloc[-1]*100),
            'mom20':float(c.pct_change(20).iloc[-1]*100) if len(closes)>=21 else 0.0,
            'vol_rat':float(a14.iloc[-1]/(a30.iloc[-1]+1e-10)),
            'vol_ratf':float(a7.iloc[-1]/(a14.iloc[-1]+1e-10)),
            'rsi_slope':float(rsi.diff(3).iloc[-1]),
            'macd_slope':float(macd_h.diff(3).iloc[-1]),
            'vol_trend':float(a14.pct_change(5).iloc[-1]*100),
            'price_acc':float((c.pct_change(3)-c.pct_change(3).shift(3)).iloc[-1]),
            'stoch_k':float(stk.iloc[-1]),
            'vel3':float(vel3.iloc[-1]),'vel10':float(vel10.iloc[-1]),
            'pos50':float(pos50.iloc[-1]),'udr':float(udr.iloc[-1]),
        }
        row=[feat_map.get(f,0.0) for f in ML_FEATURES] if ML_FEATURES else list(feat_map.values())
        if any(np.isnan(v) for v in row): return None,0.0
        X=np.array([row])
        proba=ML_MODEL.predict_proba(X)[0]; pred=int(ML_MODEL.predict(X)[0])
        conf=float(proba[pred])*100
        if conf<55.0: return "NESSUNO",conf
        return {0:"NESSUNO",1:"LONG",2:"SHORT"}[pred],conf
    except Exception as e:
        print("Errore ML: {}".format(e),flush=True); return None,0.0

# ---------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TG: credenziali mancanti",flush=True); return False
    url="https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
    pezzi=[msg[i:i+4000] for i in range(0,len(msg),4000)] or [""]
    tutto_ok=True
    for pezzo in pezzi:
        inviato=False
        for tentativo in range(3):
            try:
                r=requests.post(url,data={"chat_id":TELEGRAM_CHAT_ID,"text":pezzo,
                                          "parse_mode":"Markdown"},timeout=10)
                if r.status_code==200:
                    print("TG OK: {}".format(pezzo[:60].replace("\n"," ")),flush=True)
                    inviato=True; break
                print("TG ERRORE {}: {}".format(r.status_code,r.text),flush=True)
                if r.status_code==429:
                    attesa=int(r.json().get("parameters",{}).get("retry_after",2))
                    time.sleep(min(attesa,30)); continue
                if r.status_code==400 and "parse" in r.text.lower():
                    r2=requests.post(url,data={"chat_id":TELEGRAM_CHAT_ID,"text":pezzo},timeout=10)
                    print("TG retry plain: {} {}".format(r2.status_code,r2.text[:100]),flush=True)
                    if r2.status_code==200: inviato=True
                    break
                if r.status_code in (400,401,403,404): break
            except Exception as e:
                print("Errore TG (t{}): {}".format(tentativo+1,e),flush=True)
                time.sleep(2)
        tutto_ok=tutto_ok and inviato
    return tutto_ok

def send_telegram_foto(photo_path,caption):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    url="https://api.telegram.org/bot{}/sendPhoto".format(TELEGRAM_TOKEN)
    try:
        with open(photo_path,'rb') as photo:
            r=requests.post(url,data={"chat_id":TELEGRAM_CHAT_ID,"caption":caption,
                                      "parse_mode":"Markdown"},files={"photo":photo},timeout=15)
        if r.status_code==200: return True
        print("TG foto ERRORE {}: {}".format(r.status_code,r.text),flush=True)
    except Exception as e:
        print("Errore TG foto: {}".format(e),flush=True)
    send_telegram(caption); return False

def leggi_messaggio_telegram():
    global last_update_id
    try:
        url="https://api.telegram.org/bot{}/getUpdates".format(TELEGRAM_TOKEN)
        r=requests.get(url,params={"offset":last_update_id+1,"timeout":2},timeout=8).json()
        if not r.get("ok"):
            if r.get("error_code")==401:
                print("TOKEN NON VALIDO (401). Aggiorna TELEGRAM_TOKEN su Render.",flush=True)
                time.sleep(60)
            else:
                print("getUpdates errore: {}".format(r),flush=True); time.sleep(5)
            return None
        for update in r.get("result",[]):
            last_update_id=update["update_id"]
            try:
                chat_id=str(update["message"]["chat"]["id"])
                testo=update["message"]["text"]
                if chat_id==str(TELEGRAM_CHAT_ID): return testo
            except Exception: pass
    except Exception as e:
        print("Errore getUpdates: {}".format(e),flush=True)
    return None

# ---------------------------------------------------------
# GRAFICO & REPORT
# ---------------------------------------------------------
def genera_e_invia_grafico(testo_report):
    if not HAS_MATPLOTLIB:
        send_telegram(testo_report); return
    try:
        with open(FILE_STORICO,"r") as f:
            saldi=[float(l.strip()) for l in f if l.strip()]
        plt.figure(figsize=(8,4))
        plt.plot(saldi,marker='o',color='#007AFF',linewidth=2,label="Equity Line")
        plt.axhline(y=SALDO_INIZIALE,color='red',linestyle='--',alpha=0.5,label="Inizio")
        plt.title("Crescita del Capitale"); plt.xlabel("Numero Trade")
        plt.ylabel("EUR"); plt.grid(True,linestyle='--',alpha=0.6); plt.legend()
        plt.savefig("equity.png",bbox_inches='tight',dpi=150); plt.close()
        send_telegram_foto("equity.png",testo_report)
    except Exception as e:
        print("Errore grafico: {}".format(e),flush=True); send_telegram(testo_report)

def invia_report():
    wr=(stats["vinti"]/stats["totali"]*100) if stats["totali"]>0 else 0
    profitto=saldo_virtuale-SALDO_INIZIALE
    p_str="+{:.2f}".format(profitto) if profitto>=0 else "{:.2f}".format(profitto)
    msg=("*DIARIO DI TRADING*\n"
         "-------------------------\n"
         "Saldo    : *{:.2f} EUR*\n"
         "Profitto : *{} EUR*\n"
         "Win Rate : *{:.1f}%*\n"
         "-------------------------\n"
         "Vinti    : {}\n"
         "Persi    : {}\n"
         "Pareggi  : {}\n"
         "Totali   : {}").format(
        saldo_virtuale,p_str,wr,
        stats["vinti"],stats["persi"],stats["pareggi"],stats["totali"])
    genera_e_invia_grafico(msg)

# ---------------------------------------------------------
# STORICO SETTIMANALE (NUOVO)
# ---------------------------------------------------------
def carica_storico_settimana():
    if os.path.exists(FILE_SETTIMANA):
        try:
            with open(FILE_SETTIMANA,"r") as f: return json.load(f)
        except Exception: pass
    return {"settimane":[]}

def salva_trade_settimana(profit,direction,symbol):
    dati=carica_storico_settimana()
    sw_key=adesso().strftime("%Y-W%V")
    sw=next((s for s in dati["settimane"] if s["settimana"]==sw_key),None)
    if sw is None:
        sw={"settimana":sw_key,"trades":[],"pnl":0.0,"vinti":0,"persi":0}
        dati["settimane"].append(sw)
    sw["trades"].append({"profit":round(profit,2),"dir":direction,
                         "sym":symbol.replace("/",""),
                         "ora":adesso().strftime("%d/%m %H:%M")})
    sw["pnl"]=round(sw["pnl"]+profit,2)
    if profit>0.02: sw["vinti"]+=1
    elif profit<-0.02: sw["persi"]+=1
    try:
        with open(FILE_SETTIMANA,"w") as f: json.dump(dati,f)
    except Exception as e:
        print("Errore salv. settimana: {}".format(e),flush=True)

def genera_report_settimanale():
    dati=carica_storico_settimana()
    if not dati["settimane"]:
        return "*Nessun dato settimanale ancora disponibile*"
    msg="*REPORT SETTIMANALE*\n"+"="*24+"\n\n"
    for sw in dati["settimane"][-4:]:
        tot=sw["vinti"]+sw["persi"]
        wr=sw["vinti"]/tot*100 if tot>0 else 0
        pnl=sw["pnl"]
        pnl_str="+{:.2f}".format(pnl) if pnl>=0 else "{:.2f}".format(pnl)
        msg+="*Settimana {}*\n".format(sw["settimana"])
        msg+="P&L    : *{} EUR*\n".format(pnl_str)
        msg+="WinRate: *{:.1f}%* ({}/{})\n".format(wr,sw["vinti"],tot)
        for t in sw["trades"][-5:]:
            sg="+" if t["profit"]>=0 else ""
            msg+="  {} {} {} {}{:.2f}\n".format(
                t["ora"],t["sym"],t["dir"],sg,t["profit"])
        msg+="\n"
    return msg

# ---------------------------------------------------------
# PIVOT POINTS SETTIMANALI (NUOVO)
# ---------------------------------------------------------
def calcola_pivot_points(symbol):
    try:
        url="https://api.twelvedata.com/time_series"
        r=requests.get(url,params={"symbol":symbol,"interval":"1week",
                                   "outputsize":3,"apikey":TWELVEDATA_API_KEY},timeout=15).json()
        if "values" not in r: return None
        vals=list(reversed(r["values"]))
        if len(vals)<2: return None
        prev=vals[-2]
        H=float(prev["high"]); L=float(prev["low"]); C=float(prev["close"])
        PP=(H+L+C)/3
        return {"PP":PP,"R1":2*PP-L,"R2":PP+(H-L),"R3":H+2*(PP-L),
                "S1":2*PP-H,"S2":PP-(H-L),"S3":L-2*(H-PP)}
    except Exception as e:
        print("Pivot error {}: {}".format(symbol,e),flush=True); return None

# ---------------------------------------------------------
# FILTRO CORRELAZIONE (NUOVO)
# ---------------------------------------------------------
def check_correlazione():
    dirs={}
    for symbol in SYMBOLS:
        closes,_=fetch_candles(symbol,"1h",outputsize=60)
        if closes is None: continue
        ema50=compute_ema(closes,50)
        if ema50 is None: continue
        dirs[symbol]="LONG" if closes[-1]>ema50 else "SHORT"
    if len(dirs)<2: return True,"N/D (dati insufficienti)"
    d_eu=dirs.get("EUR/USD"); d_gb=dirs.get("GBP/USD")
    if d_eu==d_gb: return True,"Correlati ({})".format(d_eu)
    return False,"DIVERGENTI EU:{} GB:{}".format(d_eu,d_gb)

# ---------------------------------------------------------
# REPORT DETTAGLIATO (NUOVO)
# ---------------------------------------------------------
def genera_report_dettagliato():
    wr=(stats["vinti"]/stats["totali"]*100) if stats["totali"]>0 else 0
    profitto=saldo_virtuale-SALDO_INIZIALE
    p_str="+{:.2f}".format(profitto) if profitto>=0 else "{:.2f}".format(profitto)

    msg="*REPORT DETTAGLIATO*\n"+"="*24+"\n\n"
    msg+="*Conto complessivo*\n"
    msg+="Saldo    : *{:.2f} EUR*\n".format(saldo_virtuale)
    msg+="Profitto : *{} EUR*\n".format(p_str)
    msg+="Win Rate : *{:.1f}%*\n".format(wr)
    msg+="Trade    : {} (V:{} P:{} Par:{})\n\n".format(
        stats["totali"],stats["vinti"],stats["persi"],stats["pareggi"])

    # settimana corrente
    dati_sw=carica_storico_settimana()
    sw_key=adesso().strftime("%Y-W%V")
    sw=next((s for s in dati_sw["settimane"] if s["settimana"]==sw_key),None)
    if sw:
        tot_sw=sw["vinti"]+sw["persi"]
        wr_sw=sw["vinti"]/tot_sw*100 if tot_sw>0 else 0
        pnl=sw["pnl"]
        pnl_str="+{:.2f}".format(pnl) if pnl>=0 else "{:.2f}".format(pnl)
        msg+="*Settimana corrente*\n"
        msg+="P&L     : *{} EUR*\n".format(pnl_str)
        msg+="Win Rate: *{:.1f}%* ({}/{})\n\n".format(wr_sw,sw["vinti"],tot_sw)

    # pivot points
    msg+="*Pivot Points settimanali*\n"
    for symbol in SYMBOLS:
        pp=calcola_pivot_points(symbol)
        if pp:
            msg+="{}: PP={:.5f}\n  R1={:.5f} R2={:.5f}\n  S1={:.5f} S2={:.5f}\n".format(
                symbol.replace("/",""),pp["PP"],pp["R1"],pp["R2"],pp["S1"],pp["S2"])
        else:
            msg+="{}: N/D\n".format(symbol.replace("/",""))

    # correlazione
    corr_ok,corr_msg=check_correlazione()
    msg+="\n*Correlazione*: {}\n".format(corr_msg)
    msg+="Stato: {}\n".format("OK" if corr_ok else "ATTENZIONE divergenza")
    return msg

# ---------------------------------------------------------
# REGISTRA RISULTATO (aggiornato con storico settimanale)
# ---------------------------------------------------------
def registra_risultato(testo):
    global saldo_virtuale,stats,trade_attivo
    testo=testo.strip().replace(",",".")
    try: profit=float(testo)
    except Exception:
        send_telegram("Formato non riconosciuto.\n\n+1.50=guadagno\n-1.50=perdita\n0=pareggio")
        return False
    saldo_virtuale+=profit; stats["totali"]+=1
    direction=trade_attivo.get("direction","N/D")
    symbol=trade_attivo.get("symbol","N/D")
    if profit>0.02: stats["vinti"]+=1; emoji="VINTO"
    elif profit<-0.02: stats["persi"]+=1; emoji="PERSO"
    else: stats["pareggi"]+=1; emoji="PAREGGIO"
    salva_stato()
    salva_trade_settimana(profit,direction,symbol)   # NUOVO
    with open(FILE_STORICO,"a") as f: f.write("{:.2f}\n".format(saldo_virtuale))
    segno="+" if profit>=0 else ""
    send_telegram("Registrato: *{}{} EUR* - {}\nSaldo: *{:.2f} EUR*".format(
        segno,profit,emoji,saldo_virtuale))
    invia_report()
    trade_attivo["aperto"]=False
    trade_attivo["in_attesa_risultato"]=False
    trade_attivo["step"]=None
    return True

# ---------------------------------------------------------
# CONTROLLI MERCATO
# ---------------------------------------------------------
def is_mercato_aperto():
    n=adesso(); g=n.weekday(); o=n.hour
    if g==4 and o>=23: return False
    if g==5: return False
    if g==6 and o<23: return False
    return True

def is_sessione_base():
    if pausa_bot_fino and adesso()<pausa_bot_fino: return False
    return SESSIONE_START<=adesso().hour<SESSIONE_END

def in_sessione_ottimale():
    ora=adesso().hour
    for s,e in SESSIONI_OTTIMALI:
        if s<=ora<e: return True,"{:02d}:00-{:02d}:00".format(s,e)
    return False,""

def check_news_block():
    ora=adesso()
    return ora.hour in [10,11,14,15,16] and ora.minute<15

# ---------------------------------------------------------
# FETCH DATI
# ---------------------------------------------------------
def fetch_candles(symbol,interval,outputsize=100):
    try:
        r=requests.get("https://api.twelvedata.com/time_series",
                       params={"symbol":symbol,"interval":interval,
                               "outputsize":outputsize,"apikey":TWELVEDATA_API_KEY},
                       timeout=15).json()
        if "values" not in r:
            print("TwelveData {} {}: {}".format(symbol,interval,str(r)[:150]),flush=True)
            return None,None
        raw=list(reversed(r["values"])); candles=[]
        for v in raw:
            try:
                candles.append({"open":float(v["open"]),"high":float(v["high"]),
                                 "low":float(v["low"]),"close":float(v["close"])})
            except Exception: continue
        closes=[c["close"] for c in candles]
        if len(closes)<outputsize*0.6: return None,None
        return closes,candles
    except Exception as e:
        print("Errore fetch {}: {}".format(symbol,e),flush=True); return None,None

# ---------------------------------------------------------
# INDICATORI
# ---------------------------------------------------------
def compute_ema(prices,period):
    if len(prices)<period: return mean(prices) if prices else None
    k=2/(period+1); ema=mean(prices[:period])
    for p in prices[period:]: ema=p*k+ema*(1-k)
    return ema

def compute_macd_veloce(closes,symbol,fast=12,slow=26,signal=9):
    if len(closes)<slow: return 0,0,0
    fast_ema=compute_ema(closes,fast); slow_ema=compute_ema(closes,slow)
    macd_line=fast_ema-slow_ema
    if symbol not in macd_memoria: macd_memoria[symbol]=[]
    macd_memoria[symbol].append(macd_line)
    if len(macd_memoria[symbol])>50: macd_memoria[symbol].pop(0)
    signal_line=compute_ema(macd_memoria[symbol],signal)
    if signal_line is None: signal_line=macd_line
    return macd_line,signal_line,macd_line-signal_line

def compute_atr(candles,period=14):
    if len(candles)<period+1: return None
    trs=[]
    for i in range(1,len(candles)):
        tr=max(candles[i]["high"]-candles[i]["low"],
               abs(candles[i]["high"]-candles[i-1]["close"]),
               abs(candles[i]["low"]-candles[i-1]["close"]))
        trs.append(tr)
    return mean(trs[-period:])

def compute_rsi(closes,period=14):
    if len(closes)<period+1: return 50
    deltas=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    gains=[d if d>0 else 0 for d in deltas[-period:]]
    losses=[-d if d<0 else 0 for d in deltas[-period:]]
    avg_gain=mean(gains) if gains else 0
    avg_loss=mean(losses) if losses else 0
    if avg_loss==0: return 100
    return 100-(100/(1+avg_gain/avg_loss))

def compute_bollinger(closes,period=20):
    if len(closes)<period: return None,None,None
    recent=closes[-period:]; ma=mean(recent)
    try: sd=stdev(recent)
    except Exception: return None,None,None
    return ma+2*sd,ma,ma-2*sd

def get_sr(candles,lookback=100):
    if len(candles)<lookback: lookback=len(candles)
    recent=candles[-lookback:]
    return min(c["low"] for c in recent),max(c["high"] for c in recent)

def detect_pattern(candles,direction,atr):
    if len(candles)<3 or not atr: return 0,"Nessun pattern"
    c1=candles[-2]; c2=candles[-3]
    corpo1=abs(c1["close"]-c1["open"])
    ombra_sup1=c1["high"]-max(c1["close"],c1["open"])
    ombra_inf1=min(c1["close"],c1["open"])-c1["low"]
    if direction=="LONG":
        if ombra_inf1>=corpo1*2 and ombra_sup1<=corpo1*0.5 and corpo1>=atr*0.1:
            return 3,"Hammer"
        if (c1["close"]>c1["open"] and c2["close"]<c2["open"] and
                c1["close"]>c2["open"] and c1["open"]<c2["close"]):
            return 3,"Bullish Engulfing"
        if corpo1<atr*0.1 and ombra_inf1>=atr*0.3: return 1,"Doji rialzista"
    elif direction=="SHORT":
        if ombra_sup1>=corpo1*2 and ombra_inf1<=corpo1*0.5 and corpo1>=atr*0.1:
            return 3,"Shooting Star"
        if (c1["close"]<c1["open"] and c2["close"]>c2["open"] and
                c1["close"]<c2["open"] and c1["open"]>c2["close"]):
            return 3,"Bearish Engulfing"
        if corpo1<atr*0.1 and ombra_sup1>=atr*0.3: return 1,"Doji ribassista"
    return 0,"Nessun pattern"

# ---------------------------------------------------------
# MATRICE SCORE + ML (aggiornata con correlazione e pivot)
# ---------------------------------------------------------
def calcola_matrice(symbol):
    closes_15m,candles_15m=fetch_candles(symbol,"15min",outputsize=100)
    if closes_15m is None: return None,"Dati 15m non disponibili"
    closes_1h,_=fetch_candles(symbol,"1h",outputsize=220)
    if closes_1h is None: return None,"Dati 1H non disponibili"
    closes_4h,_=fetch_candles(symbol,"4h",outputsize=40)

    price=closes_15m[-1]
    ema50_15m=compute_ema(closes_15m,50)
    ema50_1h=compute_ema(closes_1h,50)
    ema200_1h=compute_ema(closes_1h,200) if len(closes_1h)>=200 else None
    ema20_4h=compute_ema(closes_4h,20) if closes_4h else compute_ema(closes_1h,80)
    atr=compute_atr(candles_15m); rsi=compute_rsi(closes_15m)

    if not all([ema50_15m,ema50_1h,atr,rsi]): return None,"Indicatori non calcolabili"

    bb_upper,_,bb_lower=compute_bollinger(closes_15m)
    if bb_upper and bb_lower:
        if (bb_upper-bb_lower)*10000<8.0: return None,"BB Squeeze - volatilita bassa"

    direction=None
    if price>ema50_15m and price>ema50_1h: direction="LONG"
    elif price<ema50_15m and price<ema50_1h: direction="SHORT"
    if direction is None: return None,"Trend 15m/1H non allineato"

    if ema20_4h:
        if direction=="LONG" and price<ema20_4h: return None,"Contro trend 4H"
        if direction=="SHORT" and price>ema20_4h: return None,"Contro trend 4H"
    if ema200_1h:
        if direction=="LONG" and price<ema200_1h: return None,"Sotto EMA200 H1"
        if direction=="SHORT" and price>ema200_1h: return None,"Sopra EMA200 H1"
    if direction=="LONG" and rsi>75: return None,"RSI ipercomprato {:.1f}".format(rsi)
    if direction=="SHORT" and rsi<25: return None,"RSI ipervenduto {:.1f}".format(rsi)

    # FILTRO CORRELAZIONE (NUOVO)
    corr_ok,corr_msg=check_correlazione()
    if not corr_ok:
        return None,"Correlazione divergente: {}".format(corr_msg)

    if direction=="LONG":
        sl=price-atr*1.5-(SPREAD_BUFFER/10000)
        tp=price+atr*2.5+(SPREAD_BUFFER/10000)
    else:
        sl=price+atr*1.5+(SPREAD_BUFFER/10000)
        tp=price-atr*2.5-(SPREAD_BUFFER/10000)

    pip_sl=abs(price-sl)*10000; pip_tp=abs(price-tp)*10000
    if pip_sl<10: return None,"SL troppo stretto ({:.1f} pip)".format(pip_sl)
    if pip_tp<15: return None,"TP troppo stretto ({:.1f} pip)".format(pip_tp)

    supporto,resistenza=get_sr(candles_15m)
    if supporto and resistenza:
        if direction=="LONG" and (resistenza-price)*10000<5:
            return None,"Troppo vicino a resistenza"
        if direction=="SHORT" and (price-supporto)*10000<5:
            return None,"Troppo vicino a supporto"

    # PIVOT POINTS come filtro aggiuntivo (NUOVO)
    pp_data=calcola_pivot_points(symbol)
    pivot_msg="N/D"
    if pp_data:
        pp=pp_data["PP"]
        if direction=="LONG":
            # Preferibile essere vicino a S1/S2 (supporto da pivot)
            dist_s1=abs(price-pp_data["S1"])*10000
            pivot_msg="Vicino S1 ({:.0f}pip)".format(dist_s1) if dist_s1<30 else "PP={:.5f}".format(pp)
        else:
            dist_r1=abs(price-pp_data["R1"])*10000
            pivot_msg="Vicino R1 ({:.0f}pip)".format(dist_r1) if dist_r1<30 else "PP={:.5f}".format(pp)

    bb_ok=False; bb_msg="Dentro bande"
    if bb_upper and bb_lower:
        if direction=="LONG" and price<=bb_lower*1.001:
            bb_ok=True; bb_msg="Banda inferiore OK"
        elif direction=="SHORT" and price>=bb_upper*0.999:
            bb_ok=True; bb_msg="Banda superiore OK"

    _,_,macd_hist=compute_macd_veloce(closes_15m,symbol)
    macd_ok=False; macd_msg="MACD N/D"
    if macd_hist!=0:
        if direction=="LONG" and macd_hist>0: macd_ok=True; macd_msg="MACD rialzista"
        elif direction=="SHORT" and macd_hist<0: macd_ok=True; macd_msg="MACD ribassista"
        else: macd_msg="MACD contro"

    punti_pattern,nome_pattern=detect_pattern(candles_15m,direction,atr)
    sess_ok,sess_nome=in_sessione_ottimale()
    rr=pip_tp/pip_sl if pip_sl>0 else 0

    punti=3
    atr_list=[compute_atr(candles_15m[max(0,i-14):i],14) or atr
              for i in range(max(14,len(candles_15m)-30),len(candles_15m))]
    atr_mean=mean(atr_list) if atr_list else atr
    r_atr=atr/(atr_mean+1e-10)
    if r_atr>=1.3: punti+=3
    elif r_atr>=1.1: punti+=2
    elif r_atr>=0.9: punti+=1

    if rr>=2.0: punti+=2
    elif rr>=1.5: punti+=1
    if direction=="LONG" and 40<rsi<65: punti+=1
    elif direction=="SHORT" and 35<rsi<60: punti+=1
    if bb_ok: punti+=2
    if macd_ok: punti+=2
    if sess_ok: punti+=2
    punti+=punti_pattern

    # Bonus pivot vicino a livello chiave
    if pp_data:
        if direction=="LONG" and abs(price-pp_data["S1"])*10000<20: punti+=1
        elif direction=="SHORT" and abs(price-pp_data["R1"])*10000<20: punti+=1

    opens=[c["open"] for c in candles_15m]
    highs=[c["high"] for c in candles_15m]
    lows=[c["low"] for c in candles_15m]
    ml_pred,ml_conf=predici_ml(closes_15m,highs,lows,opens)
    ml_msg="ML N/D"
    if ml_pred is not None:
        if ml_pred==direction and ml_conf>=40:
            ml_bonus=3 if ml_conf>=60 else 2
            punti+=ml_bonus; ml_msg="ML {} {:.0f}%".format(ml_pred,ml_conf)
        elif ml_pred=="NESSUNO": ml_msg="ML: laterale"
        else: punti-=1; ml_msg="ML contro ({} {:.0f}%)".format(ml_pred,ml_conf)

    if punti<SOGLIA_APPROVAZIONE:
        return None,"Score insufficiente ({} punti)".format(punti)

    if punti>=14: score="A+"; molt=1.0
    elif punti>=10: score="A"; molt=0.75
    else: score="B"; molt=0.5

    if score=="B" and not sess_ok:
        return None,"Score B fuori sessione ottimale"

    rischio_eur=saldo_virtuale*RISCHIO_BASE*molt
    guadagno_pot=rischio_eur*rr
    units=rischio_eur/(atr*1.5)
    std=round(max(units/100000,0.01),2)
    be_level=price+atr*1.25 if direction=="LONG" else price-atr*1.25

    return {
        "symbol":symbol,"direction":direction,"price":price,
        "sl":sl,"tp":tp,"be_level":be_level,
        "pip_sl":pip_sl,"pip_tp":pip_tp,"rr":rr,"rsi":rsi,"atr":atr,
        "size":std,"rischio":rischio_eur,"guadagno":guadagno_pot,
        "score":score,"punti":punti,"molt":molt,
        "bb_msg":bb_msg,"macd_msg":macd_msg,"pattern":nome_pattern,
        "ml_msg":ml_msg,"sess_nome":sess_nome if sess_ok else "Sessione base",
        "supporto":supporto,"resistenza":resistenza,
        "pivot_msg":pivot_msg,"corr_msg":corr_msg
    },"OK"

# ---------------------------------------------------------
# ISTRUZIONI MT5
# ---------------------------------------------------------
def invia_istruzioni_entrata(signal):
    azione="COMPRA (Buy)" if signal["direction"]=="LONG" else "VENDI (Sell)"
    colore="BLU" if signal["direction"]=="LONG" else "ROSSO"
    msg=("*AGENTE - ISTRUZIONI ENTRATA*\n"
         "Segui questi passi su MT5:\n\n"
         "*STEP 1 - Apri nuovo ordine*\n"
         "Premi F9 oppure clicca\n'Crea Nuovo Ordine'\n\n"
         "*STEP 2 - Imposta i valori*\n"
         "Simbolo : *{}*\n"
         "Volume  : *{}* lotti\n"
         "S/L     : *{:.5f}*\n"
         "T/P     : *{:.5f}*\n\n"
         "*STEP 3 - Esegui*\n"
         "Clicca il pulsante *{}* ({})\n\n"
         "Prezzo attuale: `{:.5f}`\n"
         "Rischio: -{:.2f} EUR\n"
         "Obiettivo: +{:.2f} EUR\n\n"
         "Quando sei entrato scrivi *Entrato*").format(
        signal["symbol"].replace("/",""),signal["size"],
        signal["sl"],signal["tp"],azione,colore,
        signal["price"],signal["rischio"],signal["guadagno"])
    send_telegram(msg)

def invia_istruzioni_trailing(symbol,vecchio_sl,nuovo_sl):
    send_telegram(
        "*AGENTE - AGGIORNA STOP LOSS*\n\n"
        "Il trailing stop si e spostato!\n\n"
        "*Come aggiornare su MT5:*\n"
        "1. Clicca destro sul trade\n"
        "2. Seleziona Modifica ordine\n"
        "3. Cambia S/L da `{:.5f}`\n   a *{:.5f}*\n"
        "4. Clicca Modifica\n\nQuesto protegge il tuo profitto!".format(vecchio_sl,nuovo_sl))

def invia_istruzioni_breakeven(symbol,entrata):
    send_telegram(
        "*AGENTE - METTI IN BREAKEVEN*\n\n"
        "Hai raggiunto il 50% del TP!\n\n"
        "*Come fare su MT5:*\n"
        "1. Clicca destro sul trade\n"
        "2. Seleziona Modifica ordine\n"
        "3. Cambia S/L a *{:.5f}*\n"
        "4. Clicca Modifica\n\n"
        "Ora il trade non puo andare in perdita!".format(entrata))

def invia_istruzioni_chiusura(symbol,direction,motivo):
    send_telegram(
        "*AGENTE - CHIUDI IL TRADE*\n\n"
        "Motivo: {}\n\n"
        "*Come chiudere su MT5:*\n"
        "1. Vai nel pannello ordini\n"
        "2. Clicca la X accanto al trade {}\n"
        "3. Conferma la chiusura\n\n"
        "Poi scrivi il risultato:\n"
        "+X.XX guadagno | -X.XX perdita | 0 pareggio".format(motivo,symbol.replace("/","")))

# ---------------------------------------------------------
# TELEMETRIA
# ---------------------------------------------------------
def genera_telemetria():
    report="*TELEMETRIA AGENTE + ML*\n"+"="*24+"\n"
    report+="ML: {}\n\n".format("Attivo" if ML_MODEL else "Non disponibile")
    if pausa_bot_fino and adesso()<pausa_bot_fino:
        minuti=int((pausa_bot_fino-adesso()).total_seconds()/60)
        report+="PAUSA ({} min rimanenti)\n\n".format(minuti)
    elif segnale_in_attesa["attivo"]: report+="In attesa conferma entrata\n\n"
    elif trade_attivo["aperto"]: report+="Trade aperto: {}\n\n".format(trade_attivo["symbol"])
    for symbol in SYMBOLS:
        result,motivo=calcola_matrice(symbol)
        report+="Asset: *{}*\n".format(symbol)
        if result is None: report+="SKIP: {}\n\n".format(motivo)
        else:
            report+=("Direzione : {}\nPunti     : {}\nScore     : {}\n"
                     "RSI       : {:.1f}\nBB        : {}\nMACD      : {}\n"
                     "Pattern   : {}\nML        : {}\nPivot     : {}\n"
                     "Correlaz. : {}\nSessione  : {}\n\n").format(
                result["direction"],result["punti"],result["score"],
                result["rsi"],result["bb_msg"],result["macd_msg"],
                result["pattern"],result["ml_msg"],result.get("pivot_msg","N/D"),
                result.get("corr_msg","N/D"),result["sess_nome"])
    return report

# ---------------------------------------------------------
# MONITOR TRADE
# ---------------------------------------------------------
def monitora_trade():
    global trade_attivo
    if not trade_attivo["aperto"]: return
    symbol=trade_attivo["symbol"]; direction=trade_attivo["direction"]
    entrata=trade_attivo["entrata"]; sl=trade_attivo["sl"]; tp=trade_attivo["tp"]
    ora_entrata=trade_attivo["ora_entrata"]; atr=trade_attivo["atr"]
    closes,_=fetch_candles(symbol,"1min",outputsize=5)
    if closes is None: return
    prezzo=closes[-1]
    pip_profit=(prezzo-entrata)*10000 if direction=="LONG" else (entrata-prezzo)*10000
    print("Monitor {}: {:.5f} | {:+.1f} pip".format(symbol,prezzo,pip_profit),flush=True)
    if (direction=="LONG" and prezzo>=tp) or (direction=="SHORT" and prezzo<=tp):
        invia_istruzioni_chiusura(symbol,direction,"TARGET RAGGIUNTO!")
        send_telegram("*PROFIT!* +{:.1f} pip\n\nDopo aver chiuso scrivi il guadagno:\nEsempio: +2.50".format(abs(pip_profit)))
        trade_attivo["in_attesa_risultato"]=True; return
    if (direction=="LONG" and prezzo<=sl-0.00005) or (direction=="SHORT" and prezzo>=sl+0.00005):
        send_telegram("*STOP LOSS COLPITO* su {}\nLoss: {:.1f} pip\n\nScrivi la perdita:\nEsempio: -1.50".format(symbol,abs(pip_profit)))
        trade_attivo["in_attesa_risultato"]=True; return
    if pip_profit>0:
        if direction=="LONG":
            nuovo_sl=prezzo-atr*1.5
            if nuovo_sl>trade_attivo["sl"] and nuovo_sl>entrata:
                vecchio=trade_attivo["sl"]; trade_attivo["sl"]=nuovo_sl
                invia_istruzioni_trailing(symbol,vecchio,nuovo_sl)
        elif direction=="SHORT":
            nuovo_sl=prezzo+atr*1.5
            if nuovo_sl<trade_attivo["sl"] and nuovo_sl<entrata:
                vecchio=trade_attivo["sl"]; trade_attivo["sl"]=nuovo_sl
                invia_istruzioni_trailing(symbol,vecchio,nuovo_sl)
    if not trade_attivo["be_fatto"]:
        be_level=entrata+atr*1.25 if direction=="LONG" else entrata-atr*1.25
        if (direction=="LONG" and prezzo>=be_level) or (direction=="SHORT" and prezzo<=be_level):
            trade_attivo["sl"]=entrata; trade_attivo["be_fatto"]=True
            invia_istruzioni_breakeven(symbol,entrata)
    closes_15m,candles_15m=fetch_candles(symbol,"15min",outputsize=20)
    if closes_15m and len(closes_15m)>=3:
        rsi=compute_rsi(closes_15m); c1=candles_15m[-2]; c2=candles_15m[-3]
        inv=False; motivo_inv=""
        if direction=="LONG":
            if rsi>72: inv=True; motivo_inv="RSI ipercomprato {:.1f}".format(rsi)
            elif c1["close"]<c1["open"] and c2["close"]<c2["open"]:
                inv=True; motivo_inv="2 candele ribassiste"
        elif direction=="SHORT":
            if rsi<28: inv=True; motivo_inv="RSI ipervenduto {:.1f}".format(rsi)
            elif c1["close"]>c1["open"] and c2["close"]>c2["open"]:
                inv=True; motivo_inv="2 candele rialziste"
        if inv:
            if pip_profit>0:
                invia_istruzioni_chiusura(symbol,direction,"Possibile inversione: {}".format(motivo_inv))
                send_telegram("Sei in profitto di {:.1f} pip\nValuta se chiudere ora".format(abs(pip_profit)))
            else:
                send_telegram("*Possibile inversione* su {}\nMotivo: {}\nAspetta SL automatico".format(symbol,motivo_inv))
    if ora_entrata:
        minuti=int((adesso()-ora_entrata).total_seconds()//60)
        if minuti>=240 and pip_profit<=0:
            invia_istruzioni_chiusura(symbol,direction,"Trade aperto da 4 ore senza profitto")
            send_telegram("Loss attuale: {:.1f} pip\nValuta se chiudere manualmente".format(abs(pip_profit)))

# ---------------------------------------------------------
# ESEGUI ANALISI
# ---------------------------------------------------------
def esegui_analisi():
    global segnale_in_attesa
    if not is_mercato_aperto(): return
    if not is_sessione_base(): return
    if check_news_block():
        send_telegram("*FILTRO NEWS*\nRicerca sospesa 15 min"); return
    if trade_attivo["aperto"] or trade_attivo["in_attesa_risultato"]: return
    if segnale_in_attesa["attivo"]: return
    sess_ok,sess_nome=in_sessione_ottimale(); risultati=[]
    for symbol in SYMBOLS:
        print("Analisi {}...".format(symbol),flush=True)
        signal,motivo=calcola_matrice(symbol)
        if signal is None:
            risultati.append("{} SKIP: {}".format(symbol,motivo))
        else:
            if signal["score"]=="A+": label="A+ - SEGNALE FORTE"
            elif signal["score"]=="A": label="A - SEGNALE BUONO"
            else: label="B - VALUTA TU"
            direzione="LONG (COMPRA)" if signal["direction"]=="LONG" else "SHORT (VENDI)"
            msg=("*AGENTE - SEGNALE TROVATO*\n"
                 "Score: *{}* ({} punti)\n"
                 "Asset: *{}*\n"
                 "DIREZIONE: *{}*\n\n"
                 "Prezzo attuale: `{:.5f}`\n"
                 "Stop Loss     : `{:.5f}` ({:.1f} pip)\n"
                 "Take Profit   : `{:.5f}` ({:.1f} pip)\n"
                 "R/R           : 1:{:.2f}\n\n"
                 "Rischio  : -{:.2f} EUR\n"
                 "Obiettivo: +{:.2f} EUR\n\n"
                 "RSI    : {:.1f}\n"
                 "BB     : {}\n"
                 "MACD   : {}\n"
                 "Pattern: {}\n"
                 "ML     : {}\n"
                 "Pivot  : {}\n"
                 "Correl.: {}\n"
                 "Session: {}\n\n"
                 "Vuoi entrare? Scrivi *si* per le istruzioni\n"
                 "oppure *no* per saltare\n"
                 "Segnale scade in 5 minuti").format(
                label,signal["punti"],signal["symbol"],direzione,
                signal["price"],signal["sl"],signal["pip_sl"],
                signal["tp"],signal["pip_tp"],signal["rr"],
                signal["rischio"],signal["guadagno"],signal["rsi"],
                signal["bb_msg"],signal["macd_msg"],signal["pattern"],
                signal["ml_msg"],signal.get("pivot_msg","N/D"),
                signal.get("corr_msg","N/D"),signal["sess_nome"])
            send_telegram(msg)
            segnale_in_attesa.update({"attivo":True,
                "timestamp_generazione":time.time(),"data_trade":signal})
            return
    stato=("*AGENTE - {}*\n{}\n\n{}\n\nNessun segnale - prossima analisi tra 15 min").format(
        adesso().strftime("%H:%M"),
        "Sessione ottimale: {}".format(sess_nome) if sess_ok else "Sessione base",
        "\n".join(risultati))
    send_telegram(stato)

# ---------------------------------------------------------
# HEARTBEAT ORARIO + REPORT DOMENICALE (NUOVO)
# ---------------------------------------------------------
def invia_heartbeat():
    global ultimo_heartbeat_ora
    ora=adesso()
    if ora.hour==ultimo_heartbeat_ora: return
    ultimo_heartbeat_ora=ora.hour
    sess_ok,sess_nome=in_sessione_ottimale()
    stato_trade="Trade: *{}*".format(trade_attivo["symbol"]) if trade_attivo["aperto"] else "Nessun trade"

    # REPORT SETTIMANALE DOMENICA ALLE 20:00 (NUOVO)
    if ora.weekday()==6 and ora.hour==20:
        report_sw=genera_report_settimanale()
        send_telegram("*RIEPILOGO SETTIMANALE*\n\n"+report_sw)

    if not is_mercato_aperto():
        send_telegram("*Agente {:02d}:00*\nMercato CHIUSO\n{}\nSaldo: *{:.2f} EUR*".format(
            ora.hour,stato_trade,saldo_virtuale))
    elif not is_sessione_base():
        send_telegram("*Agente {:02d}:00*\nFuori sessione\n{}\nSaldo: *{:.2f} EUR*".format(
            ora.hour,stato_trade,saldo_virtuale))
    else:
        send_telegram("*Agente {:02d}:00*\n{}\n{}\nSaldo: *{:.2f} EUR*\nML: {}\n\n"
                      "Scrivi *filtri* per telemetria\nScrivi *report* per report dettagliato".format(
            ora.hour,
            "Sessione ottimale: {}".format(sess_nome) if sess_ok else "Sessione base",
            stato_trade,saldo_virtuale,"Attivo" if ML_MODEL else "N/D"))

# ---------------------------------------------------------
# BOT LOOP
# ---------------------------------------------------------
def bot_loop():
    global segnale_in_attesa,trade_attivo,pausa_bot_fino,saldo_virtuale
    print("FOREX AGENT AVVIATO",flush=True)
    mancanti=[n for n,v in (("TELEGRAM_TOKEN",TELEGRAM_TOKEN),
                             ("TELEGRAM_CHAT_ID",TELEGRAM_CHAT_ID),
                             ("TWELVEDATA_API_KEY",TWELVEDATA_API_KEY)) if not v]
    if mancanti:
        print("ATTENZIONE variabili mancanti: {}".format(", ".join(mancanti)),flush=True)
    try:
        requests.get("https://api.telegram.org/bot{}/deleteWebhook".format(TELEGRAM_TOKEN),timeout=10)
    except Exception: pass
    ml_ok=carica_modello()
    send_telegram(
        "*FOREX AGENT AVVIATO*\n*Render 24/7*\n\n"
        "Saldo: *{:.2f} EUR*\n"
        "Win Rate: *{:.1f}%* ({} trade)\n\n"
        "Come funziona:\n"
        "1. Agente analizza ogni 15 min\n"
        "2. Trova segnale → ti avvisa\n"
        "3. Scrivi SI → istruzioni MT5\n"
        "4. Segui i passi su MT5\n"
        "5. Scrivi Entrato → monitoraggio\n\n"
        "ML: {}\n\n"
        "Comandi:\n"
        "si/entrato → conferma trade\n"
        "no → salta segnale\n"
        "filtri → telemetria completa\n"
        "report → report dettagliato\n"
        "settimana → report settimanale\n"
        "pausa → sospendi 2 ore\n"
        "riprendi → riattiva\n"
        "saldo X.XX → aggiorna saldo\n"
        "+X.XX/-X.XX → risultato trade".format(
            saldo_virtuale,
            stats["vinti"]/stats["totali"]*100 if stats["totali"]>0 else 0,
            stats["totali"],"Attivo" if ml_ok else "Non disponibile"))
    invia_report()
    prossima_analisi=0.0; prossimo_monitor=0.0

    while True:
        try:
            invia_heartbeat()
            if segnale_in_attesa["attivo"]:
                if time.time()-segnale_in_attesa["timestamp_generazione"]>TIMEOUT_SEGNALE_SEC:
                    sym=segnale_in_attesa["data_trade"]["symbol"]
                    send_telegram("*Segnale scaduto* - {}\nRiprendo la ricerca.".format(sym))
                    segnale_in_attesa["attivo"]=False

            msg_in=leggi_messaggio_telegram()
            if msg_in:
                parola=msg_in.strip().lower()

                if parola in ["filtri","stato","telemetria"]:
                    send_telegram(genera_telemetria()); continue
                if parola in ["report","dettaglio","dettagliato"]:  # NUOVO
                    send_telegram(genera_report_dettagliato()); continue
                if parola in ["settimana","settimanale","week"]:    # NUOVO
                    send_telegram(genera_report_settimanale()); continue
                if parola in ["pausa","sospendi"]:
                    pausa_bot_fino=adesso()+timedelta(hours=2)
                    send_telegram("Agente in pausa per 2 ore.\nScrivi *riprendi* per riattivare."); continue
                if parola in ["riprendi","attiva"]:
                    pausa_bot_fino=None; send_telegram("Agente riattivato!"); continue
                if parola.startswith("saldo "):
                    try:
                        nuovo_s=float(parola.split()[1].replace(",","."))
                        saldo_virtuale=nuovo_s; salva_stato()
                        send_telegram("Saldo aggiornato: *{:.2f} EUR*".format(saldo_virtuale))
                        invia_report()
                    except Exception: send_telegram("Usa: saldo 105.50")
                    continue
                if segnale_in_attesa["attivo"] and parola in ["si","s","yes","y"]:
                    dt=segnale_in_attesa["data_trade"]; invia_istruzioni_entrata(dt); continue
                if segnale_in_attesa["attivo"] and parola in ["no","n","skip"]:
                    sym=segnale_in_attesa["data_trade"]["symbol"]
                    segnale_in_attesa["attivo"]=False
                    send_telegram("Segnale {} saltato.\nContinuo a cercare.".format(sym)); continue
                if segnale_in_attesa["attivo"] and parola in ["entrato","ok","go","confermo"]:
                    dt=segnale_in_attesa["data_trade"]
                    trade_attivo.update({
                        "aperto":True,"symbol":dt["symbol"],"direction":dt["direction"],
                        "entrata":dt["price"],"sl":dt["sl"],"tp":dt["tp"],"be_fatto":False,
                        "ora_entrata":adesso(),"atr":dt["atr"],"size":dt["size"],
                        "in_attesa_risultato":False,"step":"monitoraggio"})
                    segnale_in_attesa["attivo"]=False; prossimo_monitor=0.0
                    send_telegram("*Trade attivato!*\n{} {}\nVolume: {} lotti\n"
                                  "SL: `{:.5f}`\nTP: `{:.5f}`\n\n"
                                  "Monitoro ogni {} minuto\n\n"
                                  "Quando il trade si chiude scrivi:\n"
                                  "+X.XX guadagno | -X.XX perdita | 0 pareggio".format(
                        dt["symbol"],dt["direction"],dt["size"],dt["sl"],dt["tp"],MONITOR_MIN))
                    continue
                if not msg_in.startswith("/"):
                    if trade_attivo["in_attesa_risultato"] or trade_attivo["aperto"]:
                        registra_risultato(msg_in)
                    else:
                        send_telegram("Agente online!\nSaldo: *{:.2f} EUR*\nML: {}\n\n"
                                      "Scrivi *filtri* per lo stato\n"
                                      "Scrivi *report* per report dettagliato".format(
                            saldo_virtuale,"Attivo" if ML_MODEL else "N/D"))
                    continue

            ora_t=time.time()
            if trade_attivo["aperto"]:
                if not trade_attivo["in_attesa_risultato"] and ora_t>=prossimo_monitor:
                    monitora_trade(); prossimo_monitor=time.time()+MONITOR_MIN*60
            else:
                if not trade_attivo["in_attesa_risultato"] and ora_t>=prossima_analisi:
                    esegui_analisi(); prossima_analisi=time.time()+15*60
            time.sleep(1)

        except Exception as e:
            print("Errore loop: {}".format(e),flush=True)
            send_telegram("Errore agente: {} - riavvio...".format(str(e)[:50]))
            time.sleep(30)

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
bot_thread=None; _bot_lock_file=None

def _bot_supervisor():
    import traceback
    while True:
        try: bot_loop()
        except Exception:
            print("bot_loop CRASH:\n{}".format(traceback.format_exc()),flush=True)
        time.sleep(10)

def avvia_bot_una_volta():
    global bot_thread,_bot_lock_file
    if bot_thread is not None: return
    try:
        import fcntl
        _bot_lock_file=open("/tmp/forex_bot.lock","w")
        fcntl.flock(_bot_lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except ImportError: pass
    except OSError:
        print("Un altro worker gestisce gia il bot",flush=True); return
    bot_thread=Thread(target=_bot_supervisor,daemon=True)
    bot_thread.start()

avvia_bot_una_volta()

if __name__=="__main__":
    run_flask()
