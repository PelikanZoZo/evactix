"""Настройки из переменных окружения. Секреты в код и логи не попадают."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_host: str = os.getenv("DB_HOST", "db")
    db_port: int = int(os.getenv("DB_PORT", "5432"))
    db_name: str = os.getenv("DB_NAME", "evactix")
    db_user: str = os.getenv("DB_USER", "evactix")
    db_password: str = os.getenv("DB_PASSWORD", "")
    raw_dir: str = os.getenv("RAW_DIR", "/data/raw")
    user_agent: str = os.getenv("HTTP_USER_AGENT", "Evactix/1.0 (+https://evactix.ru; dev@evactix.local)")
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat: str = os.getenv("TELEGRAM_CHAT_ID", "")
    disk_path: str = os.getenv("DISK_PATH", "/data")
    disk_warn_pct: float = float(os.getenv("DISK_WARN_PCT", "70"))
    disk_crit_pct: float = float(os.getenv("DISK_CRIT_PCT", "85"))

    def dsn(self) -> str:
        return (f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
                f"user={self.db_user} password={self.db_password} application_name=evactix")


def settings() -> Settings:
    return Settings()
