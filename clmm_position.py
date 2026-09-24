"""Read-only Raydium CLMM position reader.

Layouts and fee arithmetic follow raydium-io/raydium-sdk-V2:
src/raydium/clmm/layout.ts and libraries/{pda,position}.ts.
Only getMultipleAccounts is called; no wallet or transaction signing.
"""
import base64
from decimal import Decimal, localcontext
import hashlib
import os

import requests
from solders.pubkey import Pubkey

PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
DEFAULT_NFT = "BCB7fqJ6BsxP1XsEWLGPAfa5XxjqmW5as1W8vhQB3hJr"
Q64 = 1 << 64
MOD128 = 1 << 128


def uint(data, offset, size=16, signed=False):
    if len(data) < offset + size:
        raise ValueError("Cuenta Solana truncada")
    return int.from_bytes(data[offset:offset + size], "little", signed=signed)


def pubkey(data, offset):
    return str(Pubkey.from_bytes(data[offset:offset + 32]))


def pda(*seeds):
    return str(Pubkey.find_program_address(list(seeds), Pubkey.from_string(PROGRAM))[0])


def decode(account, kind, minimum):
    if account is None:
        raise ValueError(f"Cuenta {kind} ausente; puede estar cerrada o no disponible")
    if account.get("owner") != PROGRAM:
        raise ValueError("La cuenta no pertenece al programa CLMM de Raydium")
    encoded, encoding = account["data"]
    if encoding != "base64":
        raise ValueError("Codificación RPC inesperada")
    data = base64.b64decode(encoded, validate=True)
    discriminator = hashlib.sha256(f"account:{kind}".encode()).digest()[:8]
    if len(data) < minimum or data[:8] != discriminator:
        raise ValueError(f"Layout inválido: {kind}")
    return data


def position_data(account, nft, pool):
    data = decode(account, "PersonalPositionState", 281)
    if pubkey(data, 9) != nft or pubkey(data, 41) != pool:
        raise ValueError("El NFT no corresponde al pool configurado")
    lower, upper = uint(data, 73, 4, True), uint(data, 77, 4, True)
    if not -443636 <= lower < upper <= 443636:
        raise ValueError("Ticks de posición inválidos")
    return dict(lower=lower, upper=upper, liquidity=uint(data, 81),
                last=[uint(data, 97), uint(data, 113)],
                owed=[uint(data, 129, 8), uint(data, 137, 8)])


def pool_data(account, stonk, usdc):
    data = decode(account, "PoolState", 1544)
    # This reader deliberately supports the verified STONK/USDC orientation only.
    if pubkey(data, 73) != stonk or pubkey(data, 105) != usdc:
        raise ValueError("Orden de mints del pool inesperado")
    if data[233:235] != bytes([9, 6]):
        raise ValueError("Decimales del pool inesperados")
    result = dict(spacing=uint(data, 235, 2), sqrt=uint(data, 253),
                  tick=uint(data, 269, 4, True),
                  growth=[uint(data, 277), uint(data, 293)])
    if result["spacing"] <= 0 or result["sqrt"] <= 0:
        raise ValueError("Precio o tick spacing inválido")
    return result


def tick_start(tick, spacing):
    return tick // (spacing * 60) * spacing * 60


