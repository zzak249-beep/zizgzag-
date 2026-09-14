# Crowding bot v2.1 — solo señales (pool arreglado)

Bot de señales del posicionamiento amontonado en perpetuos de BingX.

**NO OPERA. NO PIDE CLAVES DE API.** Solo endpoints públicos.

## Cambios v2.1
- Connection pool ampliado (40) → desaparecen los warnings `Connection pool is full`
- Retry automático en 429/5xx
- Resto de mejoras v2 intactas (premiumIndex global + ThreadPoolExecutor)

## Despliegue en Railway (importante)

### 1. Start Command
En **Settings → Deploy → Start Command** pon **exactamente**:

```
python crowding_bot.py
```

**NO** pongas `worker: python crowding_bot.py` (provoca `worker:: command not found`).

El archivo `railway.toml` ya fuerza este comando.

### 2. Volume
Monta un **Volume** en `/data`.

### 3. Variables recomendadas

```
TIMEFRAME=15m
SCAN_SEC=120
MIN_VOL_24H=2000000
MAX_SYMBOLS=300
MAX_WORKERS=20
KLINES_LIMIT=120
HIST_HORAS=168
MIN_HORAS=30
MIN_MUESTRAS=200
OI_LOOK_H=6
Z_BASIS=2.0
Z_OI=1.0
EXT_PCT=80
ATR_LEN=14
SL_ATR=1.5
TP_R=2.0
MAX_BARS=16
MIN_ATR_PCT=1.0
COST_PCT=0.25
MAX_COST_R=0.20
STATE=/data/crowding_state.json
CSV=/data/crowding_ops.csv
TG_TOKEN=
TG_CHAT=
TG_SIGNALS=false
TG_CLOSES=false
REPORT_HOUR=7
PACING=0
```

### 4. Tipo de servicio
Configura el servicio como **Worker**.

## Calentamiento
Hasta cumplir 30 h + 200 muestras por símbolo el bot no emite señales
(aparecerá “calentando”). Con el ciclo actual son ~30-35 horas de reloj.

## Telegram
Por defecto solo el informe diario. Activa con:
```
TG_SIGNALS=true
TG_CLOSES=true
```

## Archivos
```
crowding_bot.py   # v2.1 (pool arreglado)
confirm.py
requirements.txt
Procfile
railway.toml
README.md
.gitignore
```
