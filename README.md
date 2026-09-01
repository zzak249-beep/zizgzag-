# Wavelet MRA Haar 5m — Bot BingX + Telegram

Bot que recibe alertas del script Pine `wavelet_mra_haar_5m.pine` vía webhook
de TradingView, y según configuración:

- **Modo manual** (`AUTO_TRADE=false`, por defecto): solo reenvía la señal a
  Telegram con precio, SL y TP para que operes tú.
- **Modo automático** (`AUTO_TRADE=true`): además ejecuta la orden en BingX
  (perpetual swap, modo hedge) con sizing por riesgo, circuit breaker y
  reconciliación de posiciones al arrancar.

Ver `RESEARCH.md` para el análisis matemático de la estrategia: qué es
realmente (y qué no) el filtro "wavelet", y bajo qué condiciones tiene
alguna ventaja real.

## Estructura

```
wavelet_bot/
├── main.py              # webhook receiver (Flask) + lógica de entrada/salida
├── bingx_client.py       # cliente REST BingX swap v2 (HMAC)
├── telegram_notifier.py  # envío de mensajes a Telegram
├── state_manager.py      # persistencia JSON + reconciliación + circuit breaker
├── config.py             # lee todo de variables de entorno
├── pinescript/
│   └── wavelet_mra_haar_5m.pine   # estrategia Pine v6 (TradingView)
├── requirements.txt
├── Procfile
├── railway.json
├── .env.example
└── RESEARCH.md
```

## 1. Subir a GitHub

```bash
cd wavelet_bot
git init
git add .
git commit -m "Wavelet MRA Haar 5m bot"
git branch -M main
git remote add origin git@github.com:<tu-usuario>/wavelet-mra-bot.git
git push -u origin main
```

`.env` y `state.json` están en `.gitignore` — nunca subas tus claves.

## 2. Desplegar en Railway

1. Railway → New Project → Deploy from GitHub repo → selecciona el repo.
2. Railway detecta `Procfile`/`railway.json` automáticamente (Nixpacks + Python).
3. En **Variables**, copia todo lo de `.env.example` y rellena:
   - `BINGX_API_KEY` / `BINGX_API_SECRET` (API key de BingX con permisos de
     **futuros/trading**, sin permiso de retiro).
   - `TELEGRAM_BOT_TOKEN` (de @BotFather) y `TELEGRAM_CHAT_ID` (de @userinfobot
     o tu chat con el bot).
   - `WEBHOOK_SECRET`: genera uno con `openssl rand -hex 24`.
   - Deja `AUTO_TRADE=false` la primera semana. Solo señales, cero riesgo.
4. Deploy. Railway te da una URL tipo `https://tu-app.up.railway.app`.
5. Prueba: `curl https://tu-app.up.railway.app/` debe devolver `{"status":"ok",...}`.

## 3. Configurar la alerta en TradingView

1. Abre el gráfico con `wavelet_mra_haar_5m.pine` cargado como **estrategia**,
   timeframe 5m, en el símbolo que quieras (necesitas plan de TradingView
   con alertas por webhook — mínimo el plan Essential).
2. Crea una alerta (icono del reloj) → Condición: **"Any alert() function call"**
   sobre el script (esto captura las líneas `alert(json_..., ...)` del script,
   que ya llevan symbol/side/sl/tp incluidos).
3. En **Webhook URL**: `https://tu-app.up.railway.app/webhook/<WEBHOOK_SECRET>`
4. Mensaje: déjalo vacío o `{{strategy.order.alert_message}}` — el script ya
   construye el JSON completo internamente.
5. Guarda. Repite para cada símbolo/timeframe que quieras vigilar.

## 4. Verificar en modo manual antes de arriesgar dinero

Con `AUTO_TRADE=false`, cada señal solo llega a Telegram. Corre así **al
menos 1-2 semanas** y compara las señales contra lo que habría pasado
(fíjate en el `sl`/`tp` que manda cada alerta). Esto es más importante que
cualquier backtest — el backtest de Pine no captura slippage real en 5m ni
el comportamiento exacto del matching engine de BingX.

## 5. Pasar a real

Cuando confíes en las señales:
1. Cambia `AUTO_TRADE=true` en Railway (redeploy automático).
2. Empieza con `RISK_PCT_PER_TRADE` bajo (1% o menos) y `LEVERAGE` moderado.
3. Vigila el circuit breaker: se activa solo tras `MAX_CONSECUTIVE_LOSSES`
   pérdidas seguidas o `MAX_DAILY_DRAWDOWN_PCT`% de drawdown diario, y te
   avisa por Telegram. Para reactivarlo manualmente:
   ```bash
   curl -X POST https://tu-app.up.railway.app/reset-breaker/<WEBHOOK_SECRET>
   ```

## Notas de arquitectura (para que encaje con el resto de tu flota)

- **Un solo worker** (`--workers 1` en Procfile/railway.json): el estado se
  guarda en un JSON local, no hay lock distribuido. Si necesitas escalar,
  cambia `STATE_FILE` a una base de datos (Redis/Postgres) primero.
- **Firma HMAC**: se construye el query string ordenado UNA vez y se usa
  igual para firmar y transmitir — evita el bug de mismatch orden-firma/
  orden-transmisión que ya diste con `renewed-love`/`joyful-art`/`bot22`.
- **Reconciliación al arrancar**: `state_manager.reconcile()` compara
  posiciones locales vs. `/openApi/swap/v2/user/positions` y prioriza siempre
  al exchange como fuente de verdad.
- **positionSide**: el bot asume cuenta en modo **hedge** (LONG/SHORT
  simultáneos posibles). Si tu cuenta BingX está en modo one-way, cambia
  `positionSide` a `"BOTH"` en `bingx_client.place_market_order`.
- Los endpoints de BingX (`stopLoss`/`takeProfit` embebidos en la orden) están
  confirmados por la documentación pública y por ccxt, pero **verifica en modo
  demo (`BINGX_DEMO=true` + endpoint VST) antes de tocar dinero real**, porque
  BingX cambia parámetros de su API sin previo aviso frecuentemente.
