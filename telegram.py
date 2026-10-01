"""Envío a Telegram (HTML) con reintentos y respeto de 429."""
import asyncio
import logging

import aiohttp

log = logging.getLogger("tg")


class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat = token, chat_id
        self.enabled = bool(token and chat_id)

    async def send(self, session, text):
        if not self.enabled:
            log.info("[TG off] %s", text.replace("\n", " | "))
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = {"chat_id": self.chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        for k in range(4):
            try:
                async with session.post(url, json=body) as r:
                    js = await r.json(content_type=None)
                if js.get("ok"):
                    return
                wait = js.get("parameters", {}).get("retry_after")
                log.warning("Telegram: %s", js.get("description"))
                if not wait:
                    return
                await asyncio.sleep(wait + 1)
            except Exception as e:
                log.warning("Telegram error: %s", e)
                await asyncio.sleep(2 * (k + 1))
