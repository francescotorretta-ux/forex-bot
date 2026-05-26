import requests
import time
import os
import json
from datetime import datetime, timedelta
from statistics import mean, stdev
from threading import Thread
from flask import Flask

# ---------------------------------------------------------
# 1. SERVER WEB FANTASMA
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Forex Bot Core v4.2 Ultra TwelveData Active!", 200

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
# 2. CONFIGURAZIONE STRATEGICA TWELVE DATA
# ---------------------------------------------------------
SYMBOLS          = ["EUR/USD", "GBP/USD"]
SALDO_INIZIALE   = 100.0
RISCHIO_BASE     = 0.02
SESSIONE_START   = 9
SESSIONE_END     = 22
SPREAD_BUFFER    = 1.5
SOGLIA_APPROVAZIONE = 7
MONITOR_MIN      = 1        

# Credenziali inserite direttamente nel codice per sicurezza immediata
TELEGRAM_TOKEN   = "8661209874:AAEJoMSfIVQ35TOrgACCF-cO6zlQWcAVuuI"
TELEGRAM_CHAT_ID = "6559735989"
TWELVEDATA_API_KEY = "f7ad19a1b160485cb773bacfad03543d"

FILE_STORICO     = "storico_saldo.txt"
FILE_STATO       = "stato_bot.json"   

# ---------------------------------------------------------
# 3. PERSISTENZA STATO E VARIABILI GLOBALI
# ---------------------------------------------------------
def carica_stato():
    default = {
        "saldo_virtuale": 105.73,
        "stats": {"vinti": 4, "persi": 5, "pareggi": 0, "totali": 9}
    }
    if os.path.exists(FILE_STATO):
        try:
            with open(FILE_STATO, "r") as f:
                return json.load(f)
        except:
            pass
    return default

def salva_stato():
    try:
        with open(FILE_STATO, "w") as f:
            json.dump({"saldo_virtuale": saldo_virtuale, "stats": stats}, f)
    except Exception as e:
        print(f"[ERRORE] Salvataggio stato: {e}")

_stato = carica_stato()
saldo_virtuale   = _stato["saldo_virtuale"]
stats            = _stato["stats"]

last_update_id          = -1
ultimo_heartbeat_orario = -1
macd_memoria            = {}
pausa_bot_fino          = None  

trade_attivo = {
    "aperto": False, "symbol": None, "direction": None, "entrata": None,
    "sl": None, "tp": None, "be_fatto": False, "max_prezzo_raggiunto": None,
    "min_prezzo_raggiunto": None, "in_attesa_risultato": False, "atr": 0.0015
}

segnale_in_attesa = {
    "attivo": False, "timestamp_generazione": None, "data_trade": None
}

if not os.path.exists(FILE_STORICO):
    with open(FILE_STORICO, "w") as f:
        f.write("100.0\n101.5\n103.0\n105.85\n108.18\n108.10\n108.02\n107.65\n106.63\n105.73\n")

# ---------------------------------------------------------
# 4. FUNZIONI TELEGRAM & GRAFICI
# ---------------------------------------------------------
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] {msg}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass

def send_telegram_foto(photo_path, caption):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as photo:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}, files={"photo": photo}, timeout=15)
    except:
        pass

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
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return None
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
                except:
                    pass
    except:
        pass
    return None

def invia_report():
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
    try:
        profit = float(testo)
    except:
        send_telegram("⚠️ Formato non riconosciuto. Scrivi es. `+1.50` o `-0.80`")
        return False

    saldo_virtuale += profit
    stats["totali"] += 1
    if profit > 0.02:
        stats["vinti"] += 1
        emoji = "🏆 VINTO"
    elif profit < -0.02:
        stats["persi"] += 1
        emoji = "❌ PERSO"
    else:
        stats["pareggi"] += 1
        emoji = "🛡️ PAREGGIO"

    salva_stato()

    with open(FILE_STORICO, "a") as f:
        f.write(f"{saldo_virtuale:.2f}\n")

    send_telegram(f"Trade registered: *{profit:+.2f} EUR* - {emoji}")
    invia_report()
    trade_attivo["aperto"] = False
    trade_attivo["in_attesa_risultato"] = False
    return True

