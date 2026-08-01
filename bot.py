import requests
import time
import os
import json
from datetime import datetime, timedelta
from statistics import mean, stdev
from threading import Thread
from flask import Flask

# ---------------------------------------------------------
# FLASK SERVER
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    ora = datetime.now().strftime("%H:%M:%S")
    wr  = (stats["vinti"] / stats["totali"] * 100) if stats["totali"] > 0 else 0
    return (
        "FOREX BOT ONLINE\n"
        "Ora: {}\n"
        "Saldo: {:.2f} EUR\n"
        "Win Rate: {:.1f}%\n"
        "Trade: {}\n"
        "Trade aperto: {}".format(
            ora, saldo_virtuale, wr,
            stats["totali"],
            trade_attivo["symbol"] if trade_attivo["aperto"] else "Nessuno"
        )
    ), 200

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
# CONFIGURAZIONE GENERALE
# ---------------------------------------------------------
SYMBOLS             = ["EUR/USD", "GBP/USD"]
SALDO_INIZIALE      = 100.0
RISCHIO_BASE        = 0.02
SESSIONE_START      = 9
SESSIONE_END        = 22
SPREAD_BUFFER       = 1.5
SOGLIA_APPROVAZIONE = 7
MONITOR_MIN         = 1
TIMEOUT_SEGNALE_SEC = 300

SESSIONI_OTTIMALI = [
    (9, 11),
    (14, 16),
    (16, 18),
]

# CREDENZIALI INTEGRATE
TELEGRAM_TOKEN      = "8661209874:AAH1x_HQWTo03WQe5fVt70KpBcsTpsFLbT0"
TELEGRAM_CHAT_ID    = "6559735989"
TWELVEDATA_API_KEY  = "f7ad19a1b160485cb773bacfad03543d"

FILE_STORICO        = "storico_saldo.txt"
FILE_STATO          = "stato_bot.json"

