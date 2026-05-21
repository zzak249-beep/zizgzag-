# QF×JP Crypto Bot

Bot de trading automático para BingX con señales desde TradingView y notificaciones en Telegram.

## Arquitectura

```
TradingView (Pine Script)
    │ Alerta webhook JSON
    ▼
Railway (FastAPI bot)
    ├── BingX API → ejecuta órdenes reales
    └── Telegram → notificaciones en tiempo real
```

---

## Paso 1 — Clonar y configurar

```bash
git clone https://github.com/TU_USUARIO/qfjp-bot.git
cd qfjp-bot
cp .env.example .env
# Edita .env con tus claves reales
```

---

## Paso 2 — Obtener claves BingX

1. Ve a [BingX → API Management](https://bingx.com/en-us/account/api/)
2. Crea una API Key con permisos: **Read + Trade** (NO withdraw)
3. Añade la IP de Railway en la whitelist (o deja vacío para cualquier IP)
4. Copia `API Key` y `Secret Key` en el `.env`

---

## Paso 3 — Crear bot de Telegram

1. Habla con [@BotFather](https://t.me/BotFather) en Telegram
2. Escribe `/newbot` y sigue las instrucciones
3. Copia el token en `TELEGRAM_TOKEN`
4. Envía un mensaje a tu bot, luego visita:
   ```
   https://api.telegram.org/bot<TU_TOKEN>/getUpdates
   ```
5. Copia el `chat.id` en `TELEGRAM_CHAT_ID`

---

## Paso 4 — Deploy en Railway

1. Ve a [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Selecciona tu repo
3. En **Variables**, añade todas las del `.env.example` con sus valores reales:
   - `BINGX_API_KEY`
   - `BINGX_API_SECRET`
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `WEBHOOK_SECRET`
   - `TRADE_SIZE_USDT`
   - `MAX_OPEN_TRADES`
   - `SL_PCT`
   - `TP_PCT`
   - `MIN_SIGNAL_LEVEL`
4. Railway desplegará automáticamente y te dará una URL tipo:
   ```
   https://qfjp-bot-production.up.railway.app
   ```
5. Verifica que funciona:
   ```
   https://TU_URL/health
   ```

---

## Paso 5 — Configurar alertas en TradingView

1. Abre el gráfico con el script **QF×JP Crypto V3** cargado
2. Crea una alerta para cada señal que quieras operar
3. En **Webhook URL** pon:
   ```
   https://TU_URL_RAILWAY/webhook
   ```
4. En **Message** pon exactamente el JSON de la alerta. El script ya lo genera automáticamente en el `alertcondition`. Ejemplo para LONG_SUP_V3:
   ```json
   {"signal":"LONG_SUP_V3","symbol":"{{ticker}}","price":"{{close}}","tf":"3"}
   ```
5. En el header de la alerta añade:
   - Header: `X-Webhook-Secret` = el valor de tu `WEBHOOK_SECRET`

   > En TradingView Pro/Pro+ puedes añadir headers personalizados en las alertas webhook.

---

## Paso 6 — Probar en paper trading primero

Antes de operar con dinero real:
1. Pon `TRADE_SIZE_USDT=1` en Railway
2. Usa la cuenta demo de BingX (cambia el endpoint en `bot.py` a `open-api.bingx.com/demo`)
3. Observa durante al menos 1 semana

---

## Niveles de señal y riesgo

| Señal | Nivel | Descripción | Operar? |
|---|---|---|---|
| HUNT_LONG/SHORT | ⚡ 1 | Caza de stops detectada | Solo con experiencia |
| LONG/SHORT_FUEL | ▲▼ 2 | Ruptura TL + agotamiento | Mínimo recomendado |
| LONG/SHORT_SUP | ★ 3 | + Dark Pool confirmado | Buena convicción |
| LONG/SHORT_SUP_V3 | ★★ 4 | + CVD + Zona liq. segura | Máxima convicción |

Configura `MIN_SIGNAL_LEVEL=LONG_FUEL` para operar solo niveles 2+.

---

## Endpoints del bot

| Endpoint | Método | Descripción |
|---|---|---|
| `/webhook` | POST | Recibe señales de TradingView |
| `/health` | GET | Estado del bot y trades abiertos |
| `/trades` | GET | Detalle de posiciones abiertas |

---

## Gestión de riesgo incorporada

- **SL automático** en cada orden (BingX lo gestiona en servidor)
- **TP automático** en cada orden
- **Límite de posiciones simultáneas** (`MAX_OPEN_TRADES`)
- **Límite de capital por trade** (`TRADE_SIZE_USDT` o 20% del balance disponible, el menor)
- **Timeout de 3 horas**: cierra automáticamente si la posición lleva demasiado tiempo abierta
- **Filtro de señales por nivel mínimo** (`MIN_SIGNAL_LEVEL`)
- **Sin duplicados**: no abre dos posiciones en el mismo símbolo en la misma dirección

---

## Advertencia

Este bot opera con dinero real. El trading de criptomonedas conlleva riesgo de pérdida total del capital. Úsalo bajo tu propia responsabilidad, siempre con capital que puedas permitirte perder, y después de testear en demo.
