# Bot de Arranque de Impulso — BingX 30m

Busca el **principio** de un movimiento, no su continuación.

**Arranca en SIGNAL.** Cero operaciones medidas.

---

## Lo primero: esto ya falló una vez

"Entrar a favor del pump" es lo que hacía la **ruptura de rango**, y se
midió con **482 operaciones**: −0,32R en INDEXUS (159 ops), −18% en
CATE, −27% en JIMOTHY. Perdió en los tres.

El motivo es conocido: en un mercado picado el precio sale del rango
constantemente, y casi todas esas salidas mueren en el primer
retroceso. En una vertical, además, el rango de 20 velas queda tan por
debajo que la señal llega cuando el movimiento ya ocurrió.

## Qué se hace distinto aquí

Cuatro condiciones **en la misma vela**:

| Filtro | Qué exige | Por qué |
|---|---|---|
| **Compresión** | rango previo ≤ 3 ATR | Un pump nace de la calma. Si venía dando bandazos, la ruptura es una más |
| **Expansión** | vela ≥ 1,8 ATR, cierre en el tercio alto | Una vela ancha que cierra por la mitad es indecisión |
| **Volumen** | ≥ 2× la media | Sin volumen no es un pump: es un hueco en el libro |
| **Que sea PRONTO** | estirón ≤ 3,5 ATR | **El que lo separa de la ruptura**: si ya está lejos de su media, el movimiento ya pasó |

El cuarto es la única diferencia conceptual real. Los otros tres
aprietan; ese cambia *cuándo* se entra.

**Salida: trailing por ATR.** Los pumps tienen colas largas — la
mayoría no va a ninguna parte y unos pocos corren muchísimo. Un
objetivo fijo cobraría 2R en los que iban a hacer 10R.

---

## Interacción entre filtros que hay que vigilar

La vela de arranque **consume estirón**: si exiges una vela de 1,8 ATR
y a la vez un estirón máximo de 2,0, la ventana es casi imposible y el
bot no disparará nunca. Por eso `MAX_STRETCH_AT_ENTRY` está en 3,5.

Si tocas `MIN_EXPANSION_ATR`, mueve el otro en la misma dirección.

---

## Antes de operarlo: mídelo

```
python backtest.py BTR-USDT 30m 180 --mensual
python backtest.py BTR-USDT,CATE-USDT,JIMOTHY-USDT 30m 180
```

Descarga histórico de Binance (gratis, sin cuenta) y simula con el
**mismo `strategy.py`** que ejecuta el bot. El desglose mensual es lo
que dice si depende del régimen o no.

Si sale como la ruptura de rango, ya lo sabrás sin haber arriesgado
nada. Si sale distinto, tendrás por primera vez una estrategia de
continuación con datos a favor.

---

## Despliegue

Servicio **aparte** de los otros dos. Usa `state_impulse.json`, pero
comparte proceso y logs si lo pones en el mismo sitio.

Volumen en `/data`, `railway_vars_SIGNAL.txt` en el Raw Editor, y
`python test_telegram.py` antes de nada.