# ---------------------------------------------------------
# PERSISTENZA STATO
# ---------------------------------------------------------
def carica_stato():
    default = {
        "saldo_virtuale": 107.89,
        "stats": {"vinti": 18, "persi": 13, "pareggi": 0, "totali": 31}
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
        print("Errore salvataggio: {}".format(e))

_stato         = carica_stato()
saldo_virtuale = _stato["saldo_virtuale"]
stats          = _stato["stats"]

last_update_id         = -1
ultimo_heartbeat_ora   = -1
macd_memoria           = {}
pausa_bot_fino         = None

trade_attivo = {
    "aperto"              : False,
    "symbol"              : None,
    "direction"           : None,
    "entrata"             : None,
    "sl"                  : None,
    "tp"                  : None,
    "be_fatto"            : False,
    "ora_entrata"         : None,
    "atr"                 : 0.0015,
    "in_attesa_risultato" : False
}

segnale_in_attesa = {
    "attivo"               : False,
    "timestamp_generazione": None,
    "data_trade"           : None
}

if not os.path.exists(FILE_STORICO):
    with open(FILE_STORICO, "w") as f:
        f.write("100.0\n107.89\n")

# ---------------------------------------------------------
# FUNZIONI TELEGRAM
# ---------------------------------------------------------
def send_telegram(msg):
    try:
        url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
        requests.post(url, data={
            "chat_id"   : TELEGRAM_CHAT_ID,
            "text"      : msg,
            "parse_mode": "Markdown"
        }, timeout=10)
        print("TG: {}".format(msg[:60]))
    except Exception as e:
        print("Errore TG: {}".format(e))

def send_telegram_foto(photo_path, caption):
    try:
        url = "https://api.telegram.org/bot{}/sendPhoto".format(TELEGRAM_TOKEN)
        with open(photo_path, 'rb') as photo:
            requests.post(url, data={
                "chat_id"   : TELEGRAM_CHAT_ID,
                "caption"   : caption,
                "parse_mode": "Markdown"
            }, files={"photo": photo}, timeout=15)
    except:
        pass

def leggi_messaggio_telegram():
    global last_update_id
    try:
        url = "https://api.telegram.org/bot{}/getUpdates".format(TELEGRAM_TOKEN)
        params = {"offset": last_update_id + 1, "timeout": 2}
        r = requests.get(url, params=params, timeout=8).json()
        if r["result"]:
            for update in r["result"]:
                last_update_id = update["update_id"]
                try:
                    chat_id = str(update["message"]["chat"]["id"])
                    testo   = update["message"]["text"]
                    if chat_id == TELEGRAM_CHAT_ID:
                        return testo
                except:
                    pass
    except:
        pass
    return None

# ---------------------------------------------------------
# GRAFICI E REPORTISTICA
# ---------------------------------------------------------
def genera_e_invia_grafico(testo_report):
    if not HAS_MATPLOTLIB:
        send_telegram(testo_report)
        return
    try:
        with open(FILE_STORICO, "r") as f:
            saldi = [float(line.strip()) for line in f if line.strip()]
        plt.figure(figsize=(8, 4))
        plt.plot(saldi, marker='o', color='#007AFF', linewidth=2, label="Equity Line")
        plt.axhline(y=SALDO_INIZIALE, color='red', linestyle='--', alpha=0.5, label="Saldo iniziale")
        plt.title("Crescita del Capitale")
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

def invia_report():
    wr       = (stats["vinti"] / stats["totali"] * 100) if stats["totali"] > 0 else 0
    profitto = saldo_virtuale - SALDO_INIZIALE
    p_str    = "+{:.2f}".format(profitto) if profitto >= 0 else "{:.2f}".format(profitto)
    msg = (
        "*DIARIO DI TRADING*\n"
        "-------------------------\n"
        "Saldo    : *{:.2f} EUR*\n"
        "Profitto : *{} EUR*\n"
        "Win Rate : *{:.1f}%*\n"
        "-------------------------\n"
        "Vinti    : {}\n"
        "Persi    : {}\n"
        "Pareggi  : {}\n"
        "Totali   : {}"
    ).format(
        saldo_virtuale, p_str, wr,
        stats["vinti"], stats["persi"],
        stats["pareggi"], stats["totali"]
    )
    genera_e_invia_grafico(msg)

def registra_risultato(testo):
    global saldo_virtuale, stats, trade_attivo
    testo = testo.strip().replace(",", ".")
    try:
        profit = float(testo)
    except:
        send_telegram(
            "Formato non riconosciuto.\n\n"
            "Scrivi:\n"
            "+1.50 = guadagno\n"
            "-1.50 = perdita\n"
            "0 = pareggio"
        )
        return False

    saldo_virtuale += profit
    stats["totali"] += 1

    if profit > 0.02:
        stats["vinti"] += 1
        emoji = "VINTO"
    elif profit < -0.02:
        stats["persi"] += 1
        emoji = "PERSO"
    else:
        stats["pareggi"] += 1
        emoji = "PAREGGIO"

    salva_stato()
    with open(FILE_STORICO, "a") as f:
        f.write("{:.2f}\n".format(saldo_virtuale))

    segno = "+" if profit >= 0 else ""
    send_telegram(
        "Registrato: *{}{}EUR* - {}\n"
        "Saldo: *{:.2f} EUR*".format(segno, profit, emoji, saldo_virtuale))
    invia_report()

    trade_attivo["aperto"] = False
    trade_attivo["in_attesa_risultato"] = False
    return True

# ---------------------------------------------------------
# FILTRI TEMPORALI
# ---------------------------------------------------------
def is_mercato_aperto():
    giorno = datetime.now().weekday()
    ora    = datetime.now().hour
    if giorno == 4 and ora >= 23: return False
    if giorno == 5: return False
    if giorno == 6 and ora < 23: return False
    return True

def is_sessione_base():
    global pausa_bot_fino
    if pausa_bot_fino and datetime.now() < pausa_bot_fino:
        return False
    return SESSIONE_START <= datetime.now().hour < SESSIONE_END

def in_sessione_ottimale():
    ora = datetime.now().hour
    for s, e in SESSIONI_OTTIMALI:
        if s <= ora < e:
            return True, "{:02d}:00-{:02d}:00".format(s, e)
    return False, ""

def check_news_block():
    ora = datetime.now()
    if ora.hour in [10, 11, 14, 15, 16] and ora.minute < 15:
        return True
    return False

# ---------------------------------------------------------
# RECUPERO DATI TWELVEDATA
# ---------------------------------------------------------
def fetch_candles(symbol, interval, outputsize=100):
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol"    : symbol,
            "interval"  : interval,
            "outputsize": outputsize,
            "apikey"    : TWELVEDATA_API_KEY
        }
        r = requests.get(url, params=params, timeout=15).json()

        if "values" not in r:
            print("TwelveData errore {}: {}".format(symbol, r.get("message", "?")))
            return None, None

        raw = list(reversed(r["values"]))
        candles = []
        for v in raw:
            try:
                candles.append({
                    "open" : float(v["open"]),
                    "high" : float(v["high"]),
                    "low"  : float(v["low"]),
                    "close": float(v["close"])
                })
            except:
                continue

        closes = [c["close"] for c in candles]
        if len(closes) < outputsize * 0.6:
            return None, None

        return closes, candles
    except Exception as e:
        print("Errore fetch {}: {}".format(symbol, e))
        return None, None

