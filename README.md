# Sniper Bot V50 Ultimate

Bot algorítmico para **BingX Perpetual Futures** con:
- Motor Markov 200 velas + ADX Slope Adaptativo + STC + POC (lógica V49)
- Filtro de **Funding Rate** — evita operar contra el funding extremo
- **Liquidation Map** — detecta zonas de stops acumulados y usa esas zonas como boost de señal
- Señales completas en **Telegram** con score de confianza 0–100
- Despliegue en **Railway** con reinicio automático

---

## Estructura

```
sniper_v50/
├── main.py           # Loop principal
├── indicators.py     # Motor V50: Markov + Funding + Liq Map
├── exchange.py       # Cliente BingX Perpetual (CCXT)
├── telegram_bot.py   # Mensajes Telegram enriquecidos
├── config.py         # Configuración central
├── requirements.txt
├── Procfile          # Railway worker
├── railway.toml      # Config Railway
└── .env.example      # Variables de entorno
```

---

## Setup local (prueba en paper primero)

```bash
git clone https://github.com/TU_USUARIO/sniper-bot-v50
cd sniper-bot-v50
pip install -r requirements.txt
cp .env.example .env
# Edita .env con tus claves (MODE=paper para empezar)
python main.py
```

---

## Variables de entorno

| Variable | Descripción | Ejemplo |
|---|---|---|
| `BINGX_API_KEY` | API Key de BingX | `abc123...` |
| `BINGX_API_SECRET` | API Secret de BingX | `xyz789...` |
| `TELEGRAM_BOT_TOKEN` | Token del bot Telegram | `123456:ABC...` |
| `TELEGRAM_CHAT_ID` | Tu chat ID | `-100123456` |
| `SYMBOL` | Par a operar | `BTC/USDT` |
| `TIMEFRAME` | Temporalidad | `5m` |
| `RISK_PCT` | % balance por trade | `2.0` |
| `LEVERAGE` | Apalancamiento | `3` |
| `MAX_TRADES_DAY` | Máx operaciones/día | `6` |
| `MODE` | `paper` o `live` | `paper` |
| `FUNDING_THRESHOLD` | % funding extremo | `0.01` |
| `FUNDING_AVOID` | Evitar funding adverso | `true` |
| `LIQ_LOOKBACK` | Velas para liq map | `100` |
| `LIQ_MULTIPLIER` | ATR mult zona liq | `1.5` |

---

## Despliegue en Railway

1. Sube el proyecto a un repo **privado** en GitHub
2. Entra en [railway.app](https://railway.app) → **New Project → Deploy from GitHub**
3. Selecciona el repositorio
4. Ve a **Variables** → añade todas las variables del `.env`
5. Railway detecta `Procfile` → ejecuta `python main.py`
6. Si el proceso cae, se reinicia automático (`railway.toml`)

---

## Cómo obtener las claves

### BingX API
1. [bingx.com](https://bingx.com) → Cuenta → API Management
2. Crear clave: permisos **Read + Trade** (nunca "Retiradas")
3. Whitelistea la IP de Railway (o deja abierta solo para trade)

### Telegram Bot Token
1. Habla con [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copia el token

### Telegram Chat ID
1. Habla con [@userinfobot](https://t.me/userinfobot)
2. Copia tu `id`

---

## Lógica de señales

### LONG entra cuando (todos deben cumplirse):
- `low` rompe el último valle (sweep de liquidez)
- `close < VWAP`
- Magic Slope > Slope Adaptativo (ADX)
- RVOL > 1.5x
- Prob Markov Bull > umbral
- STC < 75 (no sobrecomprado)
- Funding rate no adverso
- No en zona de liquidación SHORT

### SHORT entra cuando (todos deben cumplirse):
- `high` rompe el último pico
- `close > VWAP`
- Magic Slope < -Slope Adaptativo
- RVOL > 1.5x
- Prob Markov Bear > umbral
- STC > 25
- Funding rate no adverso
- No en zona de liquidación LONG

### Score de confianza (0–100):
- Base: probabilidad Markov (0–80)
- +10 si hay boost de liquidation map
- +10 si funding rate es favorable

### Triple barrera de salida:
- **TP**: entrada ± ATR14 × 2.0
- **SL**: entrada ∓ ATR14 × 1.2
- **Tiempo**: cierre forzado a las 20 velas

---

## Cómo funciona el Funding Rate Filter

Los perpetual futures tienen un mecanismo de funding:
- **Funding positivo** → longs pagan shorts → mercado sobrecargado de longs → sesgo bajista
- **Funding negativo** → shorts pagan longs → mercado sobrecargado de shorts → sesgo alcista

El bot evita abrir posiciones contra el funding extremo (configurable con `FUNDING_THRESHOLD`).
También envía una alerta Telegram cuando el funding supera el umbral.

## Cómo funciona el Liquidation Map

Calcula zonas donde hay alta probabilidad de stops acumulados:
- Usa pivots históricos ponderados por volumen
- Cuando el precio entra en una zona de liquidación LONG (bajo), los stops se activan y pueden generar un movimiento alcista → boost para señales LONG
- Cuando entra en zona SHORT (alto) → boost para señales SHORT

---

## Advertencia

Operar con futuros apalancados conlleva riesgo de pérdida del capital.
Empieza siempre con `MODE=paper` varios días antes de pasar a `live`.
El bot no garantiza resultados. Úsalo bajo tu propia responsabilidad.
