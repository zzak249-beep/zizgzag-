# FED + WER v7 — Señales BingX → Telegram

Escáner de los perpetuos USDT de BingX (top por volumen) que detecta **extensiones fallidas en días extremos** (fade de techo/suelo) y manda la señal a Telegram, con seguimiento automático de TP/SL/trailing y resumen diario. Solo señales: no usa claves de BingX ni opera.

## Archivos
| Archivo | Qué hace |
|---|---|
| `main.py` | Bucle: escanea al cierre de cada vela, envía señales y actualizaciones |
| `strategy.py` | Motor FED+WER v7 portado 1:1 de Pine + mejoras |
| `tracker.py` | Seguimiento de la señal vela a vela (mismo código en vivo y backtest) |
| `backtest.py` | Backtest con datos reales de BingX; `--variants` compara mejoras |
| `config.py` | Todas las variables de entorno |
| `test_engine.py` | Test sintético (`python test_engine.py`) |

## Despliegue Railway
1. Sube el repo a GitHub → Railway → New Project → Deploy from GitHub.
2. Variables: pega `.env.example` en el Raw Editor y rellena `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
3. Añade un **Volume** al servicio montado en `/data` (estado de señales abiertas; sin volumen se pierde en cada redeploy).
4. Al arrancar llega a Telegram “online” con `CODE_VERSION` y la config.

## Validar antes de fiarse
```
python backtest.py --variants --top 80 --days 45
```
Compara v7 original vs cada mejora con R neto de comisiones, PF y drawdown.

## Análisis de las dos estrategias

**FED + WER Pro v7** (base del bot) — lógica sólida y sin repintado en lo importante (open diario con `lookahead_on` es correcto porque el open ya se conoce; HTF con `[1]`). Puntos débiles:
- El “fallo” solo exige que el mínimo retroceda ≥56%: una vela que pincha y **cierra otra vez por encima del máximo roto** cuenta como fallo. → **Mejora activa:** `REQUIRE_CLOSE_INSIDE` (la vela de fallo debe cerrar dentro del rango).
- Sin filtro de tamaño de stop, en alts con ATR bajo la comisión se come buena parte del R. → **Mejora activa:** `MIN_RISK_PCT=0.3`.
- Trailing 0.65/0.65 ATR = a +0.65 ATR el stop queda en entrada: corta muchos trades que luego llegan a TP. Probar `USE_TRAILING=false` o `USE_MULTI_TP=true` en el backtest.

**Extreme Fade + Premium v7** — no la uso como base, tiene bugs:
- `request.security("D", open)` sin `lookahead_on` → en histórico usa el open/close del **día anterior**: el % diario y el RS del backtest están mal y los resultados no son fiables.
- `btcClose` diario sin lookahead → mismo problema.
- `trail_points` en precio en vez de ticks; el `strategy.exit` del trailing se pisa con los de TP1/TP2; el TP1 parcial no tiene stop.
- `vwapLen` no se usa; “Segundo símbolo” por defecto es BTC, así que la prima no significa nada.
- Lo aprovechable (prima vs VWAP diario y ROC) está integrado como filtros opcionales: `USE_PREMIUM`, `USE_ROC`.

## Comandos útiles de configuración
- Solo cortos: `ALLOW_LONG=false`
- Menos ruido: `MIN_SCORE=55`, `MAX_SIGNALS_DAY=8`
- Ver por qué se bloquean señales: `SEND_BLOCKED=true`
- Lista fija de pares: `SYMBOLS=SOL-USDT,DOGE-USDT,...`
