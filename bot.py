import requests
import time
import os
from datetime import datetime
from statistics import mean, stdev
from threading import Thread
from flask import Flask

# ---------------------------------------------------------
# 1. SERVER WEB FANTASMA (Risposta immediata per Render)
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Forex Bot Core v3.3 Pro Safe-Entry Active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except:
    HAS_MATPLOTLIB = False

# ---------------------------------------------------------
# 2. CONFIGURAZIONE STRATEGICA AVANZATA
# ---------------------------------------------------------
SYMBOLS = ["EURUSD=X", "GBPUSD=X"]
SALDO_INIZIALE = 100.0
RISCHIO_BASE   = 0.02
SESSIONE_START = 9
SESSIONE_END   = 22
SL_MIN_PIP     = 10
TP_MIN_PIP     = 15
MONITOR_MIN    = 2        
SPREAD_BUFFER  = 1.5  
SOGLIA_APPROVAZIONE = 8  

TELEGRAM_TOKEN   = "8661209874:AAEJoMSfIVQ35TOrgACCF-cO6zlQWcAVuuI"
TELEGRAM_CHAT_ID = "6559735989"
FILE_STORICO     = "storico_saldo.txt"

# ✅ AGGIORNAMENTO STATISTICHE: Inserito il 9° trade (-0.90 EUR)
saldo_virtuale = 105.73  
stats = {"vinti": 4, "persi": 5, "pareggi": 0, "totali": 9}
last_update_id = -1
ultimo_heartbeat_orario = -1

macd_memoria = {}

trade_attivo = {
    "aperto": False, "symbol": None, "direction": None, "entrata": None,
    "sl": None, "tp": None, "be_fatto": False, "max_prezzo_raggiunto": None,
    "min_prezzo_raggiunto": None, "in_attesa_risultato": False
}

# Struttura di memoria per la gestione della conferma manuale
segnale_in_attesa = {
    "attivo": False, "timestamp_generazione": None, "data_trade": None
}

if not os.path.exists(FILE_STORICO):
    with open(FILE_STORICO, "w") as f:
        # Aggiornata la stringa storica includendo il nuovo saldo finale a 105.73
        f.write("100.0\n101.5\n103.0\n105.85\n108.18\n108.10\n108.02\n107.65\n106.63\n105.73\n")

# ---------------------------------------------------------
# 3. FUNZIONI TELEGRAM & GRAFICI
# ---------------------------------------------------------
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def send_telegram_foto(photo_path, caption):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as photo:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}, files={"photo": photo}, timeout=15)
    except: pass

def genera_e_invia_grafico(testo_report):
    if not HAS_MATPLOTLIB:
        send_telegram(testo_report)
        return
    try:
        with open(FILE_STORICO, "r") as f:
            saldi = [float(line.strip()) for line in f.readlines() if line.strip()]
        plt.figure(figsize=(8, 4))
        plt.plot(saldi, marker='o', color='#007AFF', linewidth=2, label="Equity Line")
        plt.title("Crescita del Capitale (Virtual Portfolio)")
        plt.xlabel("Numero Trade")
        plt.ylabel("EUR")
        plt.grid(True, linestyle='--', alpha=0.6)
        plt.legend()
        path_img = "equity.png"
        plt.savefig(path_img, bbox_inches='tight', dpi=150)
        plt.close()
        send_telegram_foto(path_img, testo_report)
    except:
        send_telegram(testo_report)

def leggi_messaggio_telegram():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 2}
        r = requests.get(url, params=params, timeout=5).json()
        if r["result"]:
            for update in r["result"]:
                last_update_id = update["update_id"]
                try:
                    if str(update["message"]["chat"]["id"]) == TELEGRAM_CHAT_ID:
                        return update["message"]["text"]
                except: pass
    except: pass
    return None

def invia_report():
    global saldo_virtuale, stats
    wr = (stats["vinti"] / stats["totali"] * 100) if stats["totali"] > 0 else 0
    profitto = saldo_virtuale - SALDO_INIZIALE
    profitto_str = f"+{profitto:.2f}" if profitto >= 0 else f"{profitto:.2f}"
    msg = (
        "📊 *DIARIO DI TRADING AGGIORNATO*\n"
        "-------------------------\n"
        f"💰 Saldo attuale : *{saldo_virtuale:.2f} EUR*\n"
        f"📈 Profitto tot  : *{profitto_str} EUR*\n"
        f"🏆 Win Rate      : *{wr:.1f}%*\n"
        "-------------------------\n"
        f"✅ Vinti    : {stats['vinti']}\n"
        f"❌ Persi    : {stats['persi']}\n"
        f"🛡️ Pareggi  : {stats['pareggi']}\n"
        f"🔢 Totali   : {stats['totali']}"
    )
    genera_e_invia_grafico(msg)

