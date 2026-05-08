# 🎯 Sniper Bot V26.1 — Institutional Apex

Bot de trading automático: TradingView → Railway → BingX Futures + Telegram

---

## ⚠️ ADVERTENCIA IMPORTANTE

Estás operando con **apalancamiento real (10x)**. Con $8 de capital tienes $80 de exposición.
Un movimiento en contra del 10% liquida toda la posición. Opera con responsabilidad.

---

## 🏗️ Arquitectura

```
TradingView (señal)
    │  Webhook JSON
    ▼
Railway (FastAPI server)
    │  Calcula SL/TP/size con ATR
    ├──▶ BingX Futures API (ejecuta orden)
    └──▶ Telegram (notifica detalles)
```

---

## 🚀 Instalación paso a paso

### PASO 1 — Fork y clona este repositorio

```bash
git clone https://github.com/TU_USUARIO/sniper-bot.git
cd sniper-bot
```

### PASO 2 — Configura BingX API

1. Entra a [BingX](https://bingx.com) → perfil → API Management
2. Crea nueva API Key con permisos: **Futures Trading** (NO retiros)
3. Añade la IP de Railway a la whitelist (o deja vacío para todas)
4. Copia `API Key` y `Secret Key`

### PASO 3 — Configura Telegram Bot

1. Abre Telegram → busca `@BotFather`
2. Envía `/newbot` y sigue las instrucciones
3. Copia el **token** (formato: `123456:ABCdef...`)
4. Para obtener tu `CHAT_ID`:
   - Envía cualquier mensaje a tu bot
   - Visita: `https://api.telegram.org/botTU_TOKEN/getUpdates`
   - Busca `"chat":{"id":XXXXXXXXX}` → ese es tu chat_id

### PASO 4 — Despliega en Railway

1. Ve a [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Conecta este repositorio
3. En **Variables** (Settings → Variables), añade:

```
BINGX_API_KEY        = tu_api_key
BINGX_API_SECRET     = tu_api_secret
TELEGRAM_BOT_TOKEN   = 123456:ABCdef...
TELEGRAM_CHAT_ID     = -100XXXXXXXXX
WEBHOOK_SECRET       = una_clave_secreta_larga_y_aleatoria
CAPITAL_USDT         = 8
APALANCAMIENTO       = 10
RIESGO_PCT           = 1.0
RATIO_RR             = 3.0
```

4. Railway detecta el `Procfile` y despliega automáticamente
5. Copia la URL pública: `https://TU-APP.up.railway.app`

### PASO 5 — Verifica que funciona

```bash
curl https://TU-APP.up.railway.app/health
# Respuesta: {"status":"ok","posicion_abierta":false}
```

### PASO 6 — Configura TradingView

1. Carga el script `tradingview_script.pine` como indicador
2. Crea alerta para LONG:
   - Condition: `LONG APEX`
   - Webhook URL: `https://TU-APP.up.railway.app/webhook`
   - Message:
   ```json
   {
     "secret": "tu_webhook_secret",
     "action": "BUY",
     "symbol": "BTC-USDT",
     "atr": "{{plot_1}}"
   }
   ```
3. Crea alerta para SHORT (igual pero `"action": "SELL"`)
4. Repite para cada par que quieras monitorear

---

## 📱 Mensajes Telegram

**Al entrar:**
```
🟢 NUEVA OPERACIÓN — BTC-USDT
━━━━━━━━━━━━━━━━━━━━
📌 Dirección: LONG 🚀
💰 Entrada:   $67,234.5000
🛑 Stop Loss: $66,890.2000  (0.512%)
🎯 Take Profit: $68,267.4000  (1.536%)
━━━━━━━━━━━━━━━━━━━━
💼 Tamaño posición: $80.00
⚡ Apalancamiento: 10x
🎲 Riesgo máximo: $0.8000
📊 R:R objetivo: 1:3.0
📈 ATR usado: 344.210000
━━━━━━━━━━━━━━━━━━━━
🤖 Sniper Bot V26.1 | BingX Futures
```

**Al cerrar:**
```
✅ OPERACIÓN CERRADA — BTC-USDT
━━━━━━━━━━━━━━━━━━━━
📌 Fue: LONG
💵 PnL realizado: GANANCIA: +$2.4000 USDT
━━━━━━━━━━━━━━━━━━━━
🤖 Sniper Bot V26.1 | BingX Futures
```

---

## 🔗 Endpoints del servidor

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/health` | Estado del bot |
| POST | `/webhook` | Recibe señales de TradingView |

---

## 💡 Pares soportados

El bot soporta **todos los perpetuos USDT de BingX**. Ejemplos:
`BTC-USDT`, `ETH-USDT`, `SOL-USDT`, `DOGE-USDT`, `XRP-USDT`, `BNB-USDT`, etc.

El símbolo se envía dinámicamente desde TradingView con `{{ticker}}-USDT`

---

## ⚙️ Parámetros configurables

| Variable | Default | Descripción |
|----------|---------|-------------|
| `CAPITAL_USDT` | 8 | Capital base en USDT |
| `APALANCAMIENTO` | 10 | Multiplicador (máx recomendado: 10x) |
| `RIESGO_PCT` | 1.0 | % del capital apalancado por trade |
| `RATIO_RR` | 3.0 | Ratio riesgo:recompensa (TP = SL × 3) |

---

## 🛡️ Protecciones integradas

- ✅ Solo 1 posición abierta a la vez
- ✅ SL basado en ATR (no en pivotes variables)
- ✅ Validación HMAC del webhook secret
- ✅ Timeout en todas las llamadas API (10s)
- ✅ Logging completo de todas las operaciones
- ✅ Notificación Telegram en errores críticos
- ✅ `barstate.isconfirmed` para evitar señales prematuras

---

## 📁 Estructura del proyecto

```
sniper-bot/
├── app/
│   ├── __init__.py
│   ├── main.py        # Servidor FastAPI + webhook
│   ├── bingx.py       # Cliente BingX Futures API
│   ├── risk.py        # Motor de gestión de riesgo
│   ├── telegram.py    # Notificaciones
│   └── state.py       # Estado en memoria
├── tradingview_script.pine  # Script para TradingView
├── requirements.txt
├── Procfile           # Comando de inicio Railway
├── .env.example       # Variables de entorno (plantilla)
└── .gitignore
```

---

## ⚠️ Gestión de riesgo con $8 capital

Con $8 × 10x = $80 de exposición:

- Riesgo por trade: 1% de $80 = **$0.80**
- Para llegar a TP (1:3): ganancia de **$2.40**
- Máx drawdown tolerable: ~6-8 trades perdidos consecutivos

**Recomendación:** Empieza en modo paper trading (cuenta demo de BingX) antes de operar real.
