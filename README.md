# 🤖 BingX HMA + ZigZag Bot — Railway + Telegram

Bot de trading automatizado para **futuros perpetuos en BingX**, desplegado en **Railway** con alertas por **Telegram**.

---

## 🗂 Estructura

```
bingx-bot/
├── bot.py              # Loop principal + estrategia + órdenes reales
├── config.py           # Todos los parámetros
├── telegram_utils.py   # Notificaciones Telegram
├── requirements.txt
├── Procfile            # Railway: proceso worker
├── railway.toml        # Railway: build & restart automático
├── .env.example        # Plantilla de variables de entorno
├── .gitignore
└── README.md
```

---

## ⚙️ Instalación local

```bash
git clone https://github.com/tu-usuario/bingx-bot.git
cd bingx-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Rellena tus claves
python bot.py          # Arranca en modo SIMULADO
```

---

## 🚀 Deploy en Railway (100 % automático)

### 1. Subir a GitHub
```bash
git init
git add .
git commit -m "feat: initial bot"
git remote add origin https://github.com/tu-usuario/bingx-bot.git
git push -u origin main
```

### 2. Conectar Railway
1. Entra en [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**
2. Selecciona tu repositorio `bingx-bot`
3. Railway detecta el `Procfile` y lanza el worker automáticamente

### 3. Variables de entorno en Railway
Ve a tu proyecto → **Variables** → añade:

| Variable | Valor |
|----------|-------|
| `BINGX_API_KEY` | Tu API key de BingX |
| `BINGX_SECRET_KEY` | Tu Secret key de BingX |
| `TELEGRAM_TOKEN` | Token de @BotFather |
| `TELEGRAM_CHAT_ID` | Tu ID numérico (usa @userinfobot) |
| `LIVE_TRADING` | `false` (simulado) → `true` cuando estés listo |

> ✅ Railway reinicia el bot automáticamente si cae.

---

## 📲 Configurar Telegram

1. Busca **@BotFather** en Telegram → `/newbot` → copia el token
2. Busca **@userinfobot** → copia tu ID numérico
3. Añade ambos como variables de entorno

Mensajes que recibirás:
- 🟢 **Señal LONG** detectada
- 🔴 **Señal SHORT** detectada
- ✅ **Orden ejecutada** (en modo live)
- ⚠️ **Errores** importantes

---

## 📊 Estrategia

| Señal | Condición |
|-------|-----------|
| **LONG**  | Precio > último pico **Y** precio > HMA(20) |
| **SHORT** | Precio < último valle **Y** precio < HMA(20) |

**Gestión de riesgo automática:**
- Stop Loss: −1.5 %
- Take Profit: +3.0 % (ratio 1:2)
- Apalancamiento: 5×

---

## ⚠️ Riesgo

> Operar futuros con apalancamiento implica alto riesgo de pérdida. Empieza siempre con `LIVE_TRADING=false` y prueba al menos 1 semana en modo simulado antes de activar órdenes reales.