# ---------------------------------------------------------
# 5. CONTROLLI OPERATIVI DI SICUREZZA
# ---------------------------------------------------------
def is_mercato_aperto():
    giorno = datetime.now().weekday()
    ora = datetime.now().hour
    if giorno == 4 and ora >= 23: return False
    if giorno == 5: return False
    if giorno == 6 and ora < 23: return False
    return True

def is_orario_sessione():
    global pausa_bot_fino
    if pausa_bot_fino and datetime.now() < pausa_bot_fino:
        return False
    ora = datetime.now().hour
    return SESSIONE_START <= ora < SESSIONE_END

def check_news_block():
    adesso = datetime.now()
    if adesso.hour in [10, 11, 14, 15, 16] and adesso.minute < 15:
        return True
    return False

# ---------------------------------------------------------
# 6. FETCH DATI CON VALIDAZIONE VIA TWELVE DATA
# ---------------------------------------------------------
def fetch_candles(symbol, interval, outputsize=100):
    if not TWELVEDATA_API_KEY:
        print("[ERRORE] Manca la chiave API TWELVEDATA_API_KEY.")
        return None, None

    url = "https://api.twelvedata.com/time_series"
    try:
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVEDATA_API_KEY
        }
        r = requests.get(url, params=params, timeout=10).json()

        if "values" not in r:
            return None, None

        raw_values = list(reversed(r["values"]))
        
        candles = []
        for v in raw_values:
            try:
                candles.append({
                    "open":  float(v["open"]),
                    "high":  float(v["high"]),
                    "low":   float(v["low"]),
                    "close": float(v["close"])
                })
            except (ValueError, KeyError):
                continue

        closes = [c["close"] for c in candles]

        if len(closes) < (outputsize * 0.6):
            return None, None

        price = closes[-1]
        if price <= 0 or price > 1000:
            return None, None

        return closes, candles

    except:
        return None, None

# ---------------------------------------------------------
# 7. CALCOLO INDICATORI TECNICI
# ---------------------------------------------------------
def compute_ema(prices, period):
    if len(prices) < period:
        return mean(prices) if prices else None
    k = 2 / (period + 1)
    ema = mean(prices[:period])
    for price in prices[period:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def compute_macd_veloce(closes, symbol, fast=12, slow=26, signal=9):
    if len(closes) < slow:
        return 0, 0, 0
    fast_ema  = compute_ema(closes, fast)
    slow_ema  = compute_ema(closes, slow)
    macd_line = fast_ema - slow_ema
    if symbol not in macd_memoria:
        macd_memoria[symbol] = []
    macd_memoria[symbol].append(macd_line)
    if len(macd_memoria[symbol]) > 50:
        macd_memoria[symbol].pop(0)
    signal_line = compute_ema(macd_memoria[symbol], signal)
    if signal_line is None:
        signal_line = macd_line
    return macd_line, signal_line, (macd_line - signal_line)

def compute_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"]  - candles[i-1]["close"])
        )
        trs.append(tr)
    return mean(trs[-period:])

def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    deltas   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [d if d > 0 else 0 for d in deltas[-period:]]
    losses   = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = mean(gains) if gains else 0
    avg_loss = mean(losses) if losses else 0
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def compute_bollinger(closes, period=20):
    if len(closes) < period:
        return None, None, None
    recent = closes[-period:]
    ma = mean(recent)
    sd = stdev(recent)
    return ma + 2 * sd, ma, ma - 2 * sd

def get_support_resistance(candles):
    highs = [c["high"] for c in candles[-100:]]
    lows  = [c["low"]  for c in candles[-100:]]
    return max(highs), min(lows)