# ---------------------------------------------------------
# CALCOLO INDICATORI MATEMATICI
# ---------------------------------------------------------
def compute_ema(prices, period):
    if len(prices) < period:
        return mean(prices) if prices else None
    k   = 2 / (period + 1)
    ema = mean(prices[:period])
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
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
    return macd_line, signal_line, macd_line - signal_line

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
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
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
    try:
        sd = stdev(recent)
    except:
        return None, None, None
    return ma + 2 * sd, ma, ma - 2 * sd

def get_sr(candles, lookback=100):
    if len(candles) < lookback:
        lookback = len(candles)
    recent = candles[-lookback:]
    return min(c["low"] for c in recent), max(c["high"] for c in recent)

def detect_pattern(candles, direction, atr):
    if len(candles) < 3 or not atr:
        return 0, "Nessun pattern"
    c1         = candles[-2]
    c2         = candles[-3]
    corpo1     = abs(c1["close"] - c1["open"])
    ombra_sup1 = c1["high"] - max(c1["close"], c1["open"])
    ombra_inf1 = min(c1["close"], c1["open"]) - c1["low"]

    if direction == "LONG":
        if ombra_inf1 >= corpo1 * 2 and ombra_sup1 <= corpo1 * 0.5 and corpo1 >= atr * 0.1:
            return 3, "Hammer"
        if (c1["close"] > c1["open"] and c2["close"] < c2["open"] and
                c1["close"] > c2["open"] and c1["open"] < c2["close"]):
            return 3, "Bullish Engulfing"
        if corpo1 < atr * 0.1 and ombra_inf1 >= atr * 0.3:
            return 1, "Doji rialzista"
    elif direction == "SHORT":
        if ombra_sup1 >= corpo1 * 2 and ombra_inf1 <= corpo1 * 0.5 and corpo1 >= atr * 0.1:
            return 3, "Shooting Star"
        if (c1["close"] < c1["open"] and c2["close"] > c2["open"] and
                c1["close"] < c2["open"] and c1["open"] > c2["close"]):
            return 3, "Bearish Engulfing"
        if corpo1 < atr * 0.1 and ombra_sup1 >= atr * 0.3:
            return 1, "Doji ribassista"
    return 0, "Nessun pattern"

