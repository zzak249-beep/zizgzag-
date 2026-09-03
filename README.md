# Sweep Reversal Map — Bot BingX

Bot standalone en Python, independiente del bot wavelet (misma cuenta
BingX, estrategia opuesta: caza reversiones en extremos en vez de
seguir tendencia). Mismo esqueleto que el bot wavelet — reutiliza
`bingx_client.py` y `telegram_notifier.py` sin cambios.

## Origen y qué reconstruí

El script Pine `Sweep Reversal Map [Herman]` que compartiste se cortó
a media que a mitad del bloque bajista (nunca llegó el bloque alcista
ni los plots/alertas). Lo que estaba completo:

- swing high/low (`ta.pivothigh`/`pivotlow`)
- estructura local previa (highest/lowest de N barras)
- sweep bajista: dispara cuando el precio supera un swing high por
  `MIN_PENETRATION_ATR`×ATR, trackea el extremo, espera "reclaim"
  (cierre vuelve a caer bajo el nivel barrido)
- confirmación: reclaim + cierre rompe la estructura previa + vela de
  confirmación con cuerpo ≥ `MIN_DISPLACEMENT_ATR`×ATR (displacement)
- caducidad si no confirma dentro de `MAX_CONFIRMATION_BARS`

Las variables `bullish*` ya estaban declaradas en el mismo orden que
las `bearish*` en el archivo que compartiste — confirma que es un
espejo. Reconstruí ese bloque alcista como espejo exacto (sweep de un
swing low, confirma rompiendo estructura hacia arriba). **Si tu
versión completa difiere en algún detalle del lado alcista, dímelo y
lo ajusto** — no tengo forma de verificarlo sin el resto del archivo.

**Diseño propio** (el Pine original es `indicator()`, no `strategy()`
— es solo visual, no trae SL/TP): SL = nivel barrido ±
`SWEEP_SL_ATR_BUFFER`×ATR de colchón (si se retoma el extremo, la
reversión queda invalidada); TP = riesgo × `SWEEP_RR_RATIO`. Es una
elección razonable, no la única — ajústala a tu gusto.

## Diferencia de arquitectura con el bot wavelet

wavelet_engine es vectorizado (pandas `.rolling()`) porque cada señal
solo depende de la vela actual y una ventana fija hacia atrás.
`sweep_engine.replay_signal` en cambio **reproduce barra a barra**
(bucle de Python) todo el histórico en cada sondeo, porque un sweep
puede tardar varias barras en confirmarse o caducar y ese estado no
se puede calcular con una operación vectorizada simple. Es más caro en
CPU, pero como no depende de sobrevivir un reinicio de Railway (se
recalcula desde cero cada vez sobre la ventana de velas), es más
robusto.

Nota: `long_cond` y `short_cond` son dos máquinas de estado
independientes (igual que en el Pine). En teoría podrían confirmar
ambas en la misma barra por coincidencia — el bot lo detecta y no
entra en ninguna en ese caso (señal ambigua), en vez de elegir una al
azar.

## Puesta en marcha

Igual que el bot wavelet: crea la API Key en BingX (permiso Futuros,
modo Hedge), el bot de Telegram, copia `.env.example` a `.env`, prueba
primero con `LIVE_TRADING=False`, luego sube a GitHub y despliega en
Railway (`Procfile` + `requirements.txt`, Railway detecta Python
solo). El servidor de salud usa el `PORT` que Railway inyecte.

## Notas sobre los parámetros propios del bot

- `TIMEFRAME` (15m por defecto): el script original no fija ninguno.
- `SWEEP_SL_ATR_BUFFER` / `SWEEP_RR_RATIO`: SL/TP, diseño propio (ver arriba).
- `MAX_CONCURRENT_POSITIONS` / `SKIP_IF_SYMBOL_HAS_POSITION`: mismas
  salvaguardas que el bot wavelet — cuentan toda la cuenta, no solo
  este bot, para coordinar de forma segura entre los dos.
