# Raydium APR + Backyard Vault Watcher

## Monitor CLMM STONK/USDC

`clmm_monitor.py` es un worker independiente para el pool
`G4G5SzkbLFMhoSgHiQNeyJFt75sSDsL1rD8LVyT5xZbU`.
Usa el mismo bot que el monitor existente y agrega `solders` para validar
direcciones Solana. Instalar dependencias con `python -m pip install -r requirements.txt`.
El NFT configurado es `BCB7fqJ6BsxP1XsEWLGPAfa5XxjqmW5as1W8vhQB3hJr`.

```powershell
python clmm_monitor.py --once
python clmm_monitor.py
```

`--once` consulta la API y muestra las métricas sin enviar Telegram ni escribir
la base. El modo continuo requiere `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`,
envía un resumen inicial y comprueba cada 60 segundos. Para Railway, crear otro
servicio con Start Command `python clmm_monitor.py`, las variables del bot y un
volumen en `/app/data`. El Procfile existente sigue ejecutando el monitor anterior.

| Variable | Valor predeterminado | Significado |
|---|---|---|
| `CLMM_LOWER` | `0.292925` | Límite inferior, USDC por STONK |
| `CLMM_UPPER` | `0.374617` | Límite superior, USDC por STONK |
| `CLMM_NEAR_PCT` | `2` | Cercanía porcentual al límite |
| `CLMM_APR_CHANGE_PP` | `50` | Cambio de APR en puntos porcentuales |
| `CLMM_VOLUME_CHANGE_PCT` | `25` | Cambio porcentual de volumen 24h |
| `CLMM_TVL_DROP_PCT` | `10` | Caída porcentual de TVL |
| `CLMM_CHECK_INTERVAL_SECONDS` | `60` | Pausa entre consultas |
| `CLMM_DB_PATH` | `data/clmm_monitor.db` | Base SQLite persistente |
| `CLMM_NFT_MINT` | `BCB7fqJ6BsxP1XsEWLGPAfa5XxjqmW5as1W8vhQB3hJr` | NFT de la posición |
| `SOLANA_RPC_URL` | `https://api.mainnet-beta.solana.com` | RPC de lectura Solana; configurable si hay límites de uso |

Alerta al cambiar entre `IN_RANGE`, `NEAR_LOWER`, `NEAR_UPPER`, `OUT_BELOW`
y `OUT_ABOVE`, incluyendo el regreso al rango. El límite superior se considera
fuera de rango. La cercanía inferior se mide como `(precio / inferior - 1) * 100`
y la superior como `(superior / precio - 1) * 100`. Los cambios de APR, volumen
y TVL se comparan con el último resumen entregado; cada resumen exitoso actualiza
esas referencias. No repite el mismo estado sin cambios significativos.
Después de tres fallos consecutivos avisa `API_DEGRADED` y al recuperar datos
envía `API_RECOVERED`. Los envíos fallidos se reintentan en el siguiente ciclo.
Estado e historial se conservan en SQLite. Cambiar rango o umbrales reinicia
las referencias de notificación.

En modo normal, `clmm_position.py` deriva la cuenta de posición desde el NFT,
valida programa, discriminator, mints y pool, y lee posición, pool y ticks límite
en una única respuesta `getMultipleAccounts` con commitment `confirmed`.
El rango proviene de los ticks on-chain y reemplaza los límites manuales.
El estado dentro/fuera se decide por ticks, sin el redondeo de la captura.
Incluye cantidades estimadas de STONK/USDC, valor en USDC al precio del pool y
fees personales pendientes calculados con fee growth y liquidez (no sólo los
fees almacenados en la cuenta). Los saldos usan matemática Decimal y pueden
diferir en unidades mínimas del redondeo entero del contrato; son estimaciones,
no una cotización ejecutable de retiro. Los rewards adicionales no se calculan.
No calcula PnL ni APR personal; el APR, TVL, volumen y fees 24h siguen siendo
métricas generales del pool. Los datos REST y RPC pueden tener distinta antigüedad.

Alerta también por cambios de liquidez y marca `NO_LIQUIDITY` cuando es cero.
Una cuenta ausente se trata como error (posible cierre o indisponibilidad), nunca
como saldo cero confirmado. Si falla RPC no usa silenciosamente el rango manual:
registra el error y aplica las alertas de degradación. No requiere claves privadas,
no conecta wallets ni firma transacciones.

Para consultar sólo el pool usar `python clmm_monitor.py --pool-only --once`
o ejecutar `--pool-only` continuamente. En ese modo los límites manuales provienen
de la captura y están redondeados; deben actualizarse si cambia la posición.
Las consultas son periódicas y no garantizan detectar cruces breves.