def detect_candle_pattern(candles):
    if len(candles) < 2:
        return "NONE"
    
    c_att = candles[-1]
    c_prec = candles[-2]
    
    body_att      = abs(c_att["close"] - c_att["open"])
    total_range   = c_att["high"] - c_att["low"]
    
    if total_range == 0:
        return "DOJI"
        
    lower_shadow  = min(c_att["open"], c_att["close"]) - c_att["low"]
    upper_shadow  = c_att["high"] - max(c_att["open"], c_att["close"])
    
    if body_att <= (total_range * 0.1):
        return "DOJI"
        
    body_prec = abs(c_prec["close"] - c_prec["open"])
    if body_att > body_prec:
        if c_att["close"] > c_att["open"] and c_prec["close"] < c_prec["open"]:
            if c_att["close"] >= c_prec["open"] and c_att["open"] <= c_prec["close"]:
                return "BULLISH_ENGULFING"
        if c_att["close"] < c_att["open"] and c_prec["close"] > c_prec["open"]:
            if c_att["close"] <= c_prec["open"] and c_att["open"] >= c_prec["close"]:
                return "BEARISH_ENGULFING"

    if lower_shadow >= (body_att * 2) and upper_shadow <= (total_range * 0.2):
        return "HAMMER"
    if upper_shadow >= (body_att * 2) and lower_shadow <= (total_range * 0.2):
        return "SHOOTING_STAR"
        
    return "NONE"

# ---------------------------------------------------------
# 8. FILTRI MULTI-TIMEFRAME AVANZATI (H1 & H4)
# ---------------------------------------------------------
def get_ema20_h4_reale(symbol):
    closes_4h, _ = fetch_candles(symbol, "4h", outputsize=40)
    if closes_4h is None:
        return None
    return compute_ema(closes_4h, 20)

def get_ema200_h1(symbol):
    closes_1h, _ = fetch_candles(symbol, "1h", outputsize=220)
    if closes_1h is None:
        return None
    return compute_ema(closes_1h, 200)

# ---------------------------------------------------------
# 9. MOTORE DI CALCOLO MATRICE CON FILTRO VOLATILITÀ
# ---------------------------------------------------------
def calcola_matrice_asset(symbol):
    closes_15m, candles_15m = fetch_candles(symbol, "15m", outputsize=100)
    if closes_15m is None:
        return None

    closes_1h, _ = fetch_candles(symbol, "1h", outputsize=80)
    if closes_1h is None:
        return None

    ema200_h1 = get_ema200_h1(symbol)
    ema20_h4 = get_ema20_h4_reale(symbol)
    if ema20_h4 is None:
        ema20_h4 = compute_ema(closes_1h, 80)

    price      = closes_15m[-1]
    ema50_15m  = compute_ema(closes_15m, 50)
    ema50_1h   = compute_ema(closes_1h, 50)
    rsi        = compute_rsi(closes_15m)
    bb_upper, bb_ma, bb_lower = compute_bollinger(closes_15m)
    _, _, macd_hist       = compute_macd_veloce(closes_15m, symbol)
    res_max, sup_min      = get_support_resistance(candles_15m)
    pattern    = detect_candle_pattern(candles_15m)
    atr        = compute_atr(candles_15m)

    if any(v is None for v in [ema50_15m, ema50_1h, bb_upper, bb_lower, atr]):
        return {"symbol": symbol, "punti": 0, "direzione": "NESSUNO", "price": price, "msg": "Inizializzazione dati..."}

    # FILTRO VOLATILITÀ
    larghezza_bb_pips = (bb_upper - bb_lower) * 10000
    if larghezza_bb_pips < 8.0:
        return {"symbol": symbol, "punti": 0, "direzione": "NESSUNO", "price": price, "msg": "❌ Bloccato: Volatilità bassa (BB Squeeze)"}

    dir_base = "NESSUNO"
    if price > ema50_15m and price > ema50_1h:
        dir_base = "LONG"
    elif price < ema50_15m and price < ema50_1h:
        dir_base = "SHORT"

    if dir_base == "NESSUNO":
        return {"symbol": symbol, "punti": 0, "direzione": "NESSUNO", "price": price, "msg": "Trend M15/H1 disallineato"}

    # Super Controllo Istituzionale EMA 200 su H1 + EMA 20 su H4
    h4_ok = (dir_base == "LONG" and price > ema20_h4) or (dir_base == "SHORT" and price < ema20_h4)
    if not h4_ok:
        return {"symbol": symbol, "punti": 0, "direzione": dir_base, "price": price, "msg": "❌ Bloccato: Contro Trend H4"}
        
    if ema200_h1:
        h1_200_ok = (dir_base == "LONG" and price > ema200_h1) or (dir_base == "SHORT" and price < ema200_h1)
        if not h1_200_ok:
            return {"symbol": symbol, "punti": 0, "direzione": dir_base, "price": price, "msg": "❌ Bloccato: Contro EMA200 H1"}

    punti  = 2  
    p_bb   = 2 if ((dir_base == "LONG" and price <= bb_lower * 1.001) or (dir_base == "SHORT" and price >= bb_upper * 0.999)) else 0
    p_rsi  = 1 if (40 < rsi < 60) else 0
    p_macd = 2 if ((dir_base == "LONG" and macd_hist > 0) or (dir_base == "SHORT" and macd_hist < 0)) else 0
    
    proximity = (atr * 0.5) if atr else 0.001
    is_near_support = abs(price - sup_min) <= proximity
    is_near_resistance = abs(price - res_max) <= proximity
    p_sr   = 2 if ((dir_base == "LONG" and is_near_support) or (dir_base == "SHORT" and is_near_resistance)) else 0
    
    p_pat  = 0
    if dir_base == "LONG" and (pattern == "HAMMER" or pattern == "BULLISH_ENGULFING" or (pattern == "DOJI" and is_near_support)):
        p_pat = 2
    elif dir_base == "SHORT" and (pattern == "SHOOTING_STAR" or pattern == "BEARISH_ENGULFING" or (pattern == "DOJI" and is_near_resistance)):
        p_pat = 2

    totale = punti + p_bb + p_rsi + p_macd + p_sr + p_pat

    buffer_val = (SPREAD_BUFFER / 10000)
    sl = (price - atr * 1.5) - buffer_val if dir_base == "LONG" else (price + atr * 1.5) + buffer_val
    tp = (price + atr * 2.5) + buffer_val if dir_base == "LONG" else (price - atr * 2.5) - buffer_val
    pip_sl = abs(price - sl) * 10000

    return {
        "symbol":    symbol,
        "punti":     totale,
        "direzione": dir_base,
        "price":     price,
        "sl":        sl,
        "tp":        tp,
        "pip_sl":    pip_sl,
        "atr":       atr,
        "score":     "A+" if totale >= 9 else "A",
        "msg":       f"Filtri superati ({totale}/11) [{pattern}]" if totale >= SOGLIA_APPROVAZIONE else f"Sotto soglia ({SOGLIA_APPROVAZIONE}) [{pattern}]"
    }