def tick_fees(account, pool, tick, spacing):
    data = decode(account, "TickArrayState", 10240)
    start = tick_start(tick, spacing)
    if pubkey(data, 8) != pool or uint(data, 40, 4, True) != start:
        raise ValueError("Tick array no corresponde al pool/rango")
    offset = 44 + ((tick - start) // spacing) * 168
    if uint(data, offset, 4, True) != tick or uint(data, offset + 20) == 0:
        raise ValueError("Tick límite no inicializado")
    return [uint(data, offset + 36), uint(data, offset + 52)]


def pending_fee(global_growth, lower_outside, upper_outside, current, lower, upper,
                last, liquidity, owed):
    below = lower_outside if current >= lower else (global_growth - lower_outside) % MOD128
    above = upper_outside if current < upper else (global_growth - upper_outside) % MOD128
    inside = (global_growth - below - above) % MOD128
    return owed + ((inside - last) % MOD128) * liquidity // Q64


class PositionClient:
    def __init__(self, pool, stonk, usdc, nft=None):
        self.pool, self.stonk, self.usdc = pool, stonk, usdc
        self.nft = nft or os.getenv("CLMM_NFT_MINT", DEFAULT_NFT)
        self.address = pda(b"position", bytes(Pubkey.from_string(self.nft)))
        self.url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

    def accounts(self, addresses, minimum_slot=None):
        options = dict(encoding="base64", commitment="confirmed")
        if minimum_slot is not None:
            options["minContextSlot"] = minimum_slot
        try:
            response = requests.post(self.url, json=dict(jsonrpc="2.0", id=1,
                method="getMultipleAccounts", params=[addresses, options]), timeout=20)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            # RPC URLs may contain API keys. Never propagate URL or response text.
            raise RuntimeError("No se pudo consultar Solana RPC (red/HTTP/JSON)") from None
        if payload.get("error"):
            raise RuntimeError("Solana RPC rechazó la consulta")
        result = payload["result"]
        if len(result["value"]) != len(addresses):
            raise ValueError("Número de cuentas RPC inesperado")
        return result["context"]["slot"], result["value"]

    def fetch(self):
        slot, accounts = self.accounts([self.address, self.pool])
        position = position_data(accounts[0], self.nft, self.pool)
        pool = pool_data(accounts[1], self.stonk, self.usdc)
        bounds = [position["lower"], position["upper"]]
        if any(tick % pool["spacing"] for tick in bounds):
            raise ValueError("Ticks incompatibles con tick spacing")
        addresses = [pda(b"tick_array", bytes(Pubkey.from_string(self.pool)),
                         tick_start(tick, pool["spacing"]).to_bytes(4, "big", signed=True))
                     for tick in bounds]
        # Read pool, position and both boundary arrays from one bank snapshot.
        slot, accounts = self.accounts([self.address, self.pool] + addresses, slot)
        position = position_data(accounts[0], self.nft, self.pool)
        pool = pool_data(accounts[1], self.stonk, self.usdc)
        if bounds != [position["lower"], position["upper"]]:
            raise ValueError("El rango cambió durante la consulta; reintentar")
        if position["liquidity"]:
            outside = [tick_fees(account, self.pool, tick, pool["spacing"])
                       for account, tick in zip(accounts[2:], bounds)]
            fees = [pending_fee(pool["growth"][i], outside[0][i], outside[1][i],
                               pool["tick"], *bounds, position["last"][i],
                               position["liquidity"], position["owed"][i]) for i in range(2)]
        else:
            fees = position["owed"]
        with localcontext() as ctx:
            ctx.prec = 70
            low, high = [Decimal("1.0001") ** tick for tick in bounds]
            sqrt_low, sqrt_high = low.sqrt(), high.sqrt()
            sqrt_price = Decimal(pool["sqrt"]) / Q64
            effective = min(max(sqrt_price, sqrt_low), sqrt_high)
            liquidity = Decimal(position["liquidity"])
            amount_a = liquidity * (sqrt_high - effective) / (effective * sqrt_high) / 10**9
            amount_b = liquidity * (effective - sqrt_low) / 10**6
            price = sqrt_price**2 * 1000
            value = amount_a * price + amount_b
        return dict(nft=self.nft, address=self.address, slot=slot,
                    lower=float(low * 1000), upper=float(high * 1000),
                    tick_lower=bounds[0], tick_upper=bounds[1], tick_current=pool["tick"],
                    liquidity=str(position["liquidity"]), price=float(price),
                    stonk=float(amount_a), usdc=float(amount_b), value_usdc=float(value),
                    fee_stonk=fees[0] / 10**9, fee_usdc=fees[1] / 10**6,
                    fee_value_usdc=float(Decimal(fees[0]) / 10**9 * price + Decimal(fees[1]) / 10**6))