Validación: `python -m unittest -v test_clmm_monitor.py test_clmm_position.py`.
Fuente: [API oficial Raydium V3](https://api-v3.raydium.io/docs/).
Layouts y fees: [SDK oficial Raydium](https://github.com/raydium-io/raydium-sdk-V2/tree/master/src/raydium/clmm).

Worker Python mínimo para Railway que consulta exclusivamente la API oficial V3 de Raydium:

`GET https://api-v3.raydium.io/pools/info/ids?ids=58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2`

El parser valida `success`, verifica el ID exacto y lee el APR 24h, el volumen 24h y el TVL desde `data[0].day.apr`, `data[0].day.volume` y `data[0].tvl`. Si el esquema no coincide, registra una muestra acotada de la respuesta y no estima ningún valor.

También monitorea el vault público de Backyard configurado en `BACKYARD_VAULT_ID` (por defecto, Syntropia USDC) mediante `GET https://alpha.api.backyard.finance/vaults/{BACKYARD_VAULT_ID}`. Se validan APY, TVL de Backyard, TVL del protocolo, `lpPrice`, precio del activo y cooldown, usando el mismo bot de Telegram y estado separado en SQLite.

## Archivos

- `main.py`: worker, cliente Raydium, cliente Telegram, reglas y SQLite.
- `requirements.txt`: `requests` y `solders` (lector CLMM on-chain).
- `Procfile`: proceso `worker` para Railway.
- `data/raydium_monitor.db`: base local creada automáticamente y persistida en el volumen del servicio.

## Configuración local

Requiere Python 3.10+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN = "123456:ABC..."
$env:TELEGRAM_CHAT_ID = "123456789"
$env:CHECK_INTERVAL_SECONDS = "300"
$env:BACKYARD_VAULT_ID = "abc49ba6-f259-4e33-9daf-2370b7879cd1"
python main.py
```

Para una prueba rápida se puede usar `CHECK_INTERVAL_SECONDS=30`. El worker ejecuta una lectura inmediatamente al iniciar y después espera el intervalo.

La base se puede inspeccionar con:

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/raydium_monitor.db'); print(*c.execute('select timestamp_utc, apr, state, volume_24h, tvl, volume_state, error from readings order by id desc limit 10'), sep='\n')"
```

## Alertas

- `LOW_APR` si APR < 30%.
- `NORMAL` si APR está entre 30% y 60% inclusive.
- `HIGH_APR` si APR está por encima de 60% y hasta 100%.
- `VERY_HIGH` si APR está por encima de 100% y hasta 150%.
- `EXTREME` si APR está por encima de 150% y hasta 200%.
- `EXTREME_PLUS` si APR > 200%.
- `API_DEGRADED` después de 3 fallos consecutivos.

Se envía alerta al cruzar cada banda, tanto al subir como al bajar. Dentro de una misma banda se alerta sólo cuando el APR se aleja al menos 20 puntos porcentuales desde la última alerta. La última lectura válida, estado, referencia de alerta y contador de fallos se guardan en SQLite para sobrevivir reinicios.

Todas las alertas de APR incluyen volumen 24h, TVL y el ratio volumen/TVL. El volumen se monitorea de forma independiente:

- `VOLUME_MOVE` si cambia al menos 25% y USD 2 millones desde la última alerta de volumen.
- `HIGH_TURNOVER` si volumen/TVL es al menos 2x.
- `LOW_TURNOVER` si volumen/TVL es como máximo 0,25x.
- `VOLUME_NORMAL` en el resto de los casos.

La primera lectura sólo establece la referencia de volumen y no genera una alerta de volumen falsa. La referencia y el estado también sobreviven reinicios mediante SQLite.

## Alertas de Backyard

- `LOW_APY` si APY < 10% y `HIGH_APY` si APY > 20%.
- Cambio de APY de al menos 3 puntos porcentuales dentro de la misma banda.
- Caída del TVL total de al menos 10% respecto de la referencia de alerta.
- Caída del `lpPrice` de al menos 1% respecto de la referencia de alerta.
- `API_DEGRADED` después de 3 fallos consecutivos.

Los umbrales se pueden cambiar con `BACKYARD_LOW_APY_THRESHOLD`, `BACKYARD_HIGH_APY_THRESHOLD`, `BACKYARD_APY_CHANGE_THRESHOLD`, `BACKYARD_TVL_DROP_THRESHOLD` y `BACKYARD_LP_PRICE_DROP_THRESHOLD`. El TVL total es `backyardTvlUsd + protocolTvlUsd`.

Las lecturas se guardan en `backyard_readings` y el estado en `backyard_state`, dentro de la misma base SQLite.

## Crear el bot de Telegram

1. En Telegram, abrir `@BotFather` y ejecutar `/newbot`.
2. Elegir nombre y username terminado en `bot`.
3. Guardar el token entregado como `TELEGRAM_BOT_TOKEN`.
4. Abrir una conversación con el bot y enviar `/start`.

Para obtener `TELEGRAM_CHAT_ID`, con el token configurado abrir:

`https://api.telegram.org/bot<TOKEN>/getUpdates`

En la respuesta JSON buscar `message.chat.id`. Para un grupo, agregar primero el bot al grupo y enviar un mensaje; el ID normalmente es negativo.

## Railway

1. Crear un proyecto nuevo desde este repositorio.
2. Railway detectará `requirements.txt` y el `Procfile`.
3. En Variables configurar `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` y opcionalmente `CHECK_INTERVAL_SECONDS=300`.
4. Para SQLite persistente, agregar un Volume montado en `/app/data` (o el directorio de trabajo que muestre Railway). Sin volumen, la base puede perderse al redeploy/restart.
5. Desplegar y revisar los logs del servicio worker.

Logs esperados:

```text
Worker iniciado; pool=58oQChx...; intervalo=300s
Métricas válidas: APR=44.80% estado=NORMAL volumen24h=USD 12.50 M TVL=USD 14.00 M rotación=0.89x estado_volumen=VOLUME_NORMAL
```

Un fallo se verá como `Raydium API falló (1/3): ...`; al tercer fallo aparece `Alerta API_DEGRADED enviada`. Nunca se usa otra fuente ni se calcula el APR localmente.

## Verificación de la fuente

La forma soportada por Raydium es `/pools/info/ids`; la documentación oficial describe `data` como una lista de pools, y los tipos oficiales del SDK V2 describen `day.apr` como el APR del período diario.