def genera_report_ispettivo():
    global pausa_bot_fino
    report = "🔍 *TELEMETRIA FILTRI TWELVE DATA*\n-------------------------\n"
    if pausa_bot_fino and datetime.now() < pausa_bot_fino:
        minuti_rimasti = int((pausa_bot_fino - datetime.now()).total_seconds() / 60)
        report += f"⏸️ *Stato*: BOT IN PAUSA DELIBERATA (Ancora {minuti_rimasti} min)\n-------------------------\n"
    elif segnale_in_attesa["attivo"]:
        report += "⏳ *Stato*: In attesa di conferma utente...\n-------------------------\n"
    elif trade_attivo["aperto"] or trade_attivo["in_attesa_risultato"]:
        report += "⚠️ *Stato*: Ricerca sospesa (Trade a mercato in corso)\n-------------------------\n"
    
    for symbol in SYMBOLS:
        res = calcola_matrice_asset(symbol)
        if res:
            report += f"💱 *{res['symbol']}* | Prezzo: `{res['price']:.5f}`\n"
            report += f"🏷️ Direzione: *{res['direzione']}* | Punti: *{res['punti']}/11*\n"
            report += f"📋 Diagnosi: _{res['msg']}_\n\n"
    return report

# ---------------------------------------------------------
# 10. MONITORAGGIO TRADE ATTIVO CON TRAILING STOP DINAMICO
# ---------------------------------------------------------
def monitora_trade():
    global trade_attivo
    if not trade_attivo["aperto"]:
        return

    closes, _ = fetch_candles(trade_attivo["symbol"], "1min", outputsize=5)
    if closes is None:
        return
    prezzo = closes[-1]

    dir_t      = trade_attivo["direction"]
    entrata    = trade_attivo["entrata"]
    tp         = trade_attivo["tp"]
    sl_attuale = trade_attivo["sl"]
    atr_c      = trade_attivo.get("atr", 0.0015)
    distanza_tp = abs(tp - entrata)

    # 1. Break-Even
    if not trade_attivo["be_fatto"]:
        if (dir_t == "LONG"  and (prezzo - entrata) >= (distanza_tp * 0.5)) or \
           (dir_t == "SHORT" and (entrata - prezzo) >= (distanza_tp * 0.5)):
            trade_attivo["sl"]       = entrata
            trade_attivo["be_fatto"] = True
            send_telegram(f"🛡️ *BREAK-EVEN ATTIVATO* su {trade_attivo['symbol']}.")

    # 2. TRAILING STOP DINAMICO
    if dir_t == "LONG":
        nuovo_sl_inseguimento = prezzo - (atr_c * 1.5)
        if nuovo_sl_inseguimento > trade_attivo["sl"] and prezzo > entrata:
            trade_attivo["sl"] = nuovo_sl_inseguimento
    elif dir_t == "SHORT":
        nuovo_sl_inseguimento = prezzo + (atr_c * 1.5)
        if nuovo_sl_inseguimento < trade_attivo["sl"] and prezzo < entrata:
            trade_attivo["sl"] = nuovo_sl_inseguimento

    tolleranza = 0.00005
    if (dir_t == "LONG"  and prezzo >= tp) or (dir_t == "SHORT" and prezzo <= tp):
        send_telegram(f"🎯 *TARGET RAGGIUNTO* su {trade_attivo['symbol']}! Digita il profitto netto (es: `+2.45`).")
        trade_attivo["in_attesa_risultato"] = True
    elif (dir_t == "LONG"  and prezzo <= (trade_attivo["sl"] - tolleranza)) or \
         (dir_t == "SHORT" and prezzo >= (trade_attivo["sl"] + tolleranza)):
        send_telegram(f"🛑 *TRAILING STOP COLPITO* su {trade_attivo['symbol']}! Inserisci l'esito numerico.")
        trade_attivo["in_attesa_risultato"] = True

