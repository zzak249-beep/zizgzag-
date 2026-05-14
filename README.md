# 🧠 Sniper Turbo Markov Bot

Implementación en Python del Pine Script "Sniper Bot V48.7: Turbo Markov" para BingX Futures.

## ¿Por qué 8 pares y no más?

| Pares | Problema |
|-------|----------|
| 1-5   | Pocas oportunidades, el filtro density no tiene referencia estadística |
| **6-10**  | **✅ Óptimo: volumen consistente, pivotes limpios, Markov significativo** |
| 11-20 | El filtro density pierde significado (escalas de volumen muy distintas) |
| 500+  | Ruido total, fees destruyen cualquier edge, imposible gestionar riesgo |

Los **8 pares por defecto** tienen el mayor volumen en BingX, lo que hace que el detector de volumen spike (`density`) sea estadísticamente válido.

## La ventaja real: Cadena de Markov

```
Estado actual  →  Probabilidad del próximo estado
─────────────────────────────────────────────────
BULL           →  P(BULL)=62%  P(BEAR)=18%  P(NEUTRAL)=20%
BEAR           →  P(BULL)=21%  P(BEAR)=58%  P(NEUTRAL)=21%
NEUTRAL        →  P(BULL)=35%  P(BEAR)=33%  P(NEUTRAL)=32%
```

El bot solo opera cuando la probabilidad histórica de éxito supera el 40%. Esto elimina entradas en mercados transitorios y opera solo en tendencias con respaldo estadístico.

## Lógica de entrada (exacta del Pine Script)

```
LONG:  low < pivot_low  AND close < VWAP AND slope > +30 AND vol > avg×2 AND P(BULL) > 40%
SHORT: high > pivot_high AND close > VWAP AND slope < -30 AND vol > avg×2 AND P(BEAR) > 40%
```

- **Dip buy**: precio toca soporte (pivot low) con volumen explosivo, momentum positivo
- **Fade breakout**: precio rompe resistencia (pivot high) con volumen explosivo, momentum negativo

## Instalación rápida

```bash
git clone https://github.com/tu-usuario/markov-bot.git
cd markov-bot
cp .env.example .env
# Edita .env con tus claves
python bot.py   # modo simulado por defecto
```

## Deploy en Railway

1. Push a GitHub
2. New Project → Deploy from GitHub repo
3. Variables de entorno (ver `.env.example`)
4. Railway detecta el `Dockerfile` automáticamente

## Variables clave

| Variable | Recomendado | Descripción |
|----------|-------------|-------------|
| `LIVE_TRADING` | `false` → 30 días sim → `true` | Modo real |
| `TIMEFRAME` | `15m` | 15m = óptimo Markov (historia suficiente) |
| `MARKOV_LOOKBACK` | `200` | Ventana histórica para matriz de transición |
| `MARKOV_BULL_MIN` | `40.0` | % mínimo P(BULL) para entrar LONG |
| `SLOPE_MIN` | `30.0` | Pendiente mínima normalizada |
| `DENSITY_MULT` | `2.0` | Volumen debe ser 2× la media |
| `LEVERAGE` | `3` | No subir antes de 30 días live |
| `RISK_PCT` | `1.5` | % del balance por trade |

## ⚠️ Advertencia

Operar futuros con apalancamiento implica riesgo de pérdida total del capital.
Ejecuta mínimo 30 días en modo `LIVE_TRADING=false` antes de activar trades reales.
El win rate mínimo con comisiones de BingX y leverage 3× es ~35%.
