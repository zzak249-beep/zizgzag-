# 🤖 ZigZag Multi-Symbol Bot — BingX + Telegram

Escanea automáticamente las **top monedas de BingX** buscando breakouts ZigZag cada 15 minutos.

## Archivos
```
main.py              → Punto de entrada + scheduler
strategy.py          → Lógica multi-símbolo + P&L
zigzag.py            → Indicador ZigZag++ (réplica TradingView)
bingx_client.py      → API BingX Futuros
telegram_notifier.py → Notificaciones Telegram
config.py            → Variables de entorno
```

## Deploy en Railway
1. Sube el proyecto a GitHub
2. railway.app → New Project → Deploy from GitHub
3. Pega las variables de entorno (ver abajo)
4. El bot arranca automáticamente