def registra_risultato(testo):
    global saldo_virtuale, stats, trade_attivo
    testo = testo.strip().replace(",", ".")
    try: profit = float(testo)
    except:
        send_telegram("⚠️ Formato non riconosciuto. Scrivi es. `+1.50` o `-0.80`")
        return False
    saldo_virtuale += profit
    stats["totali"] += 1
    if profit > 0.02: stats["vinti"] += 1; emoji = "🏆 VINTO"
    elif profit < -0.02: stats["persi"] += 1; emoji = "❌ PERSO"
    else: stats["pareggi"] += 1; emoji = "🛡️ PAREGGIO"
    with open(FILE_STORICO, "a") as f: f.write(f"{saldo_virtuale:.2f}\n")
    send_telegram(f"Trade registrato: *{profit:+.2f} EUR* - {emoji}")
    invia_report()
    trade_attivo["aperto"] = False
    trade_attivo["in_attesa_risultato"] = False
    return True

# ---------------------------------------------------------
# 4. CONTROLLI OPERATIVI
# ---------------------------------------------------------
def is_mercato_aperto():
    giorno_settimana = datetime.now().weekday() 
    ora_corrente = datetime.now().hour
    if giorno_settimana == 4 and ora_corrente >= 23: return False
    if giorno_settimana == 5: return False
    if giorno_settimana == 6 and ora_corrente < 23: return False
    return True

def is_orario_sessione():
    ora = datetime.now().hour
    return SESSIONE_START <= ora < SESSIONE_END

# ---------------------------------------------------------
# 5. CALCOLO INDICATORI (Ottimizzati)
# ---------------------------------------------------------
def compute_ema(prices, period):
    if len(prices) < period: return mean(prices) if prices else None
    k = 2 / (period + 1)
    ema = mean(prices[:period])
    for price in prices[period:]: ema = (price * k) + (ema * (1 - k))
    return ema

def compute_macd_veloce(closes, symbol, fast_period=12, slow_period=26, signal_period=9):
    if len(closes) < slow_period: return 0, 0, 0
    fast_ema = compute_ema(closes, fast_period)
    slow_ema = compute_ema(closes, slow_period)
    macd_line = fast_ema - slow_ema
    if symbol not in macd_memoria:
        macd_memoria[symbol] = []
    macd_memoria[symbol].append(macd_line)
    if len(macd_memoria[symbol]) > 50:
        macd_memoria[symbol].pop(0)
    signal_line = compute_ema(macd_memoria[symbol], signal_period)
    if signal_line is None: signal_line = macd_line
    return macd_line, signal_line, (macd_line - signal_line)

def compute_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = []
    for i in range(1, len(candles)):
        tr = max(candles[i]["high"] - candles[i]["low"], abs(candles[i]["high"] - candles[i-1]["close"]), abs(candles[i]["low"] - candles[i-1]["close"]))
        trs.append(tr)
    return mean(trs[-period:])

def compute_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = mean(gains) if gains else 0
    avg_loss = mean(losses) if losses else 0
    if avg_loss == 0: return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def compute_bollinger(closes, period=20):
    if len(closes) < period: return None, None, None
    recent = closes[-period:]
    ma = mean(recent)
    sd = stdev(recent)
    return ma + 2 * sd, ma, ma - 2 * sd

def get_support_resistance(candles):
    highs = [c["high"] for c in candles[-100:]]
    lows = [c["low"] for c in candles[-100:]]
    return max(highs), min(lows)

def detect_candle_pattern(candles):
    if len(candles) < 2: return "NONE"
    c = candles[-1]
    body = abs(c["close"] - c["open"])
    total_range = c["high"] - c["low"]
    if total_range == 0: return "NONE"
    lower_shadow = min(c["open"], c["close"]) - c["low"]
    upper_shadow = c["high"] - max(c["open"], c["close"])
    if lower_shadow >= (body * 2) and upper_shadow <= (total_range * 0.2): return "HAMMER"
    if upper_shadow >= (body * 2) and lower_shadow <= (total_range * 0.2): return "SHOOTING_STAR"
    return "NONE"

