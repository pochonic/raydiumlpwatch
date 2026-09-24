"""Monitor del rango STONK/USDC. Ejecutar --once para consultar sin Telegram."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import json
import os
import signal
import sqlite3
import threading

import requests

from main import API_TIMEOUT_SECONDS, LOGGER, RAYDIUM_URL, TelegramClient, parse_metric, safe_error, utc_now
from clmm_position import DEFAULT_NFT, PositionClient

POOL_ID = "G4G5SzkbLFMhoSgHiQNeyJFt75sSDsL1rD8LVyT5xZbU"
STONK = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@dataclass(frozen=True)
class Config:
    lower: float = 0.292925
    upper: float = 0.374617
    near_pct: float = 2.0
    apr_change_pp: float = 50.0
    volume_change_pct: float = 25.0
    tvl_drop_pct: float = 10.0

    def __post_init__(self):
        for key, value in asdict(self).items():
            if parse_metric(value, key) <= 0:
                raise ValueError(f"{key} debe ser positivo")
        if self.lower >= self.upper:
            raise ValueError("CLMM_LOWER debe ser menor que CLMM_UPPER")
        if self.near_pct >= 100 or self.tvl_drop_pct >= 100:
            raise ValueError("near_pct y tvl_drop_pct deben ser menores que 100")

    @classmethod
    def from_env(cls):
        defaults = cls()
        return cls(**{key: float(os.getenv(f"CLMM_{key.upper()}", str(value)))
                      for key, value in asdict(defaults).items()})


def fetch_metrics():
    response = requests.get(RAYDIUM_URL, params={"ids": POOL_ID}, timeout=API_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("Raydium no devolvió success=true")
    pools = payload.get("data")
    if not isinstance(pools, list):
        raise ValueError("Raydium data no es una lista")
    pool = next((p for p in pools if isinstance(p, dict) and p.get("id") == POOL_ID), None)
    if pool is None or pool.get("type") != "Concentrated":
        raise ValueError("No se encontró el pool CLMM exacto")
    a, b = pool["mintA"]["address"], pool["mintB"]["address"]
    if {a, b} != {STONK, USDC}:
        raise ValueError("Los mints no corresponden a STONK/USDC")
    price = parse_metric(pool["price"], "price")
    if price <= 0:
        raise ValueError("Precio no positivo")
    return {
        "price": price if a == STONK else 1 / price,
        "apr": parse_metric(pool["day"]["apr"], "day.apr"),
        "volume": parse_metric(pool["day"]["volume"], "day.volume"),
        "tvl": parse_metric(pool["tvl"], "tvl"),
        "fees": parse_metric(pool["day"]["volumeFee"], "day.volumeFee"),
    }


def classify(price, config):
    if price < config.lower:
        return "OUT_BELOW"
    if price >= config.upper:
        return "OUT_ABOVE"
    low = (price / config.lower - 1) * 100
    high = (config.upper / price - 1) * 100
    if min(low, high) <= config.near_pct:
        return "NEAR_LOWER" if low <= high else "NEAR_UPPER"
    return "IN_RANGE"


def fetch_snapshot():
    metrics = fetch_metrics()
    position = PositionClient(POOL_ID, STONK, USDC).fetch()
    metrics.update(price=position["price"], position=position)
    return metrics


def position_config(metrics, config):
    position = metrics.get("position")
    return replace(config, lower=position["lower"], upper=position["upper"]) if position else config


def snapshot_status(metrics, config):
    position = metrics.get("position")
    if position:
        if int(position["liquidity"]) == 0:
            return "NO_LIQUIDITY"
        if position["tick_current"] < position["tick_lower"]:
            return "OUT_BELOW"
        if position["tick_current"] >= position["tick_upper"]:
            return "OUT_ABOVE"
        # Exact tick state takes precedence over display-price rounding.
        result = classify(metrics["price"], position_config(metrics, config))
        return {"OUT_BELOW": "NEAR_LOWER", "OUT_ABOVE": "NEAR_UPPER"}.get(result, result)
    return classify(metrics["price"], config)


def message(metrics, config, reasons):
    config = position_config(metrics, config)
    price = metrics["price"]
    position = metrics.get("position")
    labels = {
        "IN_RANGE": "Dentro del rango",
        "NEAR_LOWER": "Cerca del límite inferior",
        "NEAR_UPPER": "Cerca del límite superior",
        "OUT_BELOW": "Fuera del rango (por debajo)",
        "OUT_ABOVE": "Fuera del rango (por encima)",
        "NO_LIQUIDITY": "Sin liquidez",
    }
    status = snapshot_status(metrics, config)
    lines = ["Raydium CLMM STONK/USDC", labels.get(status, status)]
    notices = ["API recuperada" if reason == "API_RECOVERED" else reason.capitalize()
               for reason in reasons if reason not in {"INICIO", "CONSULTA", "CAMBIO DE RANGO"}]
    if notices:
        lines.append("Aviso: " + ", ".join(notices))
    lines.extend([
        f"Precio: {price:.4f} USDC",
        f"Rango: {config.lower:.4f} – {config.upper:.4f}",
    ])
    if position:
        lines.extend([
            f"Posición ≈ {position['value_usdc']:.2f} USDC (sin fees)",
            f"Fees pendientes ≈ {position['fee_value_usdc']:.4f} USDC",
        ])
    lines.append(f"APR pool (24h): {metrics['apr']:.1f}%")
    if "CAMBIO VOLUMEN" in reasons:
        lines.append(f"Volumen 24h: USD {metrics['volume']:,.0f}")
    if "CAÍDA TVL" in reasons:
        lines.append(f"TVL: USD {metrics['tvl']:,.0f}")
    return "\n".join(lines)



class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS clmm_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS clmm_readings (id INTEGER PRIMARY KEY, timestamp_utc TEXT, pool_id TEXT, state TEXT, metrics TEXT, error TEXT)")
        self.db.commit()

    def load(self, key):
        row = self.db.execute("SELECT value FROM clmm_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, key, state, status, metrics=None, error=None):
        with self.db:
            self.db.execute("INSERT INTO clmm_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(state)))
            self.db.execute("INSERT INTO clmm_readings(timestamp_utc,pool_id,state,metrics,error) VALUES (?,?,?,?,?)", (utc_now().isoformat(), POOL_ID, status, json.dumps(metrics) if metrics else None, error))


def process_cycle(store, config, telegram, fetch=fetch_snapshot):
    # Changing the range or thresholds starts a separate notification baseline.
    mode = "pool" if fetch is fetch_metrics else "position"
    key = json.dumps([POOL_ID, os.getenv("CLMM_NFT_MINT", DEFAULT_NFT), mode,
                      asdict(config)], sort_keys=True)
    state = store.load(key)
    try:
        metrics = fetch()
    except Exception as exc:
        state["failures"] = state.get("failures", 0) + 1
        error = safe_error(exc, telegram.token)
        LOGGER.error("CLMM API falló (%s/3): %s", state["failures"], error)
        if state["failures"] >= 3 and not state.get("degraded_notified"):
            try:
                telegram.send("Raydium CLMM STONK/USDC\nSin datos: 3 o más consultas fallidas.\nNo se puede confirmar el estado de la posición. Se reintentará.")
                state["degraded_notified"] = True
            except Exception as send_error:
                LOGGER.error("Telegram CLMM: %s", safe_error(send_error, telegram.token))
        store.save(key, state, "API_DEGRADED" if state["failures"] >= 3 else "API_ERROR", error=error)
        return

    status = snapshot_status(metrics, config)
    reasons = []
    if state.get("degraded_notified"):
        reasons.append("API_RECOVERED")
    reference = state.get("notified")
    if reference is None:
        reasons.append("INICIO")
    else:
        if status != state.get("notified_status"):
            reasons.append("CAMBIO DE RANGO")
        if metrics.get("position", {}).get("liquidity") != reference.get("position", {}).get("liquidity"):
            reasons.append("CAMBIO DE LIQUIDEZ")
        if abs(metrics["apr"] - reference["apr"]) >= config.apr_change_pp:
            reasons.append("CAMBIO APR")
        if reference["volume"] > 0 and abs(metrics["volume"] / reference["volume"] - 1) * 100 >= config.volume_change_pct:
            reasons.append("CAMBIO VOLUMEN")
        if reference["tvl"] > 0 and metrics["tvl"] <= reference["tvl"] * (1 - config.tvl_drop_pct / 100):
            reasons.append("CAÍDA TVL")
    state["failures"] = 0
    if reasons:
        try:
            telegram.send(message(metrics, config, reasons))
            state.update(notified=metrics, notified_status=status, degraded_notified=False)
        except Exception as exc:
            # Keep the last delivered baseline so failed alerts retry next cycle.
            LOGGER.error("Telegram CLMM: %s", safe_error(exc, telegram.token))
    LOGGER.info("CLMM precio=%.9f estado=%s APR pool=%.2f%%", metrics["price"], status, metrics["apr"])
    store.save(key, state, status, metrics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Una consulta sin Telegram ni cambios en SQLite")
    parser.add_argument("--pool-only", action="store_true", help="Consultar sólo el pool y usar límites manuales")
    args = parser.parse_args()
    config = Config.from_env()
    fetch = fetch_metrics if args.pool_only else fetch_snapshot
    if args.once:
        print(message(fetch(), config, ["CONSULTA"] ))
        return
    interval = int(os.getenv("CLMM_CHECK_INTERVAL_SECONDS", "60"))
    if interval <= 0:
        raise ValueError("CLMM_CHECK_INTERVAL_SECONDS debe ser positivo")
    telegram = TelegramClient()
    if not telegram.token or not telegram.chat_id:
        raise ValueError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    store = Store(os.getenv("CLMM_DB_PATH", "data/clmm_monitor.db"))
    try:
        while not stop.is_set():
            process_cycle(store, config, telegram, fetch)
            stop.wait(interval)
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
