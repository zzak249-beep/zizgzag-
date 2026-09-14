# Crowding bot v2.2 — solo señales

Bot de señales del posicionamiento amontonado en perpetuos de BingX.

**NO OPERA. NO PIDE CLAVES DE API.** Solo endpoints públicos.

## Novedades v2.2
- **Progreso de calentamiento** en cada ciclo:
  `calentando 287/300 (media 12.4 h, 84 muestras)`
- **Heartbeat cada hora** por Telegram + log (listos, media de horas, virtuales abiertas)
- Pool de conexiones ampliado (v2.1)
- premiumIndex global + ThreadPoolExecutor (v2)

## Despliegue en Railway

### Start Command
```
python crowding_bot.py
```
(NO pongas `worker: ...`)

### Volume
Monta un Volume en `/data`.

### Variables recomendadas
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

Servicio tipo **Worker**.

## Calentamiento
Hasta 30 h + 200 muestras por símbolo → 0 señales (normal).
Con cadencia ~2 min son aproximadamente **30-35 horas de reloj**.

El heartbeat te dirá el progreso cada hora.

## Archivos
```
crowding_bot.py
confirm.py
requirements.txt
Procfile
railway.toml
README.md
.gitignore
```
