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
    return "Forex Bot is Alive and Running!", 200

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
# 2. CONFIGURAZIONE STRATEGICA
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

TELEGRAM_TOKEN   = "8661209874:AAEJoMSfIVQ35TOrgACCF-cO6zlQWcAVuuI"
TELEGRAM_CHAT_ID = "6559735989"
FILE_STORICO     = "storico_saldo.txt"

saldo_virtuale = 106.63  
stats = {"vinti": 4, "persi": 4, "pareggi": 0, "totali": 8}
last_update_id = -1
ultimo_controllo_orario = -1

trade_attivo = {
    "aperto": False, "symbol": None, "direction": None, "entrata": None,
    "sl": None, "tp": None, "be_fatto": False, "max_prezzo_raggiunto": None,
    "min_prezzo_raggiunto": None, "in_attesa_risultato": False
}

if not os.path.exists(FILE_STORICO):
    with open(FILE_STORICO, "w") as f:
        f.write("100.0\n101.5\n103.0\n105.85\n108.18\n108.10\n108.02\n107.65\n106.63\n")

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
# 4. CONTROLLI DI SESSIONE E MERCATO
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
# 5. MATEMATICA DEGLI INDICATORI AVANZATI
# ---------------------------------------------------------
def compute_ema(prices, period):
    if len(prices) < period: return mean(prices) if prices else None
    k = 2 / (period + 1)
    ema = mean(prices[:period])
    for price in prices[period:]: ema = (price * k) + (ema * (1 - k))
    return ema

def compute_macd(closes, fast_period=12, slow_period=26, signal_period=9):
    if len(closes) < slow_period + signal_period: return 0, 0, 0
    macd_line = []
    # Generiamo la serie storica del MACD per poter calcolare la sua EMA (Signal Line)
    for i in range(slow_period, len(closes) + 1):
        sub_closes = closes[:i]
        fast_ema = compute_ema(sub_closes, fast_period)
        slow_ema = compute_ema(sub_closes, slow_period)
        macd_line.append(fast_ema - slow_ema)
    
    signal_line = compute_ema(macd_line, signal_period)
    current_macd = macd_line[-1]
    histogram = current_macd - signal_line
    return current_macd, signal_line, histogram

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
    """ Rileva strutture Hammer (Long) o Shooting Star (Short) """
    if len(candles) < 2: return "NONE"
    c = candles[-1]
    body = abs(c["close"] - c["open"])
    total_range = c["high"] - c["low"]
    if total_range == 0: return "NONE"
    
    lower_shadow = min(c["open"], c["close"]) - c["low"]
    upper_shadow = c["high"] - max(c["open"], c["close"])
    
    # Hammer: ombra inferiore lunga almeno il doppio del corpo e pochissima ombra superiore
    if lower_shadow >= (body * 2) and upper_shadow <= (total_range * 0.2):
        return "HAMMER"
    # Shooting Star: ombra superiore lunga almeno il doppio del corpo e pochissima ombra inferiore
    if upper_shadow >= (body * 2) and lower_shadow <= (total_range * 0.2):
        return "SHOOTING_STAR"
    return "NONE"

def check_news_block():
    adesso = datetime.now()
    if adesso.hour in [10, 11, 14, 15, 16] and adesso.minute < 15: return True
    return False