# ---------------------------------------------------------
# 11. ANALISI E GENERAZIONE SEGNALI
# ---------------------------------------------------------
def esegui_analisi():
    global segnale_in_attesa
    if not is_mercato_aperto() or not is_orario_sessione() or check_news_block():
        return
    if trade_attivo["aperto"] or trade_attivo["in_attesa_risultato"] or segnale_in_attesa["attivo"]:
        return

    for symbol in SYMBOLS:
        res = calcola_matrice_asset(symbol)
        if res and res["punti"] >= SOGLIA_APPROVAZIONE:
            rischio_eur = saldo_virtuale * RISCHIO_BASE * (1.0 if res["punti"] >= 9 else 0.75)
            lotti = round(max((rischio_eur / (res["pip_sl"] * 0.09)) * 0.01, 0.01), 2)

            msg = (
                f"⚠️ *SEGNALE TWELVE DATA - GENERATO*\n"
                f"Asset: *{symbol}* | Tendenza: *{res['direzione']}* ({res['punti']}/11)\n"
                f"Classe Segnale: *{res['score']}*\n"
                f"Ingresso Fineco: `{res['price']:.5f}`\n"
                f"Stop Loss Iniziale: `{res['sl']:.5f}` | Take Profit: `{res['tp']:.5f}`\n"
                f"Volume consigliato: *{lotti} lotti*\n\n"
                f"👉 Digita *'Entrato'* entro 5 minuti per attivare il monitoraggio."
            )
            send_telegram(msg)
            segnale_in_attesa.update({
                "attivo": True,
                "timestamp_generazione": time.time(),
                "data_trade": {
                    "symbol": symbol, "direction": res["direzione"],
                    "entrata": res["price"], "sl": res["sl"],
                    "tp": res["tp"], "atr": res["atr"]
                }
            })
            break