def check_news_block():
    adesso = datetime.now()
    if adesso.hour in [10, 11, 14, 15, 16] and adesso.minute < 15: return True
    return False

# ---------------------------------------------------------
# 6. MOTORE DI CALCOLO MATRICE
# ---------------------------------------------------------
def calcola_matrice_asset(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = requests.get(url, params={"interval": "15m", "range": "5d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
        result = r["chart"]["result"][0]
        closes = [x for x in result["indicators"]["quote"][0]["close"] if x is not None]
        ohlcv = result["indicators"]["quote"][0]
        candles = []
        for i in range(len(ohlcv["close"])):
            if all(ohlcv[k][i] is not None for k in ["open","high","low","close"]):
                candles.append({"open": ohlcv["open"][i], "high": ohlcv["high"][i], "low": ohlcv["low"][i], "close": ohlcv["close"][i]})
        
        price = closes[-1]
        ema50_15m = compute_ema(closes, 50)
        rsi = compute_rsi(closes)
        bb_upper, _, bb_lower = compute_bollinger(closes)
        _, _, macd_hist = compute_macd_veloce(closes, symbol)
        res_max, sup_min = get_support_resistance(candles)
        pattern = detect_candle_pattern(candles)
        atr = compute_atr(candles)

        r_1h = requests.get(url, params={"interval": "1h", "range": "15d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
        closes_1h = [x for x in r_1h["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x is not None]
        ema50_1h = compute_ema(closes_1h, 50)
        ema20_4h = compute_ema(closes_1h, 80)
    except:
        return None

    dir_base = "NESSUNO"
    if price > ema50_15m and price > ema50_1h: dir_base = "LONG"
    elif price < ema50_15m and price < ema50_1h: dir_base = "SHORT"
    
    if dir_base == "NESSUNO":
        return {"symbol": symbol, "punti": 0, "direzione": "NESSUNO", "price": price, "msg": "Trend M15/H1 disallineato"}

    h4_ok = (dir_base == "LONG" and price > ema20_4h) or (dir_base == "SHORT" and price < ema20_4h)
    if not h4_ok:
        return {"symbol": symbol, "punti": 0, "direzione": dir_base, "price": price, "msg": "❌ Bloccato: Contro Trend H4"}

    punti = 2  
    p_bb = 2 if ((dir_base == "LONG" and price <= bb_lower * 1.001) or (dir_base == "SHORT" and price >= bb_upper * 0.999)) else 0
    p_rsi = 1 if (40 < rsi < 60) else 0
    p_macd = 2 if ((dir_base == "LONG" and macd_hist > 0) or (dir_base == "SHORT" and macd_hist < 0)) else 0
    proximity = (atr * 0.5) if atr else 0.001
    p_sr = 2 if ((dir_base == "LONG" and abs(price - sup_min) <= proximity) or (dir_base == "SHORT" and abs(price - res_max) <= proximity)) else 0
    p_pat = 2 if ((dir_base == "LONG" and pattern == "HAMMER") or (dir_base == "SHORT" and pattern == "SHOOTING_STAR")) else 0
    
    totale = punti + p_bb + p_rsi + p_macd + p_sr + p_pat
    
    buffer_val = (SPREAD_BUFFER / 10000)
    sl = (price - atr * 1.5) - buffer_val if dir_base == "LONG" else (price + atr * 1.5) + buffer_val
    tp = (price + atr * 2.5) + buffer_val if dir_base == "LONG" else (price - atr * 2.5) - buffer_val
    pip_sl = abs(price - sl) * 10000

    return {
        "symbol": symbol, "punti": totale, "direzione": dir_base, "price": price, 
        "sl": sl, "tp": tp, "pip_sl": pip_sl, "atr": atr, "score": "A+" if totale >= 9 else "A",
        "msg": f"Filtri superati ({totale}/11)" if totale >= SOGLIA_APPROVAZIONE else "Sotto la soglia minima di 8"
    }

def genera_report_ispettivo():
    report = "🔍 *TELEMETRIA INTEGRALE FILTRI*\n-------------------------\n"
    if segnale_in_attesa["attivo"]:
        report += "⏳ *Stato*: In attesa di conferma entrata manuale dell'utente...\n-------------------------\n"
    elif trade_attivo["aperto"] or trade_attivo["in_attesa_risultato"]:
        report += "⚠️ *Stato*: Ricerca sospesa (Trade a mercato)\n-------------------------\n"
    for symbol in SYMBOLS:
        res = calcola_matrice_asset(symbol)
        if res:
            report += f"💱 *Asset: {res['symbol']}* | Prezzo: `{res['price']:.5f}`\n"
            report += f"🏷️ Direzione: *{res['direzione']}* | Punti: *{res['punti']}/11*\n"
            report += f"📋 Diagnosi: _{res['msg']}_\n\n"
    return report

# ---------------------------------------------------------
# 7. LOGICA CORE DI MONITORAGGIO
# ---------------------------------------------------------
def monitora_trade():
    global trade_attivo
    if not trade_attivo["aperto"]: return
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{trade_attivo['symbol']}"
        r = requests.get(url, params={"interval": "1m", "range": "1d"}, headers={"User-Agent": "Mozilla/5.0"}).json()
        prezzo = r["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1]
    except: return

    dir_t = trade_attivo["direction"]
    entrata = trade_attivo["entrata"]
    tp = trade_attivo["tp"]
    sl = trade_attivo["sl"]
    atr_corrente = trade_attivo.get("atr", 0.0015)
    distanza_tp = abs(tp - entrata)

    if not trade_attivo["be_fatto"]:
        if (dir_t == "LONG" and (prezzo - entrata) >= (distanza_tp * 0.5)) or (dir_t == "SHORT" and (entrata - prezzo) >= (distanza_tp * 0.5)):
            trade_attivo["sl"] = entrata; trade_attivo["be_fatto"] = True
            send_telegram(f"🛡️ *BREAK-EVEN ATTIVATO* su {trade_attivo['symbol']}.")

    if dir_t == "LONG":
        n_sl = prezzo - (atr_corrente * 1.5)
        if n_sl > trade_attivo["sl"] and prezzo > entrata: trade_attivo["sl"] = n_sl
    elif dir_t == "SHORT":
        n_sl = prezzo + (atr_corrente * 1.5)
        if n_sl < trade_attivo["sl"] and prezzo < entrata: trade_attivo["sl"] = n_sl

    tolleranza = 0.00005 
    if (dir_t == "LONG" and prezzo >= tp) or (dir_t == "SHORT" and prezzo <= tp):
        send_telegram(f"🎯 *TARGET RAGGIUNTO* su {trade_attivo['symbol']}! Digita il profitto netto.")
        trade_attivo["in_attesa_risultato"] = True
    elif (dir_t == "LONG" and prezzo <= (sl - tolleranza)) or (dir_t == "SHORT" and prezzo >= (sl + tolleranza)):
        send_telegram(f"🛑 *STOP LOSS COLPITO* su {trade_attivo['symbol']}! Digita l'esito numerico.")
        trade_attivo["in_attesa_risultato"] = True

def esegui_analisi():
    global segnale_in_attesa
    if not is_mercato_aperto() or not is_orario_sessione() or check_news_block(): return
    if trade_attivo["aperto"] or trade_attivo["in_attesa_risultato"] or segnale_in_attesa["attivo"]: return
    
    for symbol in SYMBOLS:
        res = calcola_matrice_asset(symbol)
        if res and res["punti"] >= SOGLIA_APPROVAZIONE:
            rischio_eur = saldo_virtuale * RISCHIO_BASE * (1.0 if res["punti"] >= 9 else 0.75)
            lotti = round(max((rischio_eur / (res["pip_sl"] * 0.09)) * 0.01, 0.01), 2)
            
            # ✅ MODIFICA SAFE-ENTRY: Il segnale richiede la conferma esplicita
            msg = (
                f"⚠️ *SEGNALE GENERATO - IN ATTESA DI CONFERMA OPERATIVA*\n"
                f"Asset: *{symbol}* | Tendenza: *{res['direzione']}* ({res['punti']}/11 Punti)\n"
                f"Ingresso consigliato: `{res['price']:.5f}`\n"
                f"Stop Loss: `{res['sl']:.5f}` | Take Profit: `{res['tp']:.5f}`\n"
                f"Volume Fineco: *{lotti} lotti*\n\n"
                f"👉 Scrivi *'Entrato'* (o 'ok') entro 5 minuti per confermare l'apertura su Fineco, altrimenti il segnale scadrà."
            )
            send_telegram(msg)
            
            # Salviamo il pacchetto dati temporaneamente
            segnale_in_attesa.update({
                "attivo": True,
                "timestamp_generazione": time.time(),
                "data_trade": {
                    "symbol": symbol, "direction": res["direzione"], "entrata": res["price"],
                    "sl": res["sl"], "tp": res["tp"], "atr": res["atr"]
                }
            })
            break

# ---------------------------------------------------------
# 8. LOOP OPERATIVO PRINCIPALE CON GESTIONE TIMEOUT CONFERMA
# ---------------------------------------------------------
def bot_loop():
    global ultimo_heartbeat_orario, segnale_in_attesa, trade_attivo
    send_telegram("🚀 *CORE V3.3 PRO - SAFE-ENTRY ATTIVATO*\n- Statistiche allineate (9 Trade totali | Saldo: 105.73 EUR)\n- Sistema di richiesta conferma ingresso attivo 🛡️")
    invia_report()
    
    while True:
        adesso_dt = datetime.now()
        
        # Gestione Scadenza Segnale (Timeout 5 minuti = 300 secondi)
        if segnale_in_attesa["attivo"]:
            if time.time() - segnale_in_attesa["timestamp_generazione"] > 300:
                send_telegram(f"❌ *SEGNALE SCADUTO*: Nessuna conferma ricevuta per {segnale_in_attesa['data_trade']['symbol']}. Ricerca ripresa.")
                segnale_in_attesa["attivo"] = False
        
        # Heartbeat Orario Automatico
        if adesso_dt.minute == 0 and adesso_dt.hour != ultimo_heartbeat_orario:
            ultimo_heartbeat_orario = adesso_dt.hour
            status_msg = f"⏱️ *HEARTBEAT ORARIO AUTOMATICO - ORE {adesso_dt.hour}:00*\n"
            if not is_mercato_aperto(): status_msg += "Status: *Stand-by* 💤 (Mercati Chiusi)"
            elif not is_orario_sessione(): status_msg += "Status: *Riposo* ⏳ (Fuori Sessione)"
            else:
                status_msg += "Status: *Scansione Attiva* 🟢\n"
                send_telegram(status_msg)
                send_telegram(genera_report_ispettivo())

        msg_in = leggi_messaggio_telegram()
        if msg_in:
            parola = msg_in.strip().lower()
            if parola in ["filtri", "stato", "telemetria", "test"]:
                send_telegram(genera_report_ispettivo())
                continue
            
            # ✅ GESTIONE CONFERMA SEGNALE IN ATTESA
            if segnale_in_attesa["attivo"] and parola in ["entrato", "ok", "go", "si", "confermo"]:
                dt = segnale_in_attesa["data_trade"]
                trade_attivo.update({
                    "aperto": True, "symbol": dt["symbol"], "direction": dt["direction"],
                    "entrata": dt["entrata"], "sl": dt["sl"], "tp": dt["tp"],
                    "be_fatto": False, "max_prezzo_raggiunto": dt["entrata"], "min_prezzo_raggiunto": dt["entrata"],
                    "atr": dt["atr"], "in_attesa_risultato": False
                })
                segnale_in_attesa["attivo"] = False
                send_telegram(f"🚀 *Ricevuto!* Trade su {dt['symbol']} attivato nel sistema di monitoraggio. Buona fortuna!")
                continue
                
            if not msg_in.startswith("/"):
                if trade_attivo["in_attesa_risultato"] or trade_attivo["aperto"]:
                    registra_risultato(msg_in)
                else:
                    send_telegram(f"🤖 *Ricevuto: '{msg_in}'*\nNessun trade da bilanciare. Scrivi 'Filtri' per lo stato.")
                continue
        
        if trade_attivo["aperto"] and not trade_attivo["in_attesa_risultato"]:
            monitora_trade()
            time.sleep(MONITOR_MIN * 60)
        else:
            esegui_analisi()
            time.sleep(60)

if __name__ == "__main__":
    t = Thread(target=bot_loop)
    t.daemon = True
    def avvio_ritardato():
        time.sleep(2)
        t.start()
    Thread(target=avvio_ritardato).start()
    run_flask()
