"""Minimal Raydium APR monitor for Railway."""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests


POOL_ID = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"
RAYDIUM_URL = "https://api-v3.raydium.io/pools/info/ids"
DB_PATH = os.path.join("data", "raydium_monitor.db")
LOW_THRESHOLD = 30.0
HIGH_THRESHOLD = 60.0
VOLUME_CHANGE_THRESHOLD = 0.25
VOLUME_CHANGE_MIN_USD = 2_000_000.0
LOW_TURNOVER_THRESHOLD = 0.25
HIGH_TURNOVER_THRESHOLD = 2.0
API_TIMEOUT_SECONDS = 20

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    stream=sys.stdout,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
LOGGER = logging.getLogger("raydium-monitor")


@dataclass
class MonitorState:
    last_valid_apr: Optional[float] = None
    last_state: str = "NORMAL"
    last_alert_apr: Optional[float] = None
    last_valid_volume: Optional[float] = None
    last_alert_volume: Optional[float] = None
    last_volume_state: Optional[str] = None
    consecutive_api_failures: int = 0


@dataclass(frozen=True)
class PoolMetrics:
    apr: float
    volume_24h: float
    tvl: float


class Storage:
    def __init__(self, path: str = DB_PATH) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                apr REAL,
                state TEXT NOT NULL,
                error TEXT
            )
            """
        )
        self._ensure_column("readings", "volume_24h", "REAL")
        self._ensure_column("readings", "tvl", "REAL")
        self._ensure_column("readings", "volume_state", "TEXT")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self.connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def load(self) -> MonitorState:
        values = {
            key: value
            for key, value in self.connection.execute(
                "SELECT key, value FROM monitor_state"
            )
        }
        return MonitorState(
            last_valid_apr=float(values["last_valid_apr"])
            if values.get("last_valid_apr")
            else None,
            last_state=values.get("last_state", "NORMAL"),
            last_alert_apr=float(values["last_alert_apr"])
            if values.get("last_alert_apr")
            else None,
            last_valid_volume=float(values["last_valid_volume"])
            if values.get("last_valid_volume")
            else None,
            last_alert_volume=float(values["last_alert_volume"])
            if values.get("last_alert_volume")
            else None,
            last_volume_state=values.get("last_volume_state") or None,
            consecutive_api_failures=int(values.get("consecutive_api_failures", "0")),
        )

    def save_state(self, state: MonitorState) -> None:
        values = {
            "last_valid_apr": "" if state.last_valid_apr is None else str(state.last_valid_apr),
            "last_state": state.last_state,
            "last_alert_apr": "" if state.last_alert_apr is None else str(state.last_alert_apr),
            "last_valid_volume": "" if state.last_valid_volume is None else str(state.last_valid_volume),
            "last_alert_volume": "" if state.last_alert_volume is None else str(state.last_alert_volume),
            "last_volume_state": state.last_volume_state or "",
            "consecutive_api_failures": str(state.consecutive_api_failures),
        }
        self.connection.executemany(
            "INSERT INTO monitor_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            values.items(),
        )
        self.connection.commit()

    def record(
        self,
        timestamp: str,
        apr: Optional[float],
        state: str,
        error: Optional[str],
        volume_24h: Optional[float] = None,
        tvl: Optional[float] = None,
        volume_state: Optional[str] = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO readings(
                timestamp_utc, apr, state, error, volume_24h, tvl, volume_state
            ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, apr, state, error, volume_24h, tvl, volume_state),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class RaydiumClient:
    def fetch_metrics(self) -> PoolMetrics:
        response = requests.get(
            RAYDIUM_URL,
            params={"ids": POOL_ID},
            timeout=API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Raydium respondió JSON inválido: {response.text[:500]}") from exc

        if payload.get("success") is not True:
            raise RuntimeError(f"Raydium success=false: {json.dumps(payload, ensure_ascii=False)[:1000]}")

        pools = payload.get("data")
        if not isinstance(pools, list):
            raise RuntimeError(f"Esquema inesperado: data no es lista: {json.dumps(payload)[:1000]}")

        pool = next((item for item in pools if isinstance(item, dict) and item.get("id") == POOL_ID), None)
        if pool is None:
            raise RuntimeError(f"No se devolvió exactamente el pool {POOL_ID}: {json.dumps(pools)[:1000]}")

        try:
            apr = parse_metric(pool["day"]["apr"], "pool['day']['apr']")
            volume_24h = parse_metric(pool["day"]["volume"], "pool['day']['volume']")
            tvl = parse_metric(pool["tvl"], "pool['tvl']")
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise RuntimeError(
                "Falta una métrica numérica esperada (day.apr, day.volume o tvl); "
                f"respuesta relevante: {json.dumps(pool, ensure_ascii=False)[:1500]}"
            ) from exc
        return PoolMetrics(apr=apr, volume_24h=volume_24h, tvl=tvl)


class TelegramClient:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

    def send(self, message: str) -> None:
        if not self.token or not self.chat_id:
            raise RuntimeError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
        response = requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": message},
            timeout=API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("ok") is not True:
            raise RuntimeError(f"Telegram respondió ok=false: {json.dumps(payload)[:500]}")


def safe_error(exc: Exception, secret: Optional[str] = None) -> str:
    text = str(exc)
    return text.replace(secret, "<redacted>") if secret else text


def parse_metric(value: Any, field: str) -> float:
    metric = float(value)
    if metric != metric or metric in (float("inf"), float("-inf")):
        raise RuntimeError(f"{field} no es finito: {metric}")
    if metric < 0:
        raise RuntimeError(f"{field} no puede ser negativo: {metric}")
    return metric


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def classify(apr: float) -> str:
    if apr < LOW_THRESHOLD:
        return "LOW_APR"
    if apr <= HIGH_THRESHOLD:
        return "NORMAL"
    if apr <= 100.0:
        return "HIGH_APR"
    if apr <= 150.0:
        return "VERY_HIGH"
    if apr <= 200.0:
        return "EXTREME"
    return "EXTREME_PLUS"


def short_pool_id() -> str:
    return f"{POOL_ID[:6]}...{POOL_ID[-4:]}"


def format_apr(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def format_usd(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000_000:
        return f"USD {value / 1_000_000_000:.2f} B"
    if value >= 1_000_000:
        return f"USD {value / 1_000_000:.2f} M"
    if value >= 1_000:
        return f"USD {value / 1_000:.1f} K"
    return f"USD {value:.2f}"


def turnover_ratio(volume_24h: float, tvl: float) -> Optional[float]:
    return volume_24h / tvl if tvl > 0 else None


def classify_volume(volume_24h: float, tvl: float) -> str:
    ratio = turnover_ratio(volume_24h, tvl)
    if ratio is None:
        return "VOLUME_NORMAL"
    if ratio >= HIGH_TURNOVER_THRESHOLD:
        return "HIGH_TURNOVER"
    if ratio <= LOW_TURNOVER_THRESHOLD:
        return "LOW_TURNOVER"
    return "VOLUME_NORMAL"


def volume_moved(volume_24h: float, reference_volume: Optional[float]) -> bool:
    if reference_volume is None or reference_volume <= 0:
        return False
    absolute_change = abs(volume_24h - reference_volume)
    relative_change = absolute_change / reference_volume
    return (
        relative_change >= VOLUME_CHANGE_THRESHOLD
        and absolute_change >= VOLUME_CHANGE_MIN_USD
    )


def alert_message(metrics: PoolMetrics, previous_apr: Optional[float], state: str) -> str:
    apr = metrics.apr
    change = "n/a" if previous_apr is None else f"{apr - previous_apr:+.1f} pp"
    ratio = turnover_ratio(metrics.volume_24h, metrics.tvl)
    ratio_text = "n/a" if ratio is None else f"{ratio:.2f}x"
    return (
        "Raydium SOL/USDC\n"
        f"Pool: {short_pool_id()}\n"
        f"APR referencia: {format_apr(previous_apr)}\n"
        f"APR actual: {format_apr(apr)}\n"
        f"Cambio: {change}\n"
        f"Volumen 24h: {format_usd(metrics.volume_24h)}\n"
        f"TVL: {format_usd(metrics.tvl)}\n"
        f"Volumen/TVL: {ratio_text}\n"
        f"Estado: {state}\n"
        f"Hora: {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"
    )


def recovery_message(metrics: PoolMetrics, previous_state: str) -> str:
    ratio = turnover_ratio(metrics.volume_24h, metrics.tvl)
    ratio_text = "n/a" if ratio is None else f"{ratio:.2f}x"
    return (
        "Raydium SOL/USDC\n"
        f"Pool: {short_pool_id()}\n"
        f"Recuperación desde: {previous_state}\n"
        f"APR actual: {format_apr(metrics.apr)}\n"
        f"Volumen 24h: {format_usd(metrics.volume_24h)}\n"
        f"TVL: {format_usd(metrics.tvl)}\n"
        f"Volumen/TVL: {ratio_text}\n"
        "Estado: NORMAL\n"
        f"Hora: {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"
    )


def volume_alert_message(
    metrics: PoolMetrics,
    reference_volume: Optional[float],
    volume_state: str,
) -> str:
    if reference_volume is None or reference_volume <= 0:
        change = "n/a"
    else:
        change = f"{(metrics.volume_24h / reference_volume - 1) * 100:+.1f}%"
    ratio = turnover_ratio(metrics.volume_24h, metrics.tvl)
    ratio_text = "n/a" if ratio is None else f"{ratio:.2f}x"
    return (
        "Raydium SOL/USDC\n"
        f"Pool: {short_pool_id()}\n"
        f"Volumen referencia: {format_usd(reference_volume)}\n"
        f"Volumen 24h actual: {format_usd(metrics.volume_24h)}\n"
        f"Cambio volumen: {change}\n"
        f"TVL: {format_usd(metrics.tvl)}\n"
        f"Volumen/TVL: {ratio_text}\n"
        f"Estado volumen: {volume_state}\n"
        f"Hora: {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"
    )


def should_alert(apr: float, previous_state: str, new_state: str, last_alert_apr: Optional[float]) -> bool:
    if new_state != previous_state:
        return True
    return (
        new_state != "NORMAL"
        and last_alert_apr is not None
        and abs(apr - last_alert_apr) >= 20.0
    )


def process_cycle(client: RaydiumClient, telegram: TelegramClient, storage: Storage, state: MonitorState) -> None:
    timestamp = utc_now().isoformat()
    previous_apr = state.last_valid_apr
    previous_state = state.last_state
    try:
        metrics = client.fetch_metrics()
    except Exception as exc:
        state.consecutive_api_failures += 1
        error = str(exc)
        LOGGER.error("Raydium API falló (%s/3): %s", state.consecutive_api_failures, error)
        storage.record(timestamp, None, "API_DEGRADED", error)
        if state.consecutive_api_failures == 3 and previous_state != "API_DEGRADED":
            try:
                telegram.send(
                    "Raydium SOL/USDC\n"
                    f"Pool: {short_pool_id()}\n"
                    "Estado: API_DEGRADED\n"
                    "La API falló 3 ciclos consecutivos. Se reintentará en el próximo ciclo.\n"
                    f"Hora: {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"
                )
                LOGGER.warning("Alerta API_DEGRADED enviada")
            except Exception as telegram_error:
                LOGGER.error("No se pudo enviar alerta API_DEGRADED: %s", safe_error(telegram_error, telegram.token))
            state.last_state = "API_DEGRADED"
        storage.save_state(state)
        return

    state.consecutive_api_failures = 0
    apr = metrics.apr
    new_state = classify(apr)
    new_volume_state = classify_volume(metrics.volume_24h, metrics.tvl)
    ratio = turnover_ratio(metrics.volume_24h, metrics.tvl)
    LOGGER.info(
        "Métricas válidas: APR=%.2f%% estado=%s volumen24h=%s TVL=%s rotación=%s estado_volumen=%s",
        apr,
        new_state,
        format_usd(metrics.volume_24h),
        format_usd(metrics.tvl),
        "n/a" if ratio is None else f"{ratio:.2f}x",
        new_volume_state,
    )

    send_alert = should_alert(apr, previous_state, new_state, state.last_alert_apr)
    recovered = previous_state in {"LOW_APR", "HIGH_APR", "VERY_HIGH", "EXTREME", "EXTREME_PLUS", "API_DEGRADED"} and new_state == "NORMAL"
    if send_alert:
        reference_apr = state.last_alert_apr if state.last_alert_apr is not None else previous_apr
        message = recovery_message(metrics, previous_state) if recovered else alert_message(metrics, reference_apr, new_state)
        try:
            telegram.send(message)
            LOGGER.info("Alerta Telegram enviada: %s", "RECOVERY" if recovered else new_state)
            state.last_alert_apr = None if new_state == "NORMAL" else apr
        except Exception as exc:
            LOGGER.error("No se pudo enviar alerta Telegram: %s", safe_error(exc, telegram.token))

    if state.last_volume_state is None:
        state.last_volume_state = new_volume_state
        state.last_alert_volume = metrics.volume_24h
        LOGGER.info("Referencia inicial de volumen establecida sin alerta")
    else:
        turnover_changed = new_volume_state != state.last_volume_state
        significant_move = volume_moved(metrics.volume_24h, state.last_alert_volume)
        if turnover_changed or significant_move:
            alert_volume_state = new_volume_state if turnover_changed else "VOLUME_MOVE"
            try:
                telegram.send(
                    volume_alert_message(
                        metrics,
                        state.last_alert_volume,
                        alert_volume_state,
                    )
                )
                LOGGER.info("Alerta Telegram enviada: %s", alert_volume_state)
                state.last_alert_volume = metrics.volume_24h
                state.last_volume_state = new_volume_state
            except Exception as exc:
                LOGGER.error(
                    "No se pudo enviar alerta de volumen: %s",
                    safe_error(exc, telegram.token),
                )

    state.last_valid_apr = apr
    state.last_valid_volume = metrics.volume_24h
    state.last_state = new_state
    if new_state == "NORMAL":
        state.last_alert_apr = None
    storage.record(
        timestamp,
        apr,
        new_state,
        None,
        metrics.volume_24h,
        metrics.tvl,
        new_volume_state,
    )
    storage.save_state(state)


def main() -> None:
    interval = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
    if interval <= 0:
        raise ValueError("CHECK_INTERVAL_SECONDS debe ser mayor que cero")

    storage = Storage()
    state = storage.load()
    client = RaydiumClient()
    telegram = TelegramClient()
    running = True

    def stop_handler(signum: int, _frame: Any) -> None:
        nonlocal running
        LOGGER.info("Señal %s recibida; cerrando worker", signum)
        running = False

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    LOGGER.info(
        "Worker iniciado; pool=%s; intervalo=%ss; último APR=%s; estado=%s; último volumen=%s; estado_volumen=%s",
        POOL_ID,
        interval,
        format_apr(state.last_valid_apr),
        state.last_state,
        format_usd(state.last_valid_volume),
        state.last_volume_state or "sin referencia",
    )
    try:
        while running:
            cycle_started = time.monotonic()
            process_cycle(client, telegram, storage, state)
            elapsed = time.monotonic() - cycle_started
            time.sleep(max(0.0, interval - elapsed))
    finally:
        storage.close()
        LOGGER.info("Worker detenido")


if __name__ == "__main__":
    main()