# ---------------------------------------------------------
# ALGORITMO DI SELEZIONE MATRICE SCORE
# ---------------------------------------------------------
def calcola_matrice(symbol):
    closes_15m, candles_15m = fetch_candles(symbol, "15min", outputsize=100)
    if closes_15m is None:
        return None, "Dati 15m non disponibili"

    closes_1h, _ = fetch_candles(symbol, "1h", outputsize=220)
    if closes_1h is None:
        return None, "Dati 1H non disponibili"

    closes_4h, _ = fetch_candles(symbol, "4h", outputsize=40)

    price     = closes_15m[-1]
    ema50_15m = compute_ema(closes_15m, 50)
    ema50_1h  = compute_ema(closes_1h, 50)
    ema200_1h = compute_ema(closes_1h, 200) if len(closes_1h) >= 200 else None
    ema20_4h  = compute_ema(closes_4h, 20) if closes_4h else compute_ema(closes_1h, 80)
    atr       = compute_atr(candles_15m)
    rsi       = compute_rsi(closes_15m)

    if not all([ema50_15m, ema50_1h, atr, rsi]):
        return None, "Indicatori non calcolabili"

    bb_upper, _, bb_lower = compute_bollinger(closes_15m)
    if bb_upper and bb_lower:
        larghezza = (bb_upper - bb_lower) * 10000
        if larghezza < 8.0:
            return None, "BB Squeeze - volatilita troppo bassa"

    direction = None
    if price > ema50_15m and price > ema50_1h:
        direction = "LONG"
    elif price < ema50_15m and price < ema50_1h:
        direction = "SHORT"

    if direction is None:
        return None, "Trend 15m/1H non allineato"

    if closes_4h and len(closes_4h) >= 20 and ema20_4h:
        if direction == "LONG"  and price < ema20_4h: return None, "Bloccato: contro trend 4H"
        if direction == "SHORT" and price > ema20_4h: return None, "Bloccato: contro trend 4H"

    if ema200_1h:
        if direction == "LONG"  and price < ema200_1h: return None, "Bloccato: sotto EMA200 H1"
        if direction == "SHORT" and price > ema200_1h: return None, "Bloccato: sopra EMA200 H1"

    if direction == "LONG"  and rsi > 75: return None, "RSI ipercomprato {:.1f}".format(rsi)
    if direction == "SHORT" and rsi < 25: return None, "RSI ipervenduto {:.1f}".format(rsi)

    if direction == "LONG":
        sl = price - atr * 1.5 - (SPREAD_BUFFER / 10000)
        tp = price + atr * 2.5 + (SPREAD_BUFFER / 10000)
    else:
        sl = price + atr * 1.5 + (SPREAD_BUFFER / 10000)
        tp = price - atr * 2.5 - (SPREAD_BUFFER / 10000)

    pip_sl = abs(price - sl) * 10000
    pip_tp = abs(price - tp) * 10000

    if pip_sl < 10: return None, "SL troppo stretto ({:.1f} pip)".format(pip_sl)
    if pip_tp < 15: return None, "TP troppo stretto ({:.1f} pip)".format(pip_tp)

    supporto, resistenza = get_sr(candles_15m)
    if supporto and resistenza:
        if direction == "LONG"  and (resistenza - price) * 10000 < 5:
            return None, "Troppo vicino a resistenza"
        if direction == "SHORT" and (price - supporto) * 10000 < 5:
            return None, "Troppo vicino a supporto"

    bb_ok = False; bb_msg = "Dentro bande"
    if bb_upper and bb_lower:
        if direction == "LONG"  and price <= bb_lower * 1.001:
            bb_ok = True; bb_msg = "Banda inferiore OK"
        elif direction == "SHORT" and price >= bb_upper * 0.999:
            bb_ok = True; bb_msg = "Banda superiore OK"

    _, _, macd_hist = compute_macd_veloce(closes_15m, symbol)
    macd_ok = False; macd_msg = "MACD N/D"
    if macd_hist != 0:
        if direction == "LONG"  and macd_hist > 0: macd_ok = True; macd_msg = "MACD rialzista"
        elif direction == "SHORT" and macd_hist < 0: macd_ok = True; macd_msg = "MACD ribassista"
        else: macd_msg = "MACD contro"

    atr_mean = compute_atr(candles_15m[-50:], min(14, len(candles_15m)-1)) if len(candles_15m) >= 15 else atr
    punti_pattern, nome_pattern = detect_pattern(candles_15m, direction, atr)
    sess_ok, sess_nome          = in_sessione_ottimale()
    rr = pip_tp / pip_sl if pip_sl > 0 else 0

    punti = 3  

    r_atr = (atr / atr_mean) if atr_mean else 1
    if r_atr >= 1.3:   punti += 3
    elif r_atr >= 1.1: punti += 2
    elif r_atr >= 0.9: punti += 1

    if rr >= 2.0:   punti += 2
    elif rr >= 1.5: punti += 1

    if direction == "LONG"  and 40 < rsi < 65: punti += 1
    elif direction == "SHORT" and 35 < rsi < 60: punti += 1

    if bb_ok:   punti += 2
    if macd_ok: punti += 2
    if sess_ok: punti += 2
    punti += punti_pattern

    if punti < SOGLIA_APPROVAZIONE:
        return None, "Score insufficiente ({} punti)".format(punti)

    if punti >= 12:  score = "A+"; molt = 1.0
    elif punti >= 8: score = "A";  molt = 0.75
    else:            score = "B";  molt = 0.5

    if score == "B" and not sess_ok:
        return None, "Score B fuori sessione ottimale"

    rischio_eur  = saldo_virtuale * RISCHIO_BASE * molt
    guadagno_pot = rischio_eur * rr
    units        = rischio_eur / (atr * 1.5) if atr > 0 else 1000
    std          = round(max(units / 100000, 0.01), 2)
    be_level     = price + atr * 1.25 if direction == "LONG" else price - atr * 1.25

    return {
        "symbol"      : symbol,
        "direction"   : direction,
        "price"       : price,
        "sl"          : sl,
        "tp"          : tp,
        "be_level"    : be_level,
        "pip_sl"      : pip_sl,
        "pip_tp"      : pip_tp,
        "rr"          : rr,
        "rsi"         : rsi,
        "atr"         : atr,
        "size"        : std,
        "rischio"     : rischio_eur,
        "guadagno"    : guadagno_pot,
        "score"       : score,
        "punti"       : punti,
        "molt"        : molt,
        "bb_msg"      : bb_msg,
        "macd_msg"    : macd_msg,
        "pattern"     : nome_pattern,
        "sess_nome"   : sess_nome if sess_ok else "Sessione base",
        "supporto"    : supporto,
        "resistenza"  : resistenza
    }, "OK"

