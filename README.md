# Raydium APR Watcher

Worker Python mínimo para Railway que consulta exclusivamente la API oficial V3 de Raydium:

`GET https://api-v3.raydium.io/pools/info/ids?ids=58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2`

El parser valida `success`, verifica el ID exacto y lee el APR 24h desde `data[0].day.apr` (la respuesta efectiva del SDK/API V3). Si el esquema no coincide, registra una muestra acotada de la respuesta y no estima ningún valor.

## Archivos

- `main.py`: worker, cliente Raydium, cliente Telegram, reglas y SQLite.
- `requirements.txt`: única dependencia, `requests`.
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
python main.py
```

Para una prueba rápida se puede usar `CHECK_INTERVAL_SECONDS=30`. El worker ejecuta una lectura inmediatamente al iniciar y después espera el intervalo.

La base se puede inspeccionar con:

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/raydium_monitor.db'); print(*c.execute('select timestamp_utc, apr, state, error from readings order by id desc limit 10'), sep='\n')"
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
APR 24h válido: 44.80%; estado=NORMAL
```

Un fallo se verá como `Raydium API falló (1/3): ...`; al tercer fallo aparece `Alerta API_DEGRADED enviada`. Nunca se usa otra fuente ni se calcula el APR localmente.

## Verificación de la fuente

La forma soportada por Raydium es `/pools/info/ids`; la documentación oficial describe `data` como una lista de pools, y los tipos oficiales del SDK V2 describen `day.apr` como el APR del período diario.