# ---------------------------------------------------------
# 12. LOOP PRINCIPALE INTERATTIVO
# ---------------------------------------------------------
def bot_loop():
    global ultimo_heartbeat_orario, segnale_in_attesa, trade_attivo, pausa_bot_fino, saldo_virtuale

    send_telegram(
        f"🚀 *FOREX BOT v4.2 PRO TWELVE DATA* \n"
        f"- Scansione intelligente attiva 🟢\n"
        f"- Filtro Trend Istituzionale: EMA 200 (H1) + EMA 20 (H4) Abilitato 📈\n"
        f"- Protezione Capitale: Trailing Stop Automatico & Bollinger Squeeze 🛡️\n"
        f"- Crediti salvaguardati (Controllo ogni 15 minuti)"
    )
    invia_report()

    while True:
        adesso_dt = datetime.now()

        if segnale_in_attesa["attivo"]:
            if time.time() - segnale_in_attesa["timestamp_generazione"] > 300:
                send_telegram(f"❌ *SEGNALE SCADUTO* per {segnale_in_attesa['data_trade']['symbol']}. Nessun ingresso confermato.")
                segnale_in_attesa["attivo"] = False

        if adesso_dt.minute == 0 and adesso_dt.hour != ultimo_heartbeat_orario:
            ultimo_heartbeat_orario = adesso_dt.hour
            if is_mercato_aperto() and is_orario_sessione():
                send_telegram(f"⏱️ *HEARTBEAT {adesso_dt.hour}:00* — Scansione Twelve Data attiva 🟢")
                send_telegram(genera_report_ispettivo())

        msg_in = leggi_messaggio_telegram()
        if msg_in:
            parola = msg_in.strip().lower()

            if parola in ["filtri", "stato", "telemetria", "test"]:
                send_telegram(genera_report_ispettivo())
                time.sleep(10)
                continue

            if parola in ["pausa", "sospendi", "stop_analisi"]:
                pausa_bot_fino = datetime.now() + timedelta(hours=2)
                send_telegram("⏸️ *Analisi sospesa per 2 ore*.")
                continue

            if parola in ["riprendi", "attiva", "go_analisi"]:
                pausa_bot_fino = None
                send_telegram("▶️ *Analisi ripresa immediatamente*.")
                continue

            if parola.startswith("saldo "):
                try:
                    nuovo_s = float(parola.split()[1].replace(",", "."))
                    saldo_virtuale = nuovo_s
                    salva_stato()
                    send_telegram(f"💰 *Saldo allineato!* Nuovo capitale: {saldo_virtuale:.2f} EUR")
                    invia_report()
                except:
                    send_telegram("⚠️ Usa: `Saldo 105.50`")
                continue

            if segnale_in_attesa["attivo"] and parola in ["entrato", "ok", "go", "si", "confermo"]:
                dt = segnale_in_attesa["data_trade"]
                trade_attivo.update({
                    "aperto": True, "symbol": dt["symbol"], "direction": dt["direction"],
                    "entrata": dt["entrata"], "sl": dt["sl"], "tp": dt["tp"],
                    "be_fatto": False, "max_prezzo_raggiunto": dt["entrata"],
                    "min_prezzo_raggiunto": dt["entrata"], "atr": dt["atr"],
                    "in_attesa_risultato": False
                })
                segnale_in_attesa["attivo"] = False
                send_telegram(f"🚀 Trade su {dt['symbol']} registrato. Trailing attivi.")
                time.sleep(10)
                continue

            if not msg_in.startswith("/"):
                if trade_attivo["in_attesa_risultato"] or trade_attivo["aperto"]:
                    registra_risultato(msg_in)
                else:
                    send_telegram(f"🤖 Scrivi 'Filtri' per la telemetria.")
                time.sleep(10)
                continue

        if trade_attivo["aperto"] and not trade_attivo["in_attesa_risultato"]:
            monitora_trade()
            time.sleep(MONITOR_MIN * 60)
        else:
            esegui_analisi()
            time.sleep(15 * 60) 

if __name__ == "__main__":
    t = Thread(target=bot_loop)
    t.daemon = True
    def avvio_ritardato():
        time.sleep(2)
        t.start()
    Thread(target=avvio_ritardato).start()
    run_flask()
