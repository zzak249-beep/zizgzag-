#!/bin/bash
# setup.sh — Limpia el repo y sube el bot correcto a GitHub
# USO: chmod +x setup.sh && ./setup.sh

set -e
echo "🚀 Preparando repo para Railway..."

# 1. Eliminar archivos del bot antiguo (Binance/V49)
echo "🗑  Eliminando archivos obsoletos..."
rm -f main.py config.py exchange.py indicators.py telegram_bot.py
rm -f railway.json runtime.txt

# 2. Confirmar que bot.py existe
if [ ! -f "bot.py" ]; then
  echo "❌ bot.py no encontrado. Descárgalo primero."
  exit 1
fi

echo "✅ Archivos correctos presentes:"
ls -la bot.py requirements.txt Dockerfile railway.toml Procfile .env.example

# 3. Git commit
git add -A
git commit -m "fix: replace Binance bot with BingX Markov bot - fix ModuleNotFoundError"
git push

echo ""
echo "✅ Push completado."
echo ""
echo "Ahora ve a Railway → Variables y añade:"
echo "  BINGX_API_KEY      = tu_key"
echo "  BINGX_SECRET_KEY   = tu_secret"
echo "  TELEGRAM_TOKEN     = tu_token"
echo "  TELEGRAM_CHAT_ID   = tu_chat_id"
echo "  LIVE_TRADING       = false    ← empieza en simulado"
echo "  MAX_SYMBOLS        = 0        ← todas las monedas"
echo "  TIMEFRAME          = 1h"
echo "  LEVERAGE           = 3"
echo "  RISK_PCT           = 1.5"
echo ""
echo "Railway redesplegará automáticamente. ✅"
