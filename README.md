# gold-3mountains-scanner

Escanea **todos** los símbolos USDT-M de BingX buscando el patrón "Tres
Montañas / Tercer Techo Descendente" en velas de 1h — misma lógica exacta
que gold-three-mountains-bot, pero corrida sobre el universo completo en
vez de un solo símbolo.

## Antes de nada

**Este patrón nunca se ha probado con datos reales, en NINGÚN símbolo.**
Fue descrito a mano sobre un gráfico de XAUUSD. Escanear cientos de
símbolos no lo valida — solo multiplica cuántas veces se pone a prueba
la misma hipótesis sin confirmar. Arranca en `MODE=SIGNAL` y déjalo ahí
semanas, no días, antes de considerar `MODE=LIVE`.

**Con más símbolos en juego, más razón para los límites de exposición,
no menos.** `MAX_CONCURRENT_POSITIONS` (5 por defecto) y
`MAX_TRADES_PER_DAY` (10 por defecto, **global**, no por símbolo) existen
para que el bot no abra decenas de posiciones simultáneas si el patrón
se dispara en muchos símbolos el mismo día — algo que no se puede
descartar sin datos reales sobre cómo se comporta este patrón a escala.

**`EXCLUDE_TOKENIZED_ASSETS=true` por defecto.** BingX mezcla cripto real
con instrumentos sintéticos de acciones/materias primas/forex (prefijo
`NC`), que tienen horario de mercado real y pueden generar velas de 1h
con huecos raros durante los cierres — justo el tipo de ruido que
confundiría la detección de picos. `bingx-ict-scanner` (el otro scanner
de este proyecto) nunca tuvo este filtro; aquí sí hace falta, porque
escanea todo el universo y sí se los va a encontrar.

## Diferencias con gold-three-mountains-bot (un solo símbolo)

| | Un solo símbolo | Este scanner |
|---|---|---|
| Símbolos | XAUT-USDT fijo | Todo el universo USDT-M |
| Posiciones | Una a la vez | Varias en paralelo (hasta `MAX_CONCURRENT_POSITIONS`) |
| Límite diario | Por símbolo (n/a con uno solo) | Global, todos los símbolos juntos |
| Filtro de tokenizados | No aplica (XAUT no es "NC") | Sí, activo por defecto |
| Circuit breaker | No | Sí (racha + pérdidas/día) |
| Respaldo diario | No | Sí, a Telegram |
| Tier tracking | No aplica (un solo símbolo) | Sí (major/altcoin) |
| Sesgo HTF | No | Sí (opcional) |

La lógica del patrón (`pattern.py`) es **idéntica** — sin cambios, ya
probada con datos sintéticos en el otro proyecto y reutilizada tal cual.

## Las cuatro protecciones/mejoras añadidas

**Circuit breaker.** Frena señales *nuevas* (nunca toca posiciones ya
abiertas) tras 3 pérdidas seguidas o 3 pérdidas en el mismo día.
Decisión de diseño honesta: este bot no trackea P&L en dólares (solo
cuenta W/L), a diferencia de los scripts de Pine que sí tienen equity
real. En vez de fingir un cálculo de "% de drawdown" con datos que no
existen, el circuit breaker usa lo que el bot mide de verdad — racha y
conteo de pérdidas — documentado con este mismo razonamiento en
`state.py`.

**Respaldo diario a Telegram.** Exactamente la lección que ya pagamos
con `bingx-ict-scanner` (el volumen de Railway se reinició una vez y se
perdió el historial completo sin que nadie se diera cuenta). Envío
directo con confirmación real de entrega — si Telegram no confirma, no
se marca como enviado, se reintenta el próximo ciclo.

**Tracking por tier.** Mismos 5 majors que `bingx-ict-scanner`
(`MAJOR_SYMBOLS`), para poder responder con datos propios si el patrón
rinde distinto en BTC/ETH que en el resto — desde el primer trade, no
descubierto semanas después con operaciones tempranas "sin clasificar".

**Sesgo HTF.** Antes de aceptar un SHORT, exige que el precio esté por
debajo de una EMA en un timeframe superior (`4h` por defecto). Solo se
consulta para símbolos donde el patrón *ya* confirmó la ruptura — una
llamada API extra por señal candidata, no por cada símbolo del universo
en cada ciclo.

## Archivos

- `main.py` — loop principal.
- `scanner.py` — universo de símbolos + escaneo concurrente (con
  semáforo, mismo patrón que bingx-ict-scanner).
- `pattern.py` — la detección del patrón, sin cambios respecto al bot
  de un solo símbolo.
- `config.py` — toda la configuración, con el razonamiento de cada
  decisión documentado en comentarios.
- `state.py` — persistencia atómica, ahora con múltiples posiciones
  simultáneas (symbol -> posición).
- `executor.py` — abre la señal (papel o real), parametrizado por
  símbolo.
- `bingx_client.py`, `telegram_notifier.py`, `healthcheck.py` — mismos
  módulos ya validados en producción, sin cambios.

## Desplegar en Railway

1. Sube esta carpeta completa a un repo de GitHub.
2. Railway: New Project -> Deploy from GitHub repo.
3. Monta un volumen persistente en `/data` -- sin esto, el bot olvida
   todas las posiciones y estadísticas en cada redeploy.
4. Copia las variables de `.env.example`. Como mínimo:
   `BINGX_API_KEY`, `BINGX_API_SECRET`. Deja `MODE=SIGNAL`.
5. Railway arranca con `python main.py` automáticamente
   (`railway.toml`).
6. Configura `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` -- con
   potencialmente varias señales por día en cientos de símbolos, vas a
   querer las notificaciones, no solo los logs de Railway.

## Tests

```
python3 test_pattern.py       # deteccion del patron, datos sinteticos
python3 test_integration.py   # filtro de tokenizados, escaneo multi-simbolo, limites de exposicion
```