# ---------------------------------------------------------
# TELEMETRIA
# ---------------------------------------------------------
def genera_telemetria():
    report = "*TELEMETRIA FILTRI*\n-------------------------\n"

    if pausa_bot_fino and datetime.now() < pausa_bot_fino:
        minuti = int((pausa_bot_fino - datetime.now()).total_seconds() / 60)
        report += "BOT IN PAUSA ({} min rimanenti)\n\n".format(minuti)
    elif segnale_in_attesa["attivo"]:
        report += "In attesa conferma entrata\n\n"
    elif trade_attivo["aperto"]:
        report += "Trade aperto su {}\n\n".format(trade_attivo["symbol"])

    for symbol in SYMBOLS:
        result, motivo = calcola_matrice(symbol)
        report += "Asset: *{}*\n".format(symbol)
        if result is None:
            report += "SKIP: {}\n\n".format(motivo)
        else:
            report += (
                "Direzione : {}\n"
                "Punti     : {}\n"
                "Score     : {}\n"
                "RSI       : {:.1f}\n"
                "BB        : {}\n"
                "MACD      : {}\n"
                "Pattern   : {}\n"
                "Sessione  : {}\n\n"
            ).format(
                result["direction"], result["punti"], result["score"],
                result["rsi"], result["bb_msg"], result["macd_msg"],
                result["pattern"], result["sess_nome"]
            )
    return report

