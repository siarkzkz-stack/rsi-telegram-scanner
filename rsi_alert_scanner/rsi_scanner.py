import configparser
import json
import logging
import os
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.cfg"
STATE_DIR = ROOT / ".state"
STATE_FILE = STATE_DIR / "state.json"
HL_INFO = "https://api.hyperliquid.xyz/info"
TG_API = "https://api.telegram.org/bot{}/{}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("rsi-scanner")
INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M"}

def read_config():
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    if not CONFIG.exists():
        raise FileNotFoundError("config.cfg not found")
    cfg.read(CONFIG, encoding="utf-8")
    interval = cfg.get("scanner", "interval", fallback="5m").strip()
    if interval not in INTERVALS:
        raise ValueError("Unsupported interval: " + interval)
    if cfg.getint("scanner", "rsi_period", fallback=14) < 2:
        raise ValueError("rsi_period must be at least 2")
    instruments = {k.strip().upper(): v.strip() for k, v in cfg.items("instruments") if k.strip() and v.strip()}
    if not instruments:
        raise ValueError("No instruments configured")
    return cfg, instruments

def load_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"telegram_offset": 0, "active": [], "last_processed": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("telegram_offset", 0)
        state.setdefault("active", [])
        state.setdefault("last_processed", {})
        return state
    except Exception:
        log.exception("State file invalid; starting empty")
        return {"telegram_offset": 0, "active": [], "last_processed": {}}

def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)

