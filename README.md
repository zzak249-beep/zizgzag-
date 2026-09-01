# Bot Wavelet MRA — BingX

Descomposición multiescala causal + cruce sobre la aproximación, con el
filtro de régimen **corregido**.

**Arranca en SIGNAL.** Lee lo que viene antes de cambiarlo.

---

## El fallo del script original, medido

El script de partida compara la energía de las escalas gruesas (4 y 8
barras) contra las finas (1 y 2), y llama "tendencia" a que ese ratio
supere 1,5.

Lo simulé sobre un **paseo aleatorio puro** — ruido sin ninguna
tendencia:

| Serie | Ratio mediano | % del tiempo con ratio > 1,5 |
|---|---|---|
| **Ruido puro** | 3,04 | **92,6%** |
| Con tendencia | 5,75 | 99,5% |
| Oscilante | 1,21 | 30,8% |

El filtro se enciende el 92,6% del tiempo **en ruido puro**. No
distingue tendencia de aleatoriedad; solo descarta mercados
fuertemente oscilantes.

**La causa es matemática:** la diferencia entre dos medias de 8 barras
tiene mucha más varianza que entre dos de 1 barra, así que el
numerador arranca inflado. En un análisis wavelet serio la energía se
normaliza por escala antes de compararla.

**La corrección:** dividir la energía de cada escala por su longitud.
Con eso el mismo ruido puro da mediana **0,75**, y un umbral de 1,30
significa algo.

### Lo que cambia en la práctica

Backtest sintético, mismas series, SL 1,5 ATR / TP 2,5 ATR:

| Modo | Serie | Ops | Expectativa |
|---|---|---|---|
| **Corregido** | tendencia | 57 | **+1,076 R** |
| **Corregido** | ruido | 11 | −0,076 R |
| **Corregido** | oscilante | **0** | — |
| Original | tendencia | 136 | +0,825 R |
| Original | ruido | **146** | +0,215 R |
| Original | oscilante | **54** | **−0,898 R** |

El original opera 146 veces en ruido y 54 en mercado oscilante, donde
pierde casi 0,9 R por operación. El corregido evita ese terreno casi
por completo.

Puedes comparar los dos con `NORMALIZE_SCALES=false` y
`DOMINANCE_THRESHOLD=1.5` — no hace falta que me creas.

---

## Sobre el 71% y el Sharpe 2,44 del hilo original

No los tomes como referencia. Describen una versión con el filtro
encendido el 92% del tiempo — es decir, un cruce de precio sobre SMA(8)
sin filtro efectivo. Cualquier parecido con esta versión es casual.

**Esta estrategia tiene cero operaciones medidas en datos reales.**

---

## Lo que sí hereda: la infraestructura

Todo lo que costó encontrar en producción con los bots anteriores:

- **Margen aislado** por símbolo. Con cruzado, una cascada puede
  liquidar la cuenta entera antes de que salte el stop.
- **Límite global de posiciones** contando las de TODA la cuenta, no
  solo las de este bot. En un desplome las alts se mueven juntas.
- **Pérdida diaria máxima** en R. El circuit breaker por rachas no
  cubre seis pérdidas alternadas con dos ganancias.
- **Reconciliación** contra el exchange: detecta los cierres por SL/TP
  que el bot no vio.
- **Verificación tras respuesta perdida**: si el envío da timeout pero
  la orden existe, no se abre otra encima.
- **Redondeo a la precisión del contrato**, o BingX rechaza la orden.
- **Diario de operaciones reales** en CSV con deslizamiento y R real.
- **Salida por tiempo que solo corta lo que no va a favor.**

---

## Antes de operarlo

```
python backtest.py BTC-USDT 5m 240 --mensual
python backtest.py BTC-USDT,ETH-USDT,SOL-USDT 5m 240
python sweep.py BTC-USDT,ETH-USDT,SOL-USDT 5m 180
```

El primero mide un símbolo con desglose mensual. El segundo, varios. El
tercero prueba combinaciones de umbral y salidas mostrando **las dos
mitades del histórico**: la que solo funciona en la primera es
sobreajuste y lo marca.

Usan el mismo `strategy.py` que ejecuta el bot, así que miden
exactamente lo que operarías.

---

## Despliegue

Servicio **aparte**, volumen en `/data`, `railway_vars_SIGNAL.txt` en el
Raw Editor y `python test_telegram.py` antes de nada.

Para LIVE hacen falta los dos cerrojos: `MODE=LIVE` **y**
`LIVE_CONFIRMED=true`, más claves con permiso de futuros y **sin
retirada**.
