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
    """Ora corrente nel fuso italiano (indipendente dal fuso del server)."""
    return datetime.now(TZ) if TZ else datetime.now()


# ---------------------------------------------------------
# CONFIGURAZIONE  (i segreti arrivano SOLO da variabili d'ambiente)
# ---------------------------------------------------------
def _env(nome):
    """Legge una variabile d'ambiente ripulendo spazi, a-capo e virgolette
    (errori comuni quando si incolla il valore nel pannello di Render)."""
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

SESSIONI_OTTIMALI = [
    (9, 11),
    (14, 16),
    (16, 18),
]

FILE_STORICO = "storico_saldo.txt"
FILE_STATO   = "stato_bot.json"

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
    default = {
        "saldo_virtuale": 107.89,
        "stats": {"vinti": 18, "persi": 13, "pareggi": 0, "totali": 31}
    }
    if os.path.exists(FILE_STATO):
        try:
            with open(FILE_STATO, "r") as f:
                return json.load(f)
        except Exception:
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

last_update_id       = -1
ultimo_heartbeat_ora = -1
macd_memoria         = {}
pausa_bot_fino       = None

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
    "size"                : 0.01,
    "in_attesa_risultato" : False,
    "step"                : None
}

segnale_in_attesa = {
    "attivo"               : False,
    "timestamp_generazione": None,
    "data_trade"           : None
}

if not os.path.exists(FILE_STORICO):
    with open(FILE_STORICO, "w") as f:
        f.write("100.0\n108.58\n")

# ---------------------------------------------------------
# FLASK SERVER
# ---------------------------------------------------------
app = Flask(__name__)


