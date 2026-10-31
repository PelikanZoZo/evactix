"""Настройки Дирижёра из переменных окружения."""
import os
from dataclasses import dataclass, field

# Системные задания сборщиков: имя → интервал в минутах (при первом запуске; дальше правится как задание)
COLLECTORS = {
    "cbr.fx": 180, "cbr.metals": 360, "cbr.keyrate": 360, "xmlcalendar": 1440, "astro": 720,
    "metno.forecast": 60, "usgs.quakes": 15, "gdacs.alerts": 30,
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    db_host: str = field(default_factory=lambda: _env("CORE_DB_HOST", "db"))
    db_port: int = field(default_factory=lambda: int(_env("CORE_DB_PORT", "5432")))
    db_name: str = field(default_factory=lambda: _env("CORE_DB_NAME", "evactix"))
    db_user: str = field(default_factory=lambda: _env("CORE_DB_USER", "conductor"))
    db_password: str = field(default_factory=lambda: _env("CORE_DB_PASSWORD"))
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    owner_chat: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    # Сервер не достаёт до api.telegram.org — ходим через переходник на Cloudflare (infra/telegram-relay)
    telegram_api: str = field(default_factory=lambda: (_env("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/"))
    allowed_chats: str = field(default_factory=lambda: _env("TELEGRAM_ALLOWED"))
    deepseek_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY").strip())
    deepseek_model: str = field(default_factory=lambda: _env("DEEPSEEK_MODEL", "deepseek-chat"))
    commit: str = field(default_factory=lambda: _env("CONDUCTOR_COMMIT", "dev")[:7])
    tick_seconds: int = field(default_factory=lambda: int(_env("TICK_SECONDS", "10")))
    poll_telegram: bool = field(default_factory=lambda: _env("POLL_TELEGRAM", "1") == "1")

    def dsn(self) -> str:
        return (f"host={self.db_host} port={self.db_port} dbname={self.db_name} user={self.db_user} "
                f"password={self.db_password} application_name=conductor connect_timeout=10")

    def allowed(self) -> set[str]:
        ids = {x.strip() for x in self.allowed_chats.split(",") if x.strip()}
        if self.owner_chat:
            ids.add(self.owner_chat.strip())
        return ids
