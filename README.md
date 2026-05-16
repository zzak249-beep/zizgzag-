# 🎯 Sniper Bot V49 — Híbrido [Markov + Kotegawa]

Bot algorítmico de trading para **Binance Futures (USDT-M)** con notificaciones en **Telegram**, desplegable en **Railway** en minutos.

---

## 🧠 Ventaja competitiva

| Capa | Tecnología | Ventaja |
|---|---|---|
| **Motor Markov** | Matriz de transición 3×3 sliding | Probabilidad estadística de continuación de estado |
| **ADX Adaptativo** | Régimen detection V50.1 | Parámetros dinámicos según volatilidad |
| **Filtros institucionales** | VWAP + RVOL + POC | Detecta flujo de dinero inteligente |
| **Kotegawa Dip** | MA25 + RSI + BB | Confluencia de reversión a la media |
| **STC Oscillator** | Schaff Trend Cycle | Evita entradas en sobrecompra/sobreventa extrema |
| **Kelly Fraccional** | 1/4 Kelly criterion | Sizing matemáticamente óptimo |
| **Triple Barrera** | TP + SL + Tiempo | Exit multi-dimensión estilo ML |

La señal requiere **≥55 puntos** de un scoring compuesto de 100. Dos capas independientes deben converger para activar una entrada.

---

## 🗂 Estructura del proyecto

```
sniper-bot/
├── main.py                  # Bucle principal async
├── config.py                # Variables de entorno
├── requirements.txt
├── Procfile                 # Railway worker
├── railway.json
├── .env.example
└── bot/
    ├── indicators.py        # EMA, ATR, ADX, VWAP, RVOL, POC, STC, RSI, BB, Pivots
    ├── markov.py            # Motor Markov con ventana deslizante
    ├── strategy.py          # Fusión Sniper V49 + Kotegawa
    ├── risk_manager.py      # Kelly + Triple Barrera + DD guard
    ├── binance_client.py    # Binance Futures async (TP/SL automáticos)
    ├── telegram_notifier.py # Mensajes ricos en Telegram
    └── utils.py             # Logging con colores
```

---

## ⚙️ Configuración paso a paso

### 1. Clonar el repositorio
```bash
git clone https://github.com/TU_USUARIO/sniper-bot.git
cd sniper-bot
```

### 2. Credenciales Binance Futures
1. Ve a [Binance → Gestión de API](https://www.binance.com/es/my/settings/api-management)
2. Crea una nueva API Key con permisos:
   - ✅ **Leer**
   - ✅ **Trading de futuros**
   - ❌ **NO** habilites retiradas
3. Restringe la IP a la IP de tu servidor Railway (opcional pero recomendado)

### 3. Bot de Telegram
```bash
# 1. Habla con @BotFather en Telegram → /newbot → guarda el TOKEN
# 2. Habla con @userinfobot → guarda tu CHAT_ID
# O usa: https://api.telegram.org/bot<TOKEN>/getUpdates
```

### 4. Variables de entorno (local)
```bash
cp .env.example .env
# Edita .env con tus credenciales reales
```

---

## 🚀 Deploy en Railway

### Opción A — GitHub (recomendado)
1. Sube el proyecto a GitHub
2. En [railway.app](https://railway.app) → **New Project → Deploy from GitHub**
3. Selecciona el repositorio
4. En **Variables** agrega todas las del `.env.example`
5. Railway detecta el `Procfile` y despliega automáticamente

### Opción B — Railway CLI
```bash
npm install -g @railway/cli
railway login
railway init
railway up
railway variables set BINANCE_API_KEY=... TELEGRAM_TOKEN=... # etc.
```

### Variables obligatorias en Railway
```
BINANCE_API_KEY
BINANCE_SECRET_KEY
TELEGRAM_TOKEN
TELEGRAM_CHAT_ID
```

---

## 🧪 Paper Trading primero (OBLIGATORIO)

```env
TESTNET=true
```

Binance Futures Testnet: https://testnet.binancefuture.com

**Valida durante al menos 2 semanas antes de usar dinero real.**

---

## 📊 Mensajes Telegram

| Evento | Contenido |
|---|---|
| 🚀 Arranque | Config, modo, pares, leverage |
| 📈📉 Entrada | Precio, qty, TP, SL, régimen, ADX, probs Markov, RVOL, STC, VWAP, POC, RSI, score total, razones |
| ✅❌ Salida | Motivo (TP/SL/Tiempo), PnL USDT, PnL %, balance |
| 💓 Heartbeat | Cada hora: balance, PnL diario, DD, estado por par |
| ⚠️ Error | Stack trace truncado |

---

## ⚠️ Avisos de riesgo

> **El trading con apalancamiento puede resultar en pérdida total del capital.**
> Este software se proporciona sin garantías. Úsalo bajo tu propia responsabilidad.
> Empieza siempre con capital que puedas permitirte perder.

**Ajustes conservadores recomendados para empezar:**
```env
LEVERAGE=3
RISK_PER_TRADE=1.0
MAX_DAILY_LOSS_PCT=2.0
ATR_MULT_TP=2.5
ATR_MULT_SL=1.0
```

---

## 🔧 Ajuste de parámetros

| Parámetro | Valor base | Mercado alcista | Mercado lateral |
|---|---|---|---|
| `PROB_THRESHOLD` | 40% | 35% | 50% |
| `RVOL_MIN` | 1.5x | 1.3x | 2.0x |
| `ATR_MULT_TP` | 2.0 | 2.5 | 1.5 |
| `ADX_TREND` | 25 | 20 | 30 |

---

## 📋 Logs

Los logs se guardan en `logs/bot.log` con rotación automática.
En Railway puedes verlos en tiempo real desde el panel → **Deployments → View Logs**.
