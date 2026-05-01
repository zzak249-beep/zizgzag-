# 🤖 ZigZag Breakout Bot — BingX + Telegram

Bot de trading automático basado en la estrategia de **Maki@テクニカル先生** (1M reproducciones en TikTok).

Desplegable en **Railway** · Opera en **BingX Futuros** · Notificaciones por **Telegram**

---

## 📐 Estrategia

| Parámetro | Valor |
|---|---|
| Indicador | ZigZag++ (replicado en Python) |
| Timeframe | 15 minutos |
| Señal LONG | Precio cierra **por encima** de la resistencia ZigZag |
| Señal SHORT | Precio cierra **por debajo** del soporte ZigZag |
| Take Profit | +45 pips desde la entrada |
| Stop Loss | −30 pips desde la entrada |
| Apalancamiento | 10x |

### ¿Cómo funciona el ZigZag?
```
        Resistencia ──────┐
                          │← Si el precio rompe aquí → LONG 🟢
    ┌─────────────────────┘
    │
    └────────────────── Soporte
               ↑
    Si el precio rompe aquí → SHORT 🔴
```

---

## 🚀 Despliegue rápido en Railway

### 1. Clonar y subir a GitHub

```bash
git init
git add .
git commit -m "Initial bot"
gh repo create zigzag-bot --private --push
```

### 2. Conectar Railway

1. Ve a [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
2. Selecciona tu repo `zigzag-bot`
3. En la pestaña **Variables**, añade todas las variables de `.env.example`

### 3. Variables de entorno en Railway

```
BINGX_API_KEY       = tu_api_key
BINGX_SECRET_KEY    = tu_secret_key
TELEGRAM_TOKEN      = 123456:ABCdef...
TELEGRAM_CHAT_ID    = -100xxxxxxxxx
SYMBOL              = BTC-USDT
LEVERAGE            = 10
TP_PIPS             = 45
SL_PIPS             = 30
CAPITAL_PER_TRADE   = 50
TIMEFRAME           = 15m
```

### 4. Desplegar

Railway detecta el `Procfile` automáticamente y ejecuta `python main.py`.

---

## ⚙️ Configuración local (desarrollo)

```bash
# 1. Crear entorno virtual
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Configurar variables
cp .env.example .env
# Editar .env con tus claves reales

# 4. Ejecutar
python main.py
```

---

## 📲 Notificaciones Telegram

El bot envía mensajes automáticos para:

| Evento | Mensaje |
|---|---|
| Bot iniciado | 🤖 Configuración completa |
| Señal detectada | 🟢/🔴 ZigZag breakout |
| Orden abierta | Entrada, TP, SL, cantidad |
| Orden cerrada | Precio de salida + **P&L** |
| Error | Descripción del problema |
| Reporte diario | Estadísticas del día (00:01 UTC) |

### Crear tu bot de Telegram
1. Habla con `@BotFather` en Telegram
2. `/newbot` → sigue instrucciones → copia el **TOKEN**
3. Añade el bot a tu grupo o escríbele directamente
4. Obtén tu **CHAT_ID**: envía un mensaje y visita `https://api.telegram.org/bot{TOKEN}/getUpdates`

---

## 🔑 Obtener API keys de BingX

1. Inicia sesión en [BingX](https://bingx.com)
2. Ve a **Perfil → Gestión de API**
3. Crea una clave con permisos: **Lectura + Trading de futuros**
4. ⚠️ **NUNCA actives el permiso de retiro**
5. Añade la IP de Railway como IP permitida (opcional pero recomendado)

---

## 📁 Estructura del proyecto

```
zigzag-bot/
├── main.py              # Punto de entrada, scheduler
├── strategy.py          # Lógica de la estrategia + gestión de estado
├── zigzag.py            # Implementación del indicador ZigZag++
├── bingx_client.py      # Cliente BingX API (futuros perpetuos)
├── telegram_notifier.py # Notificaciones Telegram
├── config.py            # Configuración desde variables de entorno
├── requirements.txt     # Dependencias Python
├── railway.toml         # Configuración Railway
├── Procfile             # Comando de inicio
├── .env.example         # Plantilla de variables de entorno
└── .gitignore
```

---

## ⚠️ Advertencias de riesgo

> **TRADING CON DINERO REAL CONLLEVA RIESGO DE PÉRDIDA TOTAL**

- Empieza con **capital mínimo** (10-20 USDT por operación) hasta verificar el comportamiento
- El apalancamiento **10x amplifica tanto ganancias como pérdidas**
- Ninguna estrategia gana el 100% de las operaciones
- Monitoriza el bot regularmente, especialmente los primeros días
- Establece un límite máximo de pérdida diaria en BingX

---

## 🔧 Cómo mejorar la estrategia

Ver sección **"Cómo mejorar"** en el análisis de la estrategia.

### Ideas de mejora rápidas:
1. **Filtro de tendencia**: solo operar en la dirección del EMA 200
2. **Filtro de volumen**: solo entrar si el volumen del breakout es > media
3. **Filtro de horario**: evitar las 00:00-03:00 UTC (baja liquidez)
4. **Gestión de posición**: añadir trailing stop después de 20 pips de beneficio

---

## 📊 Backtesting

Para probar la estrategia antes de operar en real:

```bash
python backtest.py  # (pendiente de implementar)
```

---

## 📝 Licencia

MIT — Úsalo bajo tu propia responsabilidad.
