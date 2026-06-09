"""Telegram notification client."""
import logging
import httpx

log = logging.getLogger("qfjp.telegram")


class TelegramClient:
    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self._http   = httpx.AsyncClient(
            base_url="https://api.telegram.org", timeout=12.0
        )

    async def send(self, text: str) -> bool:
        try:
            r = await self._http.post(
                f"/bot{self.token}/sendMessage",
                json={
                    "chat_id":    self.chat_id,
                    "text":       text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            r.raise_for_status()
            return True
        except Exception as exc:
            log.error(f"Telegram send failed: {exc}")
            return False