# ---------------------------------------------------------
# INSEGUIMENTO PREZZO / MONITOR
# ---------------------------------------------------------
def monitora_trade():
    global trade_attivo

    if not trade_attivo["aperto"]:
        return

    symbol      = trade_attivo["symbol"]
    direction   = trade_attivo["direction"]
    entrata     = trade_attivo["entrata"]
    sl          = trade_attivo["sl"]
    tp          = trade_attivo["tp"]
    ora_entrata = trade_attivo["ora_entrata"]
    atr         = trade_attivo["atr"]

    closes, _ = fetch_candles(symbol, "1min", outputsize=5)
    if closes is None:
        return
    prezzo = closes[-1]

    pip_profit = (prezzo - entrata) * 10000 if direction == "LONG" else (entrata - prezzo) * 10000
    print("Monitor {}: {:.5f} | {:+.1f} pip".format(symbol, prezzo, pip_profit))

    if (direction == "LONG" and prezzo >= tp) or (direction == "SHORT" and prezzo <= tp):
        send_telegram(
            "*TRADE CHIUSO IN PROFIT!*\n"
            "{} {} | +{:.1f} pip\n\n"
            "Scrivi il guadagno:\n"
            "+2.50 oppure 2.50".format(symbol, direction, abs(pip_profit)))
        trade_attivo["in_attesa_risultato"] = True
        return

    if (direction == "LONG" and prezzo <= sl - 0.00005) or \
       (direction == "SHORT" and prezzo >= sl + 0.00005):
        send_telegram(
            "*TRADE CHIUSO IN LOSS*\n"
            "{} {} | {:.1f} pip\n\n"
            "Scrivi la perdita:\n"
            "-1.50".format(symbol, direction, abs(pip_profit)))
        trade_attivo["in_attesa_risultato"] = True
        return

    if pip_profit > 0:
        if direction == "LONG":
            nuovo_sl = prezzo - atr * 1.5
            if nuovo_sl > trade_attivo["sl"] and nuovo_sl > entrata:
                vecchio = trade_attivo["sl"]
                trade_attivo["sl"] = nuovo_sl
                send_telegram(
                    "*TRAILING STOP* - {}\n"
                    "SL: `{:.5f}` -> `{:.5f}`\n"
                    "Aggiorna su MT5!".format(symbol, vecchio, nuovo_sl))
        elif direction == "SHORT":
            nuovo_sl = prezzo + atr * 1.5
            if nuovo_sl < trade_attivo["sl"] and nuovo_sl < entrata:
                vecchio = trade_attivo["sl"]
                trade_attivo["sl"] = nuovo_sl
                send_telegram(
                    "*TRAILING STOP* - {}\n"
                    "SL: `{:.5f}` -> `{:.5f}`\n"
                    "Aggiorna su MT5!".format(symbol, vecchio, nuovo_sl))

    if not trade_attivo["be_fatto"]:
        be_level = entrata + atr * 1.25 if direction == "LONG" else entrata - atr * 1.25
        if (direction == "LONG" and prezzo >= be_level) or \
           (direction == "SHORT" and prezzo <= be_level):
            trade_attivo["sl"]       = entrata
            trade_attivo["be_fatto"] = True
            send_telegram(
                "*BREAKEVEN ATTIVATO* - {}\n"
                "SL spostato a entrata: `{:.5f}`\n"
                "Profit attuale: +{:.1f} pip".format(symbol, entrata, abs(pip_profit)))

    if ora_entrata:
        minuti = (datetime.now() - ora_entrata).seconds // 60
        if minuti >= 240 and pip_profit <= 0:
            send_telegram(
                "*4 ORE APERTO* - {}\n"
                "Loss: {:.1f} pip\n"
                "Considera chiusura manuale".format(symbol, abs(pip_profit)))

