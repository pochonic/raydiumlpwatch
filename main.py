"""Minimal Raydium APR monitor for Railway."""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
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
BIG_MOVE_PP = 10.0
API_TIMEOUT_SECONDS = 20

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
LOGGER = logging.getLogger("raydium-monitor")


@dataclass
class MonitorState:
    last_valid_apr: Optional[float] = None
    last_state: str = "NORMAL"
    consecutive_api_failures: int = 0


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
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

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
            consecutive_api_failures=int(values.get("consecutive_api_failures", "0")),
        )

    def save_state(self, state: MonitorState) -> None:
        values = {
            "last_valid_apr": "" if state.last_valid_apr is None else str(state.last_valid_apr),
            "last_state": state.last_state,
            "consecutive_api_failures": str(state.consecutive_api_failures),
        }
        self.connection.executemany(
            "INSERT INTO monitor_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            values.items(),
        )
        self.connection.commit()

    def record(self, timestamp: str, apr: Optional[float], state: str, error: Optional[str]) -> None:
        self.connection.execute(
            "INSERT INTO readings(timestamp_utc, apr, state, error) VALUES(?, ?, ?, ?)",
            (timestamp, apr, state, error),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class RaydiumClient:
    def fetch_apr(self) -> float:
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
            apr = pool["day"]["apr"]
            apr = float(apr)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "No existe un APR 24h numérico en pool['day']['apr']; "
                f"respuesta relevante: {json.dumps(pool, ensure_ascii=False)[:1500]}"
            ) from exc
        if apr != apr or apr in (float("inf"), float("-inf")):
            raise RuntimeError(f"APR no finito: {apr}")
        return apr


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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def classify(apr: float, previous_apr: Optional[float]) -> str:
    if apr < LOW_THRESHOLD:
        return "LOW_APR"
    if apr > HIGH_THRESHOLD:
        return "HIGH_APR"
    if previous_apr is not None and abs(apr - previous_apr) >= BIG_MOVE_PP:
        return "BIG_MOVE"
    return "NORMAL"


def short_pool_id() -> str:
    return f"{POOL_ID[:6]}...{POOL_ID[-4:]}"


def format_apr(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def alert_message(apr: float, previous_apr: Optional[float], state: str) -> str:
    change = "n/a" if previous_apr is None else f"{apr - previous_apr:+.1f} pp"
    return (
        "Raydium SOL/USDC\n"
        f"Pool: {short_pool_id()}\n"
        f"APR anterior: {format_apr(previous_apr)}\n"
        f"APR actual: {format_apr(apr)}\n"
        f"Cambio: {change}\n"
        f"Estado: {state}\n"
        f"Hora: {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"
    )


def recovery_message(apr: float, previous_state: str) -> str:
    return (
        "Raydium SOL/USDC\n"
        f"Pool: {short_pool_id()}\n"
        f"Recuperación desde: {previous_state}\n"
        f"APR actual: {format_apr(apr)}\n"
        "Estado: NORMAL\n"
        f"Hora: {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"
    )


def process_cycle(client: RaydiumClient, telegram: TelegramClient, storage: Storage, state: MonitorState) -> None:
    timestamp = utc_now().isoformat()
    previous_apr = state.last_valid_apr
    previous_state = state.last_state
    try:
        apr = client.fetch_apr()
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
    new_state = classify(apr, previous_apr)
    LOGGER.info("APR 24h válido: %.2f%%; estado=%s", apr, new_state)

    should_alert = new_state != previous_state and new_state != "NORMAL"
    recovered = previous_state in {"LOW_APR", "HIGH_APR", "BIG_MOVE", "API_DEGRADED"} and new_state == "NORMAL"
    if should_alert or recovered:
        message = recovery_message(apr, previous_state) if recovered else alert_message(apr, previous_apr, new_state)
        try:
            telegram.send(message)
            LOGGER.info("Alerta Telegram enviada: %s", "RECOVERY" if recovered else new_state)
        except Exception as exc:
            LOGGER.error("No se pudo enviar alerta Telegram: %s", safe_error(exc, telegram.token))

    state.last_valid_apr = apr
    state.last_state = new_state
    storage.record(timestamp, apr, new_state, None)
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
    LOGGER.info("Worker iniciado; pool=%s; intervalo=%ss; último APR=%s; estado=%s", POOL_ID, interval, format_apr(state.last_valid_apr), state.last_state)
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
