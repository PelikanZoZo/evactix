"""Настройки Аны из переменных окружения."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    db_host: str = field(default_factory=lambda: os.getenv("CORE_DB_HOST", "db"))
    db_port: int = field(default_factory=lambda: int(os.getenv("CORE_DB_PORT", "5432")))
    db_name: str = field(default_factory=lambda: os.getenv("CORE_DB_NAME", "evactix"))
    db_user: str = field(default_factory=lambda: os.getenv("CORE_DB_USER", "ana"))
    db_password: str = field(default_factory=lambda: os.getenv("CORE_DB_PASSWORD", ""))
    edge_url: str = field(default_factory=lambda: os.getenv("CORE_EDGE_URL", "http://edge/health"))
    state_path: str = field(default_factory=lambda: os.getenv("ANA_STATE", "/state/ana.db"))
    disk_path: str = field(default_factory=lambda: os.getenv("DISK_PATH", "/state"))
    disk_warn_pct: float = field(default_factory=lambda: float(os.getenv("DISK_WARN_PCT", "70")))
    disk_crit_pct: float = field(default_factory=lambda: float(os.getenv("DISK_CRIT_PCT", "85")))
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    check_every: int = field(default_factory=lambda: int(os.getenv("CHECK_EVERY", "300")))
    digest_hour_msk: int = field(default_factory=lambda: int(os.getenv("DIGEST_HOUR_MSK", "9")))
    version: str = field(default_factory=lambda: os.getenv("ANA_VERSION", "dev")[:7])

    def dsn(self) -> str:
        return (f"host={self.db_host} port={self.db_port} dbname={self.db_name} user={self.db_user} "
                f"password={self.db_password} application_name=ana connect_timeout=10")