# ---------------------------------------------------------
# CICLO DI ANALISI
# ---------------------------------------------------------
def esegui_analisi():
    global segnale_in_attesa

    if not is_mercato_aperto(): return
    if not is_sessione_base(): return
    if check_news_block():
        return
    if trade_attivo["aperto"] or trade_attivo["in_attesa_risultato"]: return
    if segnale_in_attesa["attivo"]: return

    sess_ok, sess_nome = in_sessione_ottimale()
    risultati = []

    for symbol in SYMBOLS:
        print("Analisi {}...".format(symbol))
        signal, motivo = calcola_matrice(symbol)

        if signal is None:
            risultati.append("{} SKIP: {}".format(symbol, motivo))
            print("SKIP: {}".format(motivo))
        else:
            score = signal["score"]
            if score == "A+":  label = "A+ ENTRA ORA"
            elif score == "A": label = "A ENTRA ORA"
            else:              label = "B VALUTA TU"

            direzione = "LONG (COMPRA)" if signal["direction"] == "LONG" else "SHORT (VENDI)"

            msg = (
                "*SEGNALE FOREX - {}*\n"
                "Score: *{}* ({} punti)\n"
                "DIREZIONE: *{}*\n\n"
                "Entrata  : `{:.5f}`\n"
                "Stop Loss: `{:.5f}` ({:.1f} pip)\n"
                "Take Prof: `{:.5f}` ({:.1f} pip)\n"
                "R/R      : 1:{:.2f}\n\n"
                "Size     : {} lotti\n"
                "Rischio  : -{:.2f} EUR\n"
                "Guadagno : +{:.2f} EUR\n\n"
                "RSI    : {:.1f}\n"
                "BB     : {}\n"
                "MACD   : {}\n"
                "Pattern: {}\n"
                "SR     : S={} R={}\n"
                "Sessione: {}\n\n"
                "Scrivi *Entrato* per attivare monitoraggio\n"
                "Segnale scade in 5 minuti"
            ).format(
                signal["symbol"], label, signal["punti"], direzione,
                signal["price"],
                signal["sl"], signal["pip_sl"],
                signal["tp"], signal["pip_tp"],
                signal["rr"],
                signal["size"], signal["rischio"], signal["guadagno"],
                signal["rsi"], signal["bb_msg"], signal["macd_msg"],
                signal["pattern"],
                "{:.5f}".format(signal["supporto"]) if signal["supporto"] else "N/D",
                "{:.5f}".format(signal["resistenza"]) if signal["resistenza"] else "N/D",
                signal["sess_nome"]
            )
            send_telegram(msg)

            segnale_in_attesa.update({
                "attivo"               : True,
                "timestamp_generazione": time.time(),
                "data_trade"           : signal
            })
            return

# ---------------------------------------------------------
# HEARTBEAT OGNI ORA
# ---------------------------------------------------------
def invia_heartbeat():
    global ultimo_heartbeat_ora
    ora = datetime.now()

    if ora.hour == ultimo_heartbeat_ora:
        return
    ultimo_heartbeat_ora = ora.hour

    sess_ok, sess_nome = in_sessione_ottimale()
    stato_trade = "Trade aperto: *{}*".format(
        trade_attivo["symbol"]) if trade_attivo["aperto"] else "Nessun trade aperto"

    if not is_mercato_aperto():
        send_telegram(
            "*Heartbeat {:02d}:00*\n"
            "Mercato CHIUSO - Weekend\n"
            "{}\n"
            "Saldo: *{:.2f} EUR*".format(ora.hour, stato_trade, saldo_virtuale))
    elif not is_sessione_base():
        send_telegram(
            "*Heartbeat {:02d}:00*\n"
            "Fuori sessione operativa\n"
            "{}\n"
            "Saldo: *{:.2f} EUR*".format(ora.hour, stato_trade, saldo_virtuale))
    else:
        send_telegram(
            "*Heartbeat {:02d}:00*\n"
            "{}\n"
            "{}\n"
            "Saldo: *{:.2f} EUR*\n\n"
            "Scrivi *filtri* per telemetria".format(
                ora.hour,
                "Sessione ottimale: {}".format(sess_nome) if sess_ok else "Sessione base attiva",
                stato_trade,
                saldo_virtuale))