@app.route('/')
def home():
    ora = adesso().strftime("%H:%M:%S")
    wr  = (stats["vinti"] / stats["totali"] * 100) if stats["totali"] > 0 else 0
    return (
        "FOREX AGENT ONLINE\n"
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


@app.route('/test')
def test_tg():
    """Apri /test dal browser: mostra la risposta reale di Telegram."""
    righe = []
    righe.append("TELEGRAM_TOKEN   : {}".format(
        "MANCANTE" if not TELEGRAM_TOKEN else
        "presente, {} caratteri, inizia con {}...".format(len(TELEGRAM_TOKEN), TELEGRAM_TOKEN[:6])))
    righe.append("TELEGRAM_CHAT_ID : {}".format(TELEGRAM_CHAT_ID or "MANCANTE"))
    righe.append("Thread bot vivo  : {}".format(
        bot_thread is not None and bot_thread.is_alive()))
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        righe.append("\nERRORE: imposta TELEGRAM_TOKEN e TELEGRAM_CHAT_ID nelle Environment di Render")
        return "\n".join(righe), 200

    base = "https://api.telegram.org/bot{}".format(TELEGRAM_TOKEN)
    passi = [
        ("getMe",          lambda: requests.get(base + "/getMe", timeout=10)),
        ("getWebhookInfo", lambda: requests.get(base + "/getWebhookInfo", timeout=10)),
        ("getChat",        lambda: requests.get(base + "/getChat",
                                                params={"chat_id": TELEGRAM_CHAT_ID}, timeout=10)),
        ("sendMessage",    lambda: requests.post(base + "/sendMessage", data={
                                "chat_id": TELEGRAM_CHAT_ID, "text": "test dal bot"}, timeout=10)),
    ]
    for nome, fn in passi:
        try:
            r = fn()
            righe.append("\n[{}] {} {}".format(nome, r.status_code, r.text[:400]))
        except Exception as e:
            righe.append("\n[{}] ERRORE rete: {}".format(nome, e))
    righe.append("\nLegenda: 401=token errato/revocato | 400 'chat not found'=chat_id errato "
                 "o non hai mai premuto Start sul bot | 403=bot bloccato")
    return "\n".join(righe), 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ---------------------------------------------------------
# CARICA MODELLO ML
# ---------------------------------------------------------
ML_MODEL    = None
ML_FEATURES = None


def carica_modello():
    global ML_MODEL, ML_FEATURES
    try:
        with open("forex_model.pkl", "rb") as f:
            data        = pickle.load(f)
            ML_MODEL    = data["model"]
            ML_FEATURES = data["features"]
        print("Modello ML caricato", flush=True)
        return True
    except Exception as e:
        print("Modello ML non disponibile: {}".format(e), flush=True)
        return False


def predici_ml(closes, highs, lows, opens):
    """
    Predizione ML v3.1 - 31 feature, bilanciato LONG/SHORT.
    Soglia 55%: accuracy 82% | SHORT recall 81%.
    """
    if ML_MODEL is None or len(closes) < 30:
        return None, 0.0
    try:
        c = pd.Series(closes)
        h = pd.Series(highs)
        l = pd.Series(lows)
        o = pd.Series(opens)

        # EMA
        ema8   = c.ewm(span=8,   adjust=False).mean()
        ema21  = c.ewm(span=21,  adjust=False).mean()
        ema50  = c.ewm(span=50,  adjust=False).mean()
        ema200 = c.ewm(span=200, adjust=False).mean() if len(closes) >= 200 else ema50

        # RSI 14 e 7
        d = c.diff()
        rsi   = 100-100/(1+d.clip(lower=0).rolling(14).mean()/((-d.clip(upper=0)).rolling(14).mean()+1e-10))
        rsi_f = 100-100/(1+d.clip(lower=0).rolling(7).mean() /((-d.clip(upper=0)).rolling(7).mean() +1e-10))

        # MACD
        ml_s   = c.ewm(span=12, adjust=False).mean()-c.ewm(span=26, adjust=False).mean()
        macd_h = ml_s - ml_s.ewm(span=9, adjust=False).mean()

        # Bollinger
        ma20   = c.rolling(20).mean(); sd20 = c.rolling(20).std()
        bu     = ma20+2*sd20; bl = ma20-2*sd20
        bb_w   = (bu-bl)/(ma20+1e-10)*100
        bb_pos = (c-bl)/(bu-bl+1e-10)
        bb_sq  = bb_w.rolling(20).min()/(bb_w+1e-10)

        # ATR
        tr  = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        a14 = tr.rolling(14).mean()
        a7  = tr.rolling(7).mean()
        a30 = a14.rolling(30).mean()

        # Posizione relativa 50 candele
        high50 = h.rolling(50).max(); low50 = l.rolling(50).min()
        pos50  = (c-low50)/(high50-low50+1e-10)

        # Velocity
        vel3  = c.diff(3)/(a14+1e-10)
        vel10 = c.diff(10)/(a14+1e-10)

        # Up/Down ratio
        up  = d.clip(lower=0).rolling(10).sum()
        dn  = (-d.clip(upper=0)).rolling(10).sum()
        udr = up/(dn+1e-10)

        # Stochastic
        low14 = l.rolling(14).min(); high14 = h.rolling(14).max()
        stk   = (c-low14)/(high14-low14+1e-10)*100

        feat_map = {
            'rsi':       float(rsi.iloc[-1]),
            'rsi_f':     float(rsi_f.iloc[-1]),
            'macd_h':    float(macd_h.iloc[-1]),
            'bb_w':      float(bb_w.iloc[-1]),
            'bb_pos':    float(bb_pos.iloc[-1]),
            'bb_sq':     float(bb_sq.iloc[-1]),
            'p_ema50':   float((c-ema50).iloc[-1]  /(a14.iloc[-1]+1e-10)),
            'p_ema200':  float((c-ema200).iloc[-1] /(a14.iloc[-1]+1e-10)),
            'p_ema8':    float((c-ema8).iloc[-1]   /(a14.iloc[-1]+1e-10)),
            'p_ema21':   float((c-ema21).iloc[-1]  /(a14.iloc[-1]+1e-10)),
            'ema_sp':    float((ema50-ema200).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'ema821':    float((ema8-ema21).iloc[-1]  /(a14.iloc[-1]+1e-10)),
            'body':      float((c-o).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'crange':    float((h-l).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'ush':       float((h-pd.concat([c, o], axis=1).max(axis=1)).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'lsh':       float((pd.concat([c, o], axis=1).min(axis=1)-l).iloc[-1]/(a14.iloc[-1]+1e-10)),
            'mom3':      float(c.pct_change(3).iloc[-1]*100),
            'mom5':      float(c.pct_change(5).iloc[-1]*100),
            'mom10':     float(c.pct_change(10).iloc[-1]*100),
            'mom20':     float(c.pct_change(20).iloc[-1]*100) if len(closes) >= 21 else 0.0,
            'vol_rat':   float(a14.iloc[-1]/(a30.iloc[-1]+1e-10)),
            'vol_ratf':  float(a7.iloc[-1]/(a14.iloc[-1]+1e-10)),
            'rsi_slope': float(rsi.diff(3).iloc[-1]),
            'macd_slope':float(macd_h.diff(3).iloc[-1]),
            'vol_trend': float(a14.pct_change(5).iloc[-1]*100),
            'price_acc': float((c.pct_change(3)-c.pct_change(3).shift(3)).iloc[-1]),
            'stoch_k':   float(stk.iloc[-1]),
            'vel3':      float(vel3.iloc[-1]),
            'vel10':     float(vel10.iloc[-1]),
            'pos50':     float(pos50.iloc[-1]),
            'udr':       float(udr.iloc[-1]),
        }

        # Usa ordine esatto del modello
        if ML_FEATURES:
            row = [feat_map.get(f, 0.0) for f in ML_FEATURES]
        else:
            row = list(feat_map.values())

        if any(np.isnan(v) for v in row):
            return None, 0.0

        X     = np.array([row])
        proba = ML_MODEL.predict_proba(X)[0]
        pred  = int(ML_MODEL.predict(X)[0])
        conf  = float(proba[pred]) * 100

        if conf < 55.0:
            return "NESSUNO", conf

        label_map = {0: "NESSUNO", 1: "LONG", 2: "SHORT"}
        return label_map[pred], conf

    except Exception as e:
        print("Errore ML: {}".format(e), flush=True)
        return None, 0.0


# ---------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------
def send_telegram(msg):
    """Invia un messaggio. Se il Markdown viene rifiutato, riprova senza formattazione."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TG: TELEGRAM_TOKEN o TELEGRAM_CHAT_ID mancanti", flush=True)
        return False
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
    # Telegram accetta max 4096 caratteri per messaggio
    pezzi = [msg[i:i + 4000] for i in range(0, len(msg), 4000)] or [""]
    tutto_ok = True
    for pezzo in pezzi:
        inviato = False
        # 3 tentativi per assorbire errori di rete / rate limit temporanei
        for tentativo in range(3):
            try:
                r = requests.post(url, data={
                    "chat_id"   : TELEGRAM_CHAT_ID,
                    "text"      : pezzo,
                    "parse_mode": "Markdown"
                }, timeout=10)
                if r.status_code == 200:
                    print("TG OK: {}".format(pezzo[:60].replace("\n", " ")), flush=True)
                    inviato = True
                    break

                print("TG ERRORE {}: {}".format(r.status_code, r.text), flush=True)

                if r.status_code == 429:
                    try:
                        attesa = int(r.json().get("parameters", {}).get("retry_after", 2))
                    except Exception:
                        attesa = 2
                    time.sleep(min(attesa, 30))
                    continue

                if r.status_code == 400 and "parse" in r.text.lower():
                    # Markdown non valido: reinvia come testo semplice
                    r2 = requests.post(url, data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text"   : pezzo
                    }, timeout=10)
                    print("TG retry senza Markdown: {} {}".format(
                        r2.status_code, r2.text[:200]), flush=True)
                    if r2.status_code == 200:
                        inviato = True
                    break

                # 401 (token errato), 400 chat not found, 403 bot bloccato:
                # inutile riprovare, l'errore e' di configurazione
                if r.status_code in (400, 401, 403, 404):
                    break
            except Exception as e:
                print("Errore TG (tentativo {}): {}".format(tentativo + 1, e), flush=True)
                time.sleep(2)
        tutto_ok = tutto_ok and inviato
    return tutto_ok


def send_telegram_foto(photo_path, caption):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = "https://api.telegram.org/bot{}/sendPhoto".format(TELEGRAM_TOKEN)
    try:
        with open(photo_path, 'rb') as photo:
            r = requests.post(url, data={
                "chat_id"   : TELEGRAM_CHAT_ID,
                "caption"   : caption,
                "parse_mode": "Markdown"
            }, files={"photo": photo}, timeout=15)
        if r.status_code == 200:
            return True
        print("TG foto ERRORE {}: {}".format(r.status_code, r.text), flush=True)
    except Exception as e:
        print("Errore TG foto: {}".format(e), flush=True)
    # fallback: almeno il testo
    send_telegram(caption)
    return False


def leggi_messaggio_telegram():
    global last_update_id
    try:
        url = "https://api.telegram.org/bot{}/getUpdates".format(TELEGRAM_TOKEN)
        params = {"offset": last_update_id + 1, "timeout": 2}
        r = requests.get(url, params=params, timeout=8).json()
        if not r.get("ok"):
            print("getUpdates errore: {}".format(r), flush=True)
            return None
        for update in r.get("result", []):
            last_update_id = update["update_id"]
            try:
                chat_id = str(update["message"]["chat"]["id"])
                testo   = update["message"]["text"]
                if chat_id == str(TELEGRAM_CHAT_ID):
                    return testo
            except Exception:
                pass
    except Exception as e:
        print("Errore getUpdates: {}".format(e), flush=True)
    return None


# ---------------------------------------------------------
# GRAFICO EQUITY
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
    except Exception as e:
        print("Errore grafico: {}".format(e), flush=True)
        send_telegram(testo_report)


# ---------------------------------------------------------
# REPORT
# ---------------------------------------------------------
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


# ---------------------------------------------------------
# REGISTRA RISULTATO
# ---------------------------------------------------------
def registra_risultato(testo):
    global saldo_virtuale, stats, trade_attivo
    testo = testo.strip().replace(",", ".")
    try:
        profit = float(testo)
    except Exception:
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
        "Registrato: *{}{} EUR* - {}\n"
        "Saldo: *{:.2f} EUR*".format(segno, profit, emoji, saldo_virtuale))
    invia_report()

    trade_attivo["aperto"]              = False
    trade_attivo["in_attesa_risultato"] = False
    trade_attivo["step"]                = None
    return True


# ---------------------------------------------------------
# CONTROLLI MERCATO
# ---------------------------------------------------------
def is_mercato_aperto():
    n      = adesso()
    giorno = n.weekday()
    ora    = n.hour
    if giorno == 4 and ora >= 23: return False
    if giorno == 5: return False
    if giorno == 6 and ora < 23: return False
    return True


def is_sessione_base():
    if pausa_bot_fino and adesso() < pausa_bot_fino:
        return False
    return SESSIONE_START <= adesso().hour < SESSIONE_END


def in_sessione_ottimale():
    ora = adesso().hour
    for s, e in SESSIONI_OTTIMALI:
        if s <= ora < e:
            return True, "{:02d}:00-{:02d}:00".format(s, e)
    return False, ""


def check_news_block():
    ora = adesso()
    if ora.hour in [10, 11, 14, 15, 16] and ora.minute < 15:
        return True
    return False


# ---------------------------------------------------------
# FETCH DATI TWELVEDATA
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
            print("TwelveData {} {}: {}".format(symbol, interval, str(r)[:150]), flush=True)
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
            except Exception:
                continue
        closes = [c["close"] for c in candles]
        if len(closes) < outputsize * 0.6:
            return None, None
        return closes, candles
    except Exception as e:
        print("Errore fetch {}: {}".format(symbol, e), flush=True)
        return None, None


# ---------------------------------------------------------
# INDICATORI
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
    except Exception:
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
# MATRICE SCORE + ML
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
        if (bb_upper - bb_lower) * 10000 < 8.0:
            return None, "BB Squeeze - volatilita bassa"

    direction = None
    if price > ema50_15m and price > ema50_1h:
        direction = "LONG"
    elif price < ema50_15m and price < ema50_1h:
        direction = "SHORT"

    if direction is None:
        return None, "Trend 15m/1H non allineato"

    if ema20_4h:
        if direction == "LONG"  and price < ema20_4h: return None, "Contro trend 4H"
        if direction == "SHORT" and price > ema20_4h: return None, "Contro trend 4H"

    if ema200_1h:
        if direction == "LONG"  and price < ema200_1h: return None, "Sotto EMA200 H1"
        if direction == "SHORT" and price > ema200_1h: return None, "Sopra EMA200 H1"

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
        if direction == "LONG" and price <= bb_lower * 1.001:
            bb_ok = True; bb_msg = "Banda inferiore OK"
        elif direction == "SHORT" and price >= bb_upper * 0.999:
            bb_ok = True; bb_msg = "Banda superiore OK"

    _, _, macd_hist = compute_macd_veloce(closes_15m, symbol)
    macd_ok = False; macd_msg = "MACD N/D"
    if macd_hist != 0:
        if direction == "LONG" and macd_hist > 0:
            macd_ok = True; macd_msg = "MACD rialzista"
        elif direction == "SHORT" and macd_hist < 0:
            macd_ok = True; macd_msg = "MACD ribassista"
        else:
            macd_msg = "MACD contro"

    punti_pattern, nome_pattern = detect_pattern(candles_15m, direction, atr)
    sess_ok, sess_nome          = in_sessione_ottimale()
    rr = pip_tp / pip_sl if pip_sl > 0 else 0

    punti = 3
    atr_list = [compute_atr(candles_15m[max(0, i-14):i], 14) or atr
                for i in range(max(14, len(candles_15m)-30), len(candles_15m))]
    atr_mean = mean(atr_list) if atr_list else atr
    r_atr = atr / (atr_mean + 1e-10)
    if r_atr >= 1.3:   punti += 3
    elif r_atr >= 1.1: punti += 2
    elif r_atr >= 0.9: punti += 1

    if rr >= 2.0:   punti += 2
    elif rr >= 1.5: punti += 1

    if direction == "LONG" and 40 < rsi < 65: punti += 1
    elif direction == "SHORT" and 35 < rsi < 60: punti += 1

    if bb_ok:   punti += 2
    if macd_ok: punti += 2
    if sess_ok: punti += 2
    punti += punti_pattern

    opens = [c["open"] for c in candles_15m]
    highs = [c["high"] for c in candles_15m]
    lows  = [c["low"]  for c in candles_15m]
    ml_pred, ml_conf = predici_ml(closes_15m, highs, lows, opens)
    ml_msg = "ML N/D"
    if ml_pred is not None:
        if ml_pred == direction and ml_conf >= 40:
            ml_bonus = 3 if ml_conf >= 60 else 2
            punti   += ml_bonus
            ml_msg   = "ML {} {:.0f}%".format(ml_pred, ml_conf)
        elif ml_pred == "NESSUNO":
            ml_msg = "ML: laterale"
        else:
            punti -= 1
            ml_msg = "ML contro ({} {:.0f}%)".format(ml_pred, ml_conf)

    if punti < SOGLIA_APPROVAZIONE:
        return None, "Score insufficiente ({} punti)".format(punti)

    if punti >= 14:   score = "A+"; molt = 1.0
    elif punti >= 10: score = "A";  molt = 0.75
    else:             score = "B";  molt = 0.5

    if score == "B" and not sess_ok:
        return None, "Score B fuori sessione ottimale"

    rischio_eur  = saldo_virtuale * RISCHIO_BASE * molt
    guadagno_pot = rischio_eur * rr
    units        = rischio_eur / (atr * 1.5)
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
        "ml_msg"      : ml_msg,
        "sess_nome"   : sess_nome if sess_ok else "Sessione base",
        "supporto"    : supporto,
        "resistenza"  : resistenza
    }, "OK"


# ---------------------------------------------------------
# ISTRUZIONI MT5 STEP BY STEP
# ---------------------------------------------------------
def invia_istruzioni_entrata(signal):
    direzione = "LONG" if signal["direction"] == "LONG" else "SHORT"
    azione    = "COMPRA (Buy)" if direzione == "LONG" else "VENDI (Sell)"
    colore    = "BLU" if direzione == "LONG" else "ROSSO"

    msg = (
        "*AGENTE - ISTRUZIONI ENTRATA*\n"
        "Segui questi passi su MT5:\n\n"
        "*STEP 1 - Apri nuovo ordine*\n"
        "Premi F9 oppure clicca\n"
        "'Crea Nuovo Ordine'\n\n"
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
        "Quando sei entrato scrivi *Entrato*"
    ).format(
        signal["symbol"].replace("/", ""),
        signal["size"],
        signal["sl"],
        signal["tp"],
        azione, colore,
        signal["price"],
        signal["rischio"],
        signal["guadagno"]
    )
    send_telegram(msg)


def invia_istruzioni_trailing(symbol, vecchio_sl, nuovo_sl):
    msg = (
        "*AGENTE - AGGIORNA STOP LOSS*\n\n"
        "Il trailing stop si e spostato!\n\n"
        "*Come aggiornare su MT5:*\n"
        "1. Vai in basso nel pannello ordini\n"
        "2. Clicca destro sul trade aperto\n"
        "3. Seleziona 'Modifica ordine'\n"
        "4. Cambia S/L da `{:.5f}`\n"
        "   a *{:.5f}*\n"
        "5. Clicca 'Modifica'\n\n"
        "Questo protegge il tuo profitto!"
    ).format(vecchio_sl, nuovo_sl)
    send_telegram(msg)


def invia_istruzioni_breakeven(symbol, entrata):
    msg = (
        "*AGENTE - METTI IN BREAKEVEN*\n\n"
        "Hai raggiunto il 50% del TP!\n"
        "Proteggi il trade:\n\n"
        "*Come fare su MT5:*\n"
        "1. Clicca destro sul trade\n"
        "2. Seleziona 'Modifica ordine'\n"
        "3. Cambia S/L a *{:.5f}*\n"
        "   (uguale al prezzo di entrata)\n"
        "4. Clicca 'Modifica'\n\n"
        "Ora il trade non puo andare in perdita!\n\n"
        "Il prezzo ha rotto un massimo/minimo?\n"
        "SI  → sposta subito\n"
        "NO  → aspetta conferma"
    ).format(entrata)
    send_telegram(msg)


def invia_istruzioni_chiusura(symbol, direction, motivo):
    msg = (
        "*AGENTE - CHIUDI IL TRADE*\n\n"
        "Motivo: {}\n\n"
        "*Come chiudere su MT5:*\n"
        "1. Vai nel pannello ordini in basso\n"
        "2. Clicca la X accanto al trade {}\n"
        "3. Conferma la chiusura\n\n"
        "Poi scrivi il risultato qui:\n"
        "+X.XX se guadagno\n"
        "-X.XX se perdita\n"
        "0 se pareggio"
    ).format(motivo, symbol.replace("/", ""))
    send_telegram(msg)


# ---------------------------------------------------------
# TELEMETRIA
# ---------------------------------------------------------
def genera_telemetria():
    report = "*TELEMETRIA AGENTE + ML*\n-------------------------\n"
    report += "ML: {}\n\n".format("Attivo" if ML_MODEL else "Non disponibile")

    if pausa_bot_fino and adesso() < pausa_bot_fino:
        minuti = int((pausa_bot_fino - adesso()).total_seconds() / 60)
        report += "PAUSA ({} min rimanenti)\n\n".format(minuti)
    elif segnale_in_attesa["attivo"]:
        report += "In attesa conferma entrata\n\n"
    elif trade_attivo["aperto"]:
        report += "Trade aperto: {}\n\n".format(trade_attivo["symbol"])

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
                "ML        : {}\n"
                "Sessione  : {}\n\n"
            ).format(
                result["direction"], result["punti"], result["score"],
                result["rsi"], result["bb_msg"], result["macd_msg"],
                result["pattern"], result["ml_msg"], result["sess_nome"]
            )
    return report


# ---------------------------------------------------------
# MONITOR TRADE - AGENTE GUIDA OGNI STEP
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
    print("Monitor {}: {:.5f} | {:+.1f} pip".format(symbol, prezzo, pip_profit), flush=True)

    # TP raggiunto
    if (direction == "LONG" and prezzo >= tp) or (direction == "SHORT" and prezzo <= tp):
        invia_istruzioni_chiusura(symbol, direction, "TARGET RAGGIUNTO!")
        send_telegram(
            "*PROFIT!* +{:.1f} pip\n\n"
            "Dopo aver chiuso scrivi il guadagno:\n"
            "Esempio: +2.50".format(abs(pip_profit)))
        trade_attivo["in_attesa_risultato"] = True
        return

    # SL raggiunto
    if (direction == "LONG" and prezzo <= sl - 0.00005) or \
       (direction == "SHORT" and prezzo >= sl + 0.00005):
        send_telegram(
            "*STOP LOSS COLPITO* su {}\n"
            "Loss: {:.1f} pip\n\n"
            "Il trade e gia chiuso automaticamente\n"
            "da MT5. Scrivi la perdita:\n"
            "Esempio: -1.50".format(symbol, abs(pip_profit)))
        trade_attivo["in_attesa_risultato"] = True
        return

    # trailing stop
    if pip_profit > 0:
        if direction == "LONG":
            nuovo_sl = prezzo - atr * 1.5
            if nuovo_sl > trade_attivo["sl"] and nuovo_sl > entrata:
                vecchio = trade_attivo["sl"]
                trade_attivo["sl"] = nuovo_sl
                invia_istruzioni_trailing(symbol, vecchio, nuovo_sl)
        elif direction == "SHORT":
            nuovo_sl = prezzo + atr * 1.5
            if nuovo_sl < trade_attivo["sl"] and nuovo_sl < entrata:
                vecchio = trade_attivo["sl"]
                trade_attivo["sl"] = nuovo_sl
                invia_istruzioni_trailing(symbol, vecchio, nuovo_sl)

    # breakeven
    if not trade_attivo["be_fatto"]:
        be_level = entrata + atr * 1.25 if direction == "LONG" else entrata - atr * 1.25
        if (direction == "LONG" and prezzo >= be_level) or \
           (direction == "SHORT" and prezzo <= be_level):
            trade_attivo["sl"]       = entrata
            trade_attivo["be_fatto"] = True
            invia_istruzioni_breakeven(symbol, entrata)

    # inversione
    closes_15m, candles_15m = fetch_candles(symbol, "15min", outputsize=20)
    if closes_15m and len(closes_15m) >= 3:
        rsi    = compute_rsi(closes_15m)
        c1     = candles_15m[-2]
        c2     = candles_15m[-3]
        inv = False; motivo_inv = ""
        if direction == "LONG":
            if rsi > 72:
                inv = True; motivo_inv = "RSI ipercomprato {:.1f}".format(rsi)
            elif c1["close"] < c1["open"] and c2["close"] < c2["open"]:
                inv = True; motivo_inv = "2 candele ribassiste"
        elif direction == "SHORT":
            if rsi < 28:
                inv = True; motivo_inv = "RSI ipervenduto {:.1f}".format(rsi)
            elif c1["close"] > c1["open"] and c2["close"] > c2["open"]:
                inv = True; motivo_inv = "2 candele rialziste"

        if inv:
            if pip_profit > 0:
                invia_istruzioni_chiusura(symbol, direction,
                    "Possibile inversione: {}".format(motivo_inv))
                send_telegram(
                    "Sei in profitto di {:.1f} pip\n"
                    "Valuta se chiudere ora per proteggere\n"
                    "oppure aspetta lo SL/TP automatico".format(abs(pip_profit)))
            else:
                send_telegram(
                    "*Possibile inversione* su {}\n"
                    "Motivo: {}\n"
                    "Sei in perdita - aspetta SL automatico\n"
                    "Non chiudere manualmente".format(symbol, motivo_inv))

    # 4 ore
    if ora_entrata:
        minuti = int((adesso() - ora_entrata).total_seconds() // 60)
        if minuti >= 240 and pip_profit <= 0:
            invia_istruzioni_chiusura(symbol, direction,
                "Trade aperto da 4 ore senza profitto")
            send_telegram(
                "Loss attuale: {:.1f} pip\n"
                "Ti conviene chiudere manualmente\n"
                "per limitare le perdite".format(abs(pip_profit)))


# ---------------------------------------------------------
# ESEGUI ANALISI
# ---------------------------------------------------------
def esegui_analisi():
    global segnale_in_attesa

    if not is_mercato_aperto(): return
    if not is_sessione_base(): return
    if check_news_block():
        send_telegram("*FILTRO NEWS*\nRicerca sospesa 15 min")
        return
    if trade_attivo["aperto"] or trade_attivo["in_attesa_risultato"]: return
    if segnale_in_attesa["attivo"]: return

    sess_ok, sess_nome = in_sessione_ottimale()
    risultati = []

    for symbol in SYMBOLS:
        print("Analisi {}...".format(symbol), flush=True)
        signal, motivo = calcola_matrice(symbol)

        if signal is None:
            risultati.append("{} SKIP: {}".format(symbol, motivo))
        else:
            score = signal["score"]
            if score == "A+":  label = "A+ - SEGNALE FORTE"
            elif score == "A": label = "A - SEGNALE BUONO"
            else:              label = "B - VALUTA TU"

            direzione = "LONG (COMPRA)" if signal["direction"] == "LONG" else "SHORT (VENDI)"

            msg_analisi = (
                "*AGENTE - SEGNALE TROVATO*\n"
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
                "Sessione: {}\n\n"
                "Vuoi entrare? Scrivi *si* per le istruzioni\n"
                "oppure *no* per saltare\n"
                "Segnale scade in 5 minuti"
            ).format(
                label, signal["punti"],
                signal["symbol"], direzione,
                signal["price"],
                signal["sl"], signal["pip_sl"],
                signal["tp"], signal["pip_tp"],
                signal["rr"],
                signal["rischio"], signal["guadagno"],
                signal["rsi"], signal["bb_msg"], signal["macd_msg"],
                signal["pattern"], signal["ml_msg"], signal["sess_nome"]
            )
            send_telegram(msg_analisi)

            segnale_in_attesa.update({
                "attivo"               : True,
                "timestamp_generazione": time.time(),
                "data_trade"           : signal
            })
            return

    stato = (
        "*AGENTE - {}*\n"
        "{}\n\n"
        "{}\n\n"
        "Nessun segnale - prossima analisi tra 15 min"
    ).format(
        adesso().strftime("%H:%M"),
        "Sessione ottimale: {}".format(sess_nome) if sess_ok else "Sessione base",
        "\n".join(risultati)
    )
    send_telegram(stato)


# ---------------------------------------------------------
# HEARTBEAT ORARIO
# ---------------------------------------------------------
def invia_heartbeat():
    global ultimo_heartbeat_ora
    ora = adesso()
    if ora.hour == ultimo_heartbeat_ora:
        return
    ultimo_heartbeat_ora = ora.hour

    sess_ok, sess_nome = in_sessione_ottimale()
    stato_trade = "Trade: *{}*".format(
        trade_attivo["symbol"]) if trade_attivo["aperto"] else "Nessun trade"

    if not is_mercato_aperto():
        send_telegram(
            "*Agente {:02d}:00*\n"
            "Mercato CHIUSO - Weekend\n"
            "{}\n"
            "Saldo: *{:.2f} EUR*".format(ora.hour, stato_trade, saldo_virtuale))
    elif not is_sessione_base():
        send_telegram(
            "*Agente {:02d}:00*\n"
            "Fuori sessione\n"
            "{}\n"
            "Saldo: *{:.2f} EUR*".format(ora.hour, stato_trade, saldo_virtuale))
    else:
        send_telegram(
            "*Agente {:02d}:00*\n"
            "{}\n"
            "{}\n"
            "Saldo: *{:.2f} EUR*\n"
            "ML: {}\n\n"
            "Scrivi *filtri* per telemetria".format(
                ora.hour,
                "Sessione ottimale: {}".format(sess_nome) if sess_ok else "Sessione base",
                stato_trade,
                saldo_virtuale,
                "Attivo" if ML_MODEL else "N/D"))


# ---------------------------------------------------------
# BOT LOOP
# ---------------------------------------------------------
def bot_loop():
    global segnale_in_attesa, trade_attivo, pausa_bot_fino, saldo_virtuale

    print("FOREX AGENT AVVIATO", flush=True)

    mancanti = [n for n, v in (("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
                               ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
                               ("TWELVEDATA_API_KEY", TWELVEDATA_API_KEY)) if not v]
    if mancanti:
        print("ATTENZIONE variabili d'ambiente mancanti: {}".format(", ".join(mancanti)), flush=True)

    # se per caso c'e un webhook attivo, getUpdates non funzionerebbe
    try:
        requests.get("https://api.telegram.org/bot{}/deleteWebhook".format(TELEGRAM_TOKEN), timeout=10)
    except Exception:
        pass

    ml_ok = carica_modello()

    send_telegram(
        "*FOREX AGENT AVVIATO*\n"
        "*Render 24/7*\n\n"
        "Saldo: *{:.2f} EUR*\n"
        "Win Rate: *{:.1f}%* ({} trade)\n\n"
        "Come funziona:\n"
        "1. Agente analizza ogni 15 min\n"
        "2. Trova segnale → ti avvisa\n"
        "3. Scrivi SI → istruzioni MT5\n"
        "4. Segui i passi su MT5\n"
        "5. Scrivi Entrato → monitoraggio\n"
        "6. Agente ti guida in tempo reale\n"
        "7. Ti dice quando e come uscire\n\n"
        "ML: {}\n\n"
        "Comandi:\n"
        "si/entrato → conferma trade\n"
        "no → salta segnale\n"
        "filtri → telemetria\n"
        "pausa → sospendi 2 ore\n"
        "riprendi → riattiva\n"
        "saldo X.XX → aggiorna saldo\n"
        "+X.XX/-X.XX → risultato trade".format(
            saldo_virtuale,
            stats["vinti"] / stats["totali"] * 100 if stats["totali"] > 0 else 0,
            stats["totali"],
            "Attivo" if ml_ok else "Non disponibile"
        )
    )
    invia_report()

    prossima_analisi = 0.0
    prossimo_monitor = 0.0

    while True:
        try:
            invia_heartbeat()

            # timeout segnale
            if segnale_in_attesa["attivo"]:
                if time.time() - segnale_in_attesa["timestamp_generazione"] > TIMEOUT_SEGNALE_SEC:
                    sym = segnale_in_attesa["data_trade"]["symbol"]
                    send_telegram(
                        "*Segnale scaduto* - {}\n"
                        "Nessuna risposta ricevuta.\n"
                        "Riprendo la ricerca.".format(sym))
                    segnale_in_attesa["attivo"] = False

            msg_in = leggi_messaggio_telegram()
            if msg_in:
                parola = msg_in.strip().lower()

                # comandi sistema
                if parola in ["filtri", "stato", "telemetria"]:
                    send_telegram(genera_telemetria())
                    continue

                if parola in ["pausa", "sospendi"]:
                    pausa_bot_fino = adesso() + timedelta(hours=2)
                    send_telegram("Agente in pausa per 2 ore.\nScrivi *riprendi* per riattivare.")
                    continue

                if parola in ["riprendi", "attiva"]:
                    pausa_bot_fino = None
                    send_telegram("Agente riattivato!")
                    continue

                if parola.startswith("saldo "):
                    try:
                        nuovo_s = float(parola.split()[1].replace(",", "."))
                        saldo_virtuale = nuovo_s
                        salva_stato()
                        send_telegram("Saldo aggiornato: *{:.2f} EUR*".format(saldo_virtuale))
                        invia_report()
                    except Exception:
                        send_telegram("Usa: saldo 105.50")
                    continue

                # risposta SI al segnale → manda istruzioni MT5
                if segnale_in_attesa["attivo"] and parola in ["si", "s", "yes", "y"]:
                    dt = segnale_in_attesa["data_trade"]
                    invia_istruzioni_entrata(dt)
                    continue

                # NO al segnale → salta
                if segnale_in_attesa["attivo"] and parola in ["no", "n", "skip"]:
                    sym = segnale_in_attesa["data_trade"]["symbol"]
                    segnale_in_attesa["attivo"] = False
                    send_telegram("Segnale {} saltato.\nContinuo a cercare.".format(sym))
                    continue

                # conferma entrata → attiva monitoraggio
                if segnale_in_attesa["attivo"] and parola in ["entrato", "ok", "go", "confermo"]:
                    dt = segnale_in_attesa["data_trade"]
                    trade_attivo.update({
                        "aperto"              : True,
                        "symbol"              : dt["symbol"],
                        "direction"           : dt["direction"],
                        "entrata"             : dt["price"],
                        "sl"                  : dt["sl"],
                        "tp"                  : dt["tp"],
                        "be_fatto"            : False,
                        "ora_entrata"         : adesso(),
                        "atr"                 : dt["atr"],
                        "size"                : dt["size"],
                        "in_attesa_risultato" : False,
                        "step"                : "monitoraggio"
                    })
                    segnale_in_attesa["attivo"] = False
                    prossimo_monitor = 0.0
                    send_telegram(
                        "*Trade attivato!*\n"
                        "{} {}\n"
                        "Volume: {} lotti\n"
                        "SL: `{:.5f}`\n"
                        "TP: `{:.5f}`\n\n"
                        "Monitoro ogni {} minuto\n"
                        "Ti avviso su ogni aggiornamento!\n\n"
                        "Quando il trade si chiude scrivi:\n"
                        "+X.XX guadagno\n"
                        "-X.XX perdita\n"
                        "0 pareggio".format(
                            dt["symbol"], dt["direction"],
                            dt["size"], dt["sl"], dt["tp"],
                            MONITOR_MIN))
                    continue

                # registra risultato
                if not msg_in.startswith("/"):
                    if trade_attivo["in_attesa_risultato"] or trade_attivo["aperto"]:
                        registra_risultato(msg_in)
                    else:
                        send_telegram(
                            "Agente online!\n"
                            "Saldo: *{:.2f} EUR*\n"
                            "ML: {}\n\n"
                            "Scrivi *filtri* per lo stato\n"
                            "Prossima analisi tra poco".format(
                                saldo_virtuale,
                                "Attivo" if ML_MODEL else "N/D"))
                    continue

            # monitor o analisi (a scadenza, senza bloccare la lettura dei messaggi)
            ora_t = time.time()
            if trade_attivo["aperto"]:
                if not trade_attivo["in_attesa_risultato"] and ora_t >= prossimo_monitor:
                    monitora_trade()
                    prossimo_monitor = time.time() + MONITOR_MIN * 60
            else:
                if not trade_attivo["in_attesa_risultato"] and ora_t >= prossima_analisi:
                    esegui_analisi()
                    prossima_analisi = time.time() + 15 * 60

            time.sleep(1)

        except Exception as e:
            print("Errore loop: {}".format(e), flush=True)
            send_telegram("Errore agente: {} - riavvio...".format(str(e)[:50]))
            time.sleep(30)


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
bot_thread = None
_bot_lock_file = None


def _bot_supervisor():
    """Fa ripartire bot_loop se muore per un'eccezione non gestita
    (prima il thread poteva morire in silenzio e non arrivava piu' nulla)."""
    import traceback
    while True:
        try:
            bot_loop()
        except Exception:
            print("bot_loop CRASH:\n{}".format(traceback.format_exc()), flush=True)
        time.sleep(10)


def avvia_bot_una_volta():
    """Avvia il thread del bot UNA sola volta, sia con `python main.py`
    sia con gunicorn (dove il blocco __main__ non viene mai eseguito:
    la pagina web risponde ma il bot non parte e Telegram resta muto)."""
    global bot_thread, _bot_lock_file
    if bot_thread is not None:
        return
    try:
        import fcntl
        _bot_lock_file = open("/tmp/forex_bot.lock", "w")
        fcntl.flock(_bot_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass  # Windows: nessun lock
    except OSError:
        print("Un altro worker gestisce gia' il bot: non avvio un duplicato", flush=True)
        return
    bot_thread = Thread(target=_bot_supervisor, daemon=True)
    bot_thread.start()


# Avvio all'import: funziona con gunicorn (`gunicorn main:app`) e con python
avvia_bot_una_volta()


if __name__ == "__main__":
    run_flask()
