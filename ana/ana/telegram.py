"""Отправка сообщений в Telegram. Токен — часть адреса запроса, поэтому адрес и тело ответа в логи не пишем."""
import logging

import httpx

log = logging.getLogger("ana.telegram")


class Telegram:
    def __init__(self, token: str, chat: str):
        self.token = token
        self.chat = chat
        self.http = httpx.Client(timeout=20)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("telegram не настроен; сообщение: %s", text.splitlines()[0])
            return False
        try:
            r = self.http.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                               json={"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True})
        except Exception as e:
            log.error("telegram недоступен: %s", type(e).__name__)
            return False
        if r.status_code != 200:
            log.error("telegram ответил %s", r.status_code)
            return False
        return True