# ---------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------
def bot_loop():
    global segnale_in_attesa, trade_attivo, pausa_bot_fino, saldo_virtuale, stats

    print("FOREX ENGINE AVVIATO SU RENDER (TWELVEDATA)")

    send_telegram(
        "*FOREX ENGINE AVVIATO*\n"
        "*Render Cloud 24/7*\n\n"
        "Saldo: *{:.2f} EUR*\n"
        "Win Rate: *{:.1f}%* ({} trade)\n\n"
        "Comandi disponibili:\n"
        "• Entrato → conferma trade\n"
        "• filtri → telemetria\n"
        "• pausa / riprendi → gestione analisi\n"
        "• saldo X.XX → imposta nuovo saldo\n"
        "• stats V P PA → imposta vinti/persi/pareggi (es: stats 19 13 0)\n"
        "• +X.XX / -X.XX → registra esito singolo trade"
    )
    invia_report()

    while True:
        try:
            invia_heartbeat()

            if segnale_in_attesa["attivo"]:
                if time.time() - segnale_in_attesa["timestamp_generazione"] > TIMEOUT_SEGNALE_SEC:
                    sym = segnale_in_attesa["data_trade"]["symbol"]
                    send_telegram("*Segnale expired* - {}. Scaduto.".format(sym))
                    segnale_in_attesa["attivo"] = False

            msg_in = leggi_messaggio_telegram()
            if msg_in:
                parola = msg_in.strip().lower()

                if parola in ["filtri", "stato", "telemetria", "test"]:
                    send_telegram(genera_telemetria())
                    continue

                if parola in ["pausa", "sospendi"]:
                    pausa_bot_fino = datetime.now() + timedelta(hours=2)
                    send_telegram("⏸️ *Analisi sospesa per 2 ore*.")
                    continue

                if parola in ["riprendi", "attiva"]:
                    pausa_bot_fino = None
                    send_telegram("▶️ *Analisi ripresa immediatamente*.")
                    continue

                # COMANDO IMPOSTA SALDO
                if parola.startswith("saldo "):
                    try:
                        saldo_virtuale = float(parola.split()[1].replace(",", "."))
                        salva_stato()
                        send_telegram("💰 Saldo aggiornato: {:.2f} EUR".format(saldo_virtuale))
                        invia_report()
                    except:
                        send_telegram("⚠️ Formato errato. Usa: `saldo 110.50`")
                    continue

                # NUOVO COMANDO IMPOSTA STATS (vinti, persi, pareggi)
                if parola.startswith("stats "):
                    try:
                        parti = parola.split()
                        vinti = int(parti[1])
                        persi = int(parti[2])
                        pareggi = int(parti[3]) if len(parti) > 3 else 0

                        stats["vinti"] = vinti
                        stats["persi"] = persi
                        stats["pareggi"] = pareggi
                        stats["totali"] = vinti + persi + pareggi
                        
                        salva_stato()
                        send_telegram(
                            "📊 *Statistiche Aggiornate Manualmente*\n"
                            "Vinti: {}\nPersi: {}\nPareggi: {}\nTotali: {}".format(
                                vinti, persi, pareggi, stats["totali"]
                            )
                        )
                        invia_report()
                    except:
                        send_telegram("⚠️ Formato errato. Usa: `stats 19 13 0` (Vinti Persi Pareggi)")
                    continue

                if segnale_in_attesa["attivo"] and parola in ["entrato", "ok", "go", "si", "confermo"]:
                    dt = segnale_in_attesa["data_trade"]
                    trade_attivo.update({
                        "aperto"              : True,
                        "symbol"              : dt["symbol"],
                        "direction"           : dt["direction"],
                        "entrata"             : dt["price"],
                        "sl"                  : dt["sl"],
                        "tp"                  : dt["tp"],
                        "be_fatto"            : False,
                        "ora_entrata"         : datetime.now(),
                        "atr"                 : dt["atr"],
                        "in_attesa_risultato" : False
                    })
                    segnale_in_attesa["attivo"] = False
                    send_telegram("🚀 Trade registrato su {}. Inseguimento attivo.".format(dt["symbol"]))
                    continue

                if trade_attivo["in_attesa_risultato"] or trade_attivo["aperto"]:
                    registra_risultato(msg_in)
                    continue

            if trade_attivo["aperto"] and not trade_attivo["in_attesa_risultato"]:
                monitora_trade()
                time.sleep(MONITOR_MIN * 60)
            else:
                esegui_analisi()
                time.sleep(60)

        except Exception as e:
            print("Errore nel loop: {}".format(e))
            time.sleep(10)

if __name__ == "__main__":
    t = Thread(target=bot_loop)
    t.daemon = True
    t.start()
    run_flask()
