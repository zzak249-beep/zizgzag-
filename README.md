# Crowding bot v2 — solo señales (más rápido y preciso)

Bot de señales del posicionamiento amontonado en perpetuos de BingX.

**NO OPERA. NO PIDE CLAVES DE API.** Solo endpoints públicos, así que no
puede tocar la cuenta ni por error.

## Mejoras v2 (velocidad + precisión)

| Antes | Ahora |
|-------|-------|
| ~9,4 min por ciclo (300 símbolos) | ~2-3 min típico |
| 3 llamadas REST por símbolo | 1 llamada global de premiumIndex + paralelo OI/klines |
| `requests` sueltos | `requests.Session` (connection pooling) |
| Secuencial + PACING 0.15 s | `ThreadPoolExecutor` (MAX_WORKERS=20 por defecto) |
| klines limit=200 | klines limit=120 (suficiente) |
| SCAN_SEC=300 | SCAN_SEC=120 (ajustable) |

Rate limit oficial de BingX (market data públicos): **500 requests / 10 s por IP**.
Con 20 workers te mantienes cómodamente por debajo.

## Qué hace

Detecta apalancamiento amontonado (basis extremo + open interest subiendo
+ precio en un extremo) y espera la primera vela EN CONTRA de la multitud.
Cada señal abre una operación **virtual** con stop y objetivo, la sigue
hasta el desenlace y anota el resultado en R con el coste descontado.

El informe diario dice la muestra acumulada **y qué se puede concluir con
ella**:

| ventaja real | operaciones necesarias |
|---|---|
| 0.50 R/op | 31 |
| 0.30 R/op | 87 |
| 0.20 R/op | 196 |
| 0.10 R/op | 784 |

## Despliegue en Railway

1. Proyecto nuevo desde este repo.
2. **Monta un Volume en `/data`.** Sin él, cada redespliegue borra la
   historia acumulada y el bot vuelve a calentar desde cero.
3. Variables de entorno (ver abajo).

## Calentamiento

BingX no sirve histórico de open interest, así que el bot acumula el suyo.
Dirá `calentando (X/30h, N/200)` y no emitirá nada hasta cumplir **las dos
condiciones**: 30 horas de historia Y 200 muestras.

Con la cadencia más rápida de v2 el calentamiento real es más corto en
tiempo de reloj (más muestras por hora).

## Los parámetros van en HORAS, no en muestras

`OI_LOOK_H`, `HIST_HORAS` y `MIN_HORAS` están en horas y el bot hace la
conversión con su cadencia real.

`MIN_MUESTRAS` sigue siendo una cuenta: hacen falta las dos cosas.

## Variables

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

### Nuevas / cambiadas en v2

- `MAX_WORKERS` (default 20): paralelismo de descarga OI+klines.
- `KLINES_LIMIT` (default 120): velas pedidas (antes fijo 200).
- `SCAN_SEC` default bajado a 120.
- `PACING` default 0 (ya no hace falta el sleep extra).

## Telegram

`TG_SIGNALS` y `TG_CLOSES` vienen **apagados**. Por defecto llega
**un mensaje al día**: el informe.

Todo queda igualmente en el CSV.

## Sobre las claves de BingX

**No las pongas.** Todo lo que este bot necesita es público.

## Régimen (confirm.py)

El módulo `confirm.py` calcula el ratio de varianzas robusto y etiqueta el
símbolo como tendencial, reversivo o indeterminado. **Aquí solo se apunta,
nunca decide.**

Motivo: el crowding opera CONTRA la multitud (reversión). El veto de
`confirm.py` está pensado para ruptura.

## Salida

- `/data/crowding_ops.csv` — una fila por operación virtual cerrada
- `/data/crowding_state.json` — historia de basis y OI + virtuales abiertas
- Telegram — informe diario (+ señales/cierres si los activas)

## Archivos

```
crowding_bot.py   # bot principal (v2 optimizado)
confirm.py        # filtro de régimen (solo registro)
requirements.txt  # requests
Procfile          # worker: python crowding_bot.py
README.md
.gitignore
```