# ---------------------------------------------------------
# 6. MOTORE DI ANALISI MULTI-TIME_FRAME CON FILTRI COMPLETI
# ---------------------------------------------------------
def analizza(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = requests.get(url, params={"interval": "15m", "range": "5d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).json()
        result = r["chart"]["result"][0]
        timestamps = result["timestamp"]
        ohlcv = result["indicators"]["quote"][0]
        candles = []
        for i in range(len(ohlcv["close"])):
            if all(ohlcv[k][i] is not None for k in ["open","high","low","close"]):
                candles.append({"time": timestamps[i], "open": ohlcv["open"][i], "high": ohlcv["high"][i], "low": ohlcv["low"][i], "close": ohlcv["close"][i]})
    except: return None, "Errore download"

    if len(candles) < 100: return None, "Dati insufficienti"
    closes = [c["close"] for c in candles]
    price = closes[-1]
    
    # Indicatori standard
    ema50_15m = compute_ema(closes, 50)
    atr = compute_atr(candles)
    rsi = compute_rsi(closes)
    bb_upper, _, bb_lower = compute_bollinger(closes)
    
    # FILTRI AVANZATI AGGIUNTI
    macd_line, signal_line, macd_hist = compute_macd(closes)
    res_max, sup_min = get_support_resistance(candles)
    pattern = detect_candle_pattern(candles)

    try:
        r_1h = requests.get(url, params={"interval": "1h", "range": "15d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
        closes_1h = [x for x in r_1h["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x is not None]
        ema50_1h = compute_ema(closes_1h, 50)
        ema20_4h = compute_ema(closes_1h, 80)
    except: return None, "Errore MTF"

    # 1. Filtro Direzionale MTF
    direction = None
    if price > ema50_15m and price > ema50_1h: direction = "LONG"
    elif price < ema50_15m and price < ema50_1h: direction = "SHORT"
    if direction is None: return None, "Trend MTF non allineato"

    # Calcolo SL e TP dinamico
    buffer_val = (SPREAD_BUFFER / 10000)
    if direction == "LONG":
        sl = (price - atr * 1.5) - buffer_val
        tp = (price + atr * 2.5) + buffer_val
    else:
        sl = (price + atr * 1.5) + buffer_val
        tp = (price - atr * 2.5) + buffer_val

    pip_sl = abs(price - sl) * 10000
    if pip_sl < SL_MIN_PIP: return None, f"SL stretto ({pip_sl:.1f} pip)"

    # Sistema di Punteggio ad Accordo Matrice
    punti = 0
    if (direction == "LONG" and price > ema20_4h) or (direction == "SHORT" and price < ema20_4h): punti += 2 # Controllo H4
    if (direction == "LONG" and price <= bb_lower * 1.001) or (direction == "SHORT" and price >= bb_upper * 0.999): punti += 2 # Bollinger
    if (40 < rsi < 60): punti += 1 # RSI d'equilibrio
    
    # Validazione Filtro MACD
    macd_ok = (direction == "LONG" and macd_hist > 0) or (direction == "SHORT" and macd_hist < 0)
    if macd_ok: punti += 2
    
    # Validazione Filtro Supporti/Resistenze (Rimbalzi volumetrici)
    proximity = atr * 0.5
    sr_ok = (direction == "LONG" and abs(price - sup_min) <= proximity) or (direction == "SHORT" and abs(price - res_max) <= proximity)
    if sr_ok: punti += 2
    
    # Validazione Pattern Candele
    pattern_ok = (direction == "LONG" and pattern == "HAMMER") or (direction == "SHORT" and pattern == "SHOOTING_STAR")
    if pattern_ok: punti += 2

    # Solo segnali istituzionali ad alto punteggio
    if punti >= 8: score, molt = "A+", 1.0
    elif punti >= 6: score, molt = "A", 0.75
    else: return None, f"Punteggio insufficiente ({punti}/11)"

    rischio_eur = saldo_virtuale * RISCHIO_BASE * molt
    lotti = round(max((rischio_eur / (pip_sl * 0.09)) * 0.01, 0.01), 2)

    return {
        "symbol": symbol, "direction": direction, "price": price, "sl": sl, "tp": tp,
        "pip_sl": pip_sl, "pip_tp": abs(price - tp) * 10000, "rr": (abs(price - tp) / abs(price - sl)), 
        "size": lotti, "rischio": rischio_eur, "score": score, "atr": atr, "punti": punti
    }, "OK"

# ---------------------------------------------------------
# 7. MONITORAGGIO TRADE (Trailing Stop & Break-Even Corretto)
# ---------------------------------------------------------
def monitora_trade():
    global trade_attivo
    if not trade_attivo["aperto"]: return
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{trade_attivo['symbol']}"
    try:
        r = requests.get(url, params={"interval": "1m", "range": "1d"}, headers={"User-Agent": "Mozilla/5.0"}).json()
        prezzo = r["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1]
    except: return

    dir_t = trade_attivo["direction"]
    entrata = trade_attivo["entrata"]
    tp = trade_attivo["tp"]
    sl = trade_attivo["sl"]
    atr_corrente = trade_attivo.get("atr", 0.0015)

    if trade_attivo["max_prezzo_raggiunto"] is None: trade_attivo["max_prezzo_raggiunto"] = prezzo
    if trade_attivo["min_prezzo_raggiunto"] is None: trade_attivo["min_prezzo_raggiunto"] = prezzo

    if prezzo > trade_attivo["max_prezzo_raggiunto"]: trade_attivo["max_prezzo_raggiunto"] = prezzo
    if prezzo < trade_attivo["min_prezzo_raggiunto"]: trade_attivo["min_prezzo_raggiunto"] = prezzo

    distanza_tp = abs(tp - entrata)
    if not trade_attivo["be_fatto"]:
        if dir_t == "LONG" and (prezzo - entrata) >= (distanza_tp * 0.5):
            trade_attivo["sl"] = entrata
            trade_attivo["be_fatto"] = True
            send_telegram(f"🛡️ *BREAK-EVEN ATTIVATO* su {trade_attivo['symbol']}. Stop loss blindato a pareggio.")
        elif dir_t == "SHORT" and (entrata - prezzo) >= (distanza_tp * 0.5):
            trade_attivo["sl"] = entrata
            trade_attivo["be_fatto"] = True
            send_telegram(f"🛡️ *BREAK-EVEN ATTIVATO* su {trade_attivo['symbol']}. Stop loss blindato a pareggio.")

    if dir_t == "LONG":
        nuovo_sl_trailing = prezzo - (atr_corrente * 1.5)
        if nuovo_sl_trailing > trade_attivo["sl"] and prezzo > entrata: 
            trade_attivo["sl"] = nuovo_sl_trailing
    elif dir_t == "SHORT":
        nuovo_sl_trailing = prezzo + (atr_corrente * 1.5)
        # ✅ FIX: Corretto l'errore di battitura 'nuevo_sl_trailing' che bloccava il bot in short
        if nuovo_sl_trailing < trade_attivo["sl"] and prezzo < entrata: 
            trade_attivo["sl"] = nuovo_sl_trailing

    if (dir_t == "LONG" and prezzo >= tp) or (dir_t == "SHORT" and prezzo <= tp):
        send_telegram(f"🎯 *TARGET RAGGIUNTO* su {trade_attivo['symbol']}!\nInserisci il profitto finale.")
        trade_attivo["in_attesa_risultato"] = True
    elif (dir_t == "LONG" and prezzo <= trade_attivo["sl"]) or (dir_t == "SHORT" and prezzo >= trade_attivo["sl"]):
        send_telegram(f"🛑 *STOP LOSS/TRAILING COLPITO* su {trade_attivo['symbol']}.\nInserisci l'esito finale.")
        trade_attivo["in_attesa_risultato"] = True

# ---------------------------------------------------------
# 8. LOOP OPERATIVO (Scansione bilanciata ogni 60s)
# ---------------------------------------------------------
def esegui_analisi():
    if not is_mercato_aperto() or not is_orario_sessione(): return
    if check_news_block(): return
        
    for symbol in SYMBOLS:
        signal, motivo = analizza(symbol)
        if signal:
            msg = (
                f"💎 *SEGNALE PRO {signal['score']} ATTIVO*\n"
                f"Asset: *{symbol}* | Configurazione: *{signal['direction']}*\n"
                f"Matrice Punti: `{signal['punti']}/11`📊\n"
                f"Ingresso: `{signal['price']:.5f}`\n"
                f"Stop Loss: `{signal['sl']:.5f}`\n"
                f"Take Profit: `{signal['tp']:.5f}`\n"
                f"Fineco Volume: *{signal['size']} lotti*"
            )
            send_telegram(msg)
            trade_attivo.update({
                "aperto": True, "symbol": symbol, "direction": signal["direction"], 
                "entrata": signal["price"], "sl": signal["sl"], "tp": signal["tp"], 
                "be_fatto": False, "max_prezzo_raggiunto": signal["price"], "min_prezzo_raggiunto": signal["price"],
                "atr": signal["atr"], "in_attesa_risultato": False
            })
            break

def bot_loop():
    global ultimo_controllo_orario
    
    send_telegram(
        f"🚀 *FOREX ENGINE CLOUD V3 CORE AGGIORNATO*\n"
        f"-----------------------------------------\n"
        f"✅ Fix Trailing Short applicato\n"
        f"✅ Richieste Yahoo bilanciate (60s)\n"
        f"✅ Filtri Attivi: MACD, S/R, Candele, MTF, Bollinger, RSI\n"
        f"-----------------------------------------\n"
        f"Status: *Live & Running* 🟢"
    )
    invia_report()
    
    while True:
        adesso = datetime.now()
        
        if adesso.minute == 0 and adesso.hour != ultimo_controllo_orario:
            ultimo_controllo_orario = adesso.hour
            if not trade_attivo["aperto"]:
                if not is_mercato_aperto():
                    send_telegram(f"💤 *Pattugliamento Ore {adesso.hour}:00* - Mercati Forex Chiusi. Monitoraggio in stand-by.")
                elif not is_orario_sessione():
                    send_telegram(f"⏳ *Pattugliamento Ore {adesso.hour}:00* - Fuori sessione (9-22).")
                else:
                    send_telegram(f"🟢 *Pattugliamento Ore {adesso.hour}:00* - Scansione attiva con matrice 11 punti.")

        msg_in = leggi_messaggio_telegram()
        if msg_in:
            if not msg_in.startswith("/"):
                if trade_attivo["in_attesa_risultato"] or trade_attivo["aperto"]:
                    registra_risultato(msg_in)
                else:
                    if not is_mercato_aperto():
                        send_telegram(f"🤖 *Ricevuto: '{msg_in}'*\n\nI mercati sono chiusi per il weekend. Il sistema è in stand-by. Saldo attuale: *{saldo_virtuale:.2f} EUR*.")
                    else:
                        send_telegram(f"🤖 *Ricevuto: '{msg_in}'*\n\nNessun trade attivo. Scansione ciclica sui filtri avanzati in esecuzione.")
                continue
        
        if trade_attivo["aperto"] and not trade_attivo["in_attesa_risultato"]:
            monitora_trade()
            time.sleep(MONITOR_MIN * 60)
        else:
            esegui_analisi()
            # ✅ SCANSIONE EQUILIBRATA: Controlliamo la chat rapidamente, ma l'analisi rispetta i tempi per non farsi bannare l'IP
            time.sleep(60)

if __name__ == "__main__":
    t = Thread(target=bot_loop)
    t.daemon = True
    
    def avvio_ritardato():
        time.sleep(2)
        t.start()
        
    Thread(target=avvio_ritardato).start()
    run_flask()