def tg_call(token, method, payload):
    response = requests.post(TG_API.format(token, method), json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram API error: {}".format(data))
    return data

def send_message(token, chat_id, text):
    try:
        tg_call(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        return True
    except Exception:
        log.exception("Telegram send failed")
        return False

def poll_commands(state, token, chat_id, instruments, period, interval, down, up):
    try:
        data = tg_call(token, "getUpdates", {
            "offset": int(state.get("telegram_offset", 0)) + 1,
            "timeout": 0,
            "allowed_updates": ["message"],
        })
    except Exception:
        log.exception("Telegram getUpdates failed")
        return
    active = set(state.get("active", []))
    last_processed = state.setdefault("last_processed", {})
    for update in data.get("result", []):
        state["telegram_offset"] = max(int(state.get("telegram_offset", 0)), int(update.get("update_id", 0)))
        message = update.get("message") or {}
        actual_chat_id = str((message.get("chat") or {}).get("id", ""))
        if actual_chat_id != chat_id:
            log.warning("Ignored command from unapproved chat")
            continue
        text = (message.get("text") or "").strip()
        if not text:
            continue
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        if command in ("/start", "/stop"):
            if len(parts) < 2:
                send_message(token, chat_id, "Укажи тикер: /start TSLA или /stop TSLA")
                continue
            requested_symbol = parts[1].upper()
            symbol = {"SPACEX": "SPCX"}.get(requested_symbol, requested_symbol)
            if symbol not in instruments:
                send_message(token, chat_id, "Неизвестный тикер. Доступны: TSLA и SpaceX.")
                continue
            if command == "/start":
                active.add(symbol)
                last_processed.pop(symbol, None)
                state.setdefault("live_rsi", {}).pop(symbol, None)
                send_message(token, chat_id, "{}: мониторинг включён. RSI({}), {}. Уровни: {} вниз / {} вверх.".format(symbol, period, interval, down, up))
            else:
                active.discard(symbol)
                last_processed.pop(symbol, None)
                state.setdefault("live_rsi", {}).pop(symbol, None)
                send_message(token, chat_id, symbol + ": мониторинг остановлен.")
        elif command == "/status":
            status = ", ".join(sorted(active)) if active else "нет активных инструментов"
            send_message(token, chat_id, "Активный мониторинг: " + status)
        elif command in ("/help", "/commands"):
            send_message(token, chat_id, "Команды:\n/start TSLA\n/stop TSLA\n/start SPCX\n/stop SPCX\n/status")
    state["active"] = sorted(active)

def in_regular_session(cfg):
    if not cfg.getboolean("scanner", "session_only", fallback=True):
        return True
    tz = ZoneInfo(cfg.get("scanner", "session_timezone", fallback="America/New_York"))
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    start = time.fromisoformat(cfg.get("scanner", "session_start", fallback="09:30"))
    end = time.fromisoformat(cfg.get("scanner", "session_end", fallback="16:00"))
    local_time = now.time().replace(tzinfo=None)
    return start <= local_time < end

def interval_duration_ms(interval):
    unit_ms = {"m": 60000, "h": 3600000, "d": 86400000, "w": 604800000, "M": 2592000000}
    return int(interval[:-1]) * unit_ms[interval[-1]]

def get_candles(coin, interval, lookback_days):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - lookback_days * 86400000
    response = requests.post(
        HL_INFO,
        json={"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": now_ms + interval_duration_ms(interval)}},
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    candles = response.json()
    if not isinstance(candles, list):
        raise RuntimeError("Unexpected candle response for " + coin)
    candles = [c for c in candles if isinstance(c, dict) and c.get("c") is not None and c.get("T") is not None and c.get("t") is not None]
    candles.sort(key=lambda c: int(c["t"]))
    return candles

def rsi_series(closes, period):
    result = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        result[i] = value(avg_gain, avg_loss)
    return result

def scan_symbol(symbol, coin, state, token, chat_id, period, interval, down, up, lookback_days):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles = get_candles(coin, interval, lookback_days)
    closed = [c for c in candles if int(c["T"]) <= now_ms]
    forming = [c for c in candles if int(c["t"]) <= now_ms < int(c["T"])]
    forming = max(forming, key=lambda c: int(c["t"])) if forming else None
    if forming is None:
        log.warning("%s: no forming candle in API response; cannot check intrabar RSI", symbol)
        return
    if len(closed) < period + 1:
        log.warning("%s: not enough closed candles for RSI", symbol)
        return

    closed_closes = [float(c["c"]) for c in closed]
    closed_rsi = rsi_series(closed_closes, period)[-1]
    current_rsi = rsi_series(closed_closes + [float(forming["c"])], period)[-1]
    if closed_rsi is None or current_rsi is None:
        return

    live = state.setdefault("live_rsi", {})
    saved = live.get(symbol)
    candle_start = int(forming["t"])
    candle_time = datetime.fromtimestamp(
        candle_start / 1000, tz=timezone.utc
    ).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")

    if not saved:
        live[symbol] = {
            "candle_start": candle_start,
            "rsi": current_rsi,
            "previous_closed_rsi": closed_rsi,
            "alerted_down": False,
            "alerted_up": False,
        }
        log.info("%s: live RSI baseline set to %.2f", symbol, current_rsi)
        return

    # The previous 5m candle has just closed: confirm only a signal that
    # was already reported intrabar and actually remained beyond the level.
    if int(saved.get("candle_start", -1)) != candle_start:
        previous_start = int(saved.get("candle_start", -1))
        last_closed = next((c for c in closed if int(c["t"]) == previous_start), None)
        if last_closed is not None:
            close_index = next(i for i, c in enumerate(closed) if int(c["t"]) == previous_start)
            closes_to_previous = [float(c["c"]) for c in closed[:close_index + 1]]
            previous_close_rsi = rsi_series(closes_to_previous, period)[-1]
            baseline_rsi = float(saved.get("previous_closed_rsi", previous_close_rsi))
            close_price = float(last_closed["c"])
            close_time = datetime.fromtimestamp(
                int(last_closed["T"]) / 1000, tz=timezone.utc
            ).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")

            if saved.get("alerted_down") and baseline_rsi > down and previous_close_rsi is not None and previous_close_rsi <= down:
                send_message(
                    token, chat_id,
                    "✅ {} — ПОДТВЕРЖДЕНИЕ: RSI закрылся ниже {}\n"
                    "ТФ: {} | RSI({}): {:.2f}\n"
                    "Цена закрытия: USD {:,.4f}\n"
                    "Пятиминутная свеча закрылась ({})".format(
                        symbol, down, interval, period, previous_close_rsi, close_price, close_time
                    )
                )
                log.info("%s: 5m close confirmed cross down, RSI %.2f", symbol, previous_close_rsi)
            elif saved.get("alerted_up") and baseline_rsi < up and previous_close_rsi is not None and previous_close_rsi >= up:
                send_message(
                    token, chat_id,
                    "✅ {} — ПОДТВЕРЖДЕНИЕ: RSI закрылся выше {}\n"
                    "ТФ: {} | RSI({}): {:.2f}\n"
                    "Цена закрытия: USD {:,.4f}\n"
                    "Пятиминутная свеча закрылась ({})".format(
                        symbol, up, interval, period, previous_close_rsi, close_price, close_time
                    )
                )
                log.info("%s: 5m close confirmed cross up, RSI %.2f", symbol, previous_close_rsi)

        saved = {
            "candle_start": candle_start,
            "rsi": current_rsi,
            "previous_closed_rsi": closed_rsi,
            "alerted_down": False,
            "alerted_up": False,
        }
        live[symbol] = saved
        return

    previous_rsi = float(saved["rsi"])
    if previous_rsi > down and current_rsi <= down and not saved.get("alerted_down", False):
        msg = (
            "🔻 {} — ПРЕДВАРИТЕЛЬНЫЙ СИГНАЛ: RSI пересёк {} СВЕРХУ ВНИЗ\n"
            "ТФ: {} | RSI({}): {:.2f}\n"
            "Текущая цена: USD {:,.4f}\n"
            "Пятиминутная свеча ещё НЕ закрыта ({})"
        ).format(symbol, down, interval, period, current_rsi, float(forming["c"]), candle_time)
        if send_message(token, chat_id, msg):
            saved["alerted_down"] = True
            log.info("%s: one intrabar cross-down alert sent, RSI %.2f", symbol, current_rsi)
        else:
            log.error("%s: cross-down alert delivery failed", symbol)

    elif previous_rsi < up and current_rsi >= up and not saved.get("alerted_up", False):
        msg = (
            "🔺 {} — ПРЕДВАРИТЕЛЬНЫЙ СИГНАЛ: RSI пересёк {} СНИЗУ ВВЕРХ\n"
            "ТФ: {} | RSI({}): {:.2f}\n"
            "Текущая цена: USD {:,.4f}\n"
            "Пятиминутная свеча ещё НЕ закрыта ({})"
        ).format(symbol, up, interval, period, current_rsi, float(forming["c"]), candle_time)
        if send_message(token, chat_id, msg):
            saved["alerted_up"] = True
            log.info("%s: one intrabar cross-up alert sent, RSI %.2f", symbol, current_rsi)
        else:
            log.error("%s: cross-up alert delivery failed", symbol)

    saved["rsi"] = current_rsi

def run_cycle(cfg, instruments, state, token, chat_id):
    period = cfg.getint("scanner", "rsi_period", fallback=14)
    interval = cfg.get("scanner", "interval", fallback="5m").strip()
    down = cfg.getfloat("levels", "cross_down", fallback=63)
    up = cfg.getfloat("levels", "cross_up", fallback=30)
    lookback_days = cfg.getint("scanner", "lookback_days", fallback=5)
    poll_commands(state, token, chat_id, instruments, period, interval, down, up)
    if in_regular_session(cfg):
        for symbol in state.get("active", []):
            coin = instruments.get(symbol)
            if not coin:
                log.warning("Active symbol %s is not configured", symbol)
                continue
            try:
                scan_symbol(symbol, coin, state, token, chat_id, period, interval, down, up, lookback_days)
            except Exception:
                log.exception("Failed to scan %s", symbol)
    else:
        log.info("Outside regular US session; commands processed, scanning skipped")
    save_state(state)

def main():
    cfg, instruments = read_config()
    token = (os.getenv("RSI_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("RSI_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token:
        log.error("Missing GitHub Secret RSI_TELEGRAM_BOT_TOKEN")
        return 2
    if not chat_id:
        log.error("Missing GitHub Secret RSI_TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID")
        return 2
    state = load_state()
    log.info("Command-controlled RSI monitor started: scans only individually activated instruments, every 60 seconds")
    try:
        while True:
            cycle_started = datetime.now(timezone.utc)
            run_cycle(cfg, instruments, state, token, chat_id)
            elapsed = (datetime.now(timezone.utc) - cycle_started).total_seconds()
            time_to_next_check = max(1, 60 - int(elapsed))
            import time as time_module
            time_module.sleep(time_to_next_check)
    except KeyboardInterrupt:
        log.info("RSI monitor stopped")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
