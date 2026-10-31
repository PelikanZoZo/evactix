"""Собственное состояние Аны: тревоги и отметки. Хранится у неё (SQLite), не в ядре —
чтобы Ана помнила, что уже сообщила, даже когда база ядра недоступна."""
import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("""create table if not exists alerts (
            key text primary key, level text not null, message text not null,
            first_seen text not null, last_sent text, resolved_at text)""")
        self.db.execute("create table if not exists kv (key text primary key, value text not null, updated_at text not null)")

    # --- тревоги
    def alert(self, key: str):
        r = self.db.execute("select level, message, first_seen, last_sent, resolved_at from alerts where key=?",
                            (key,)).fetchone()
        if r is None:
            return None
        return {"level": r[0], "message": r[1], "first_seen": r[2],
                "last_sent": datetime.fromisoformat(r[3]) if r[3] else None,
                "resolved_at": r[4]}

    def open_alert(self, key: str, level: str, message: str, sent: bool) -> None:
        self.db.execute("""insert into alerts (key, level, message, first_seen, last_sent, resolved_at)
                           values (?,?,?,?,?,null)
                           on conflict(key) do update set level=excluded.level, message=excluded.message,
                             first_seen=excluded.first_seen, last_sent=excluded.last_sent, resolved_at=null""",
                        (key, level, message, _now(), _now() if sent else None))

    def touch_alert(self, key: str, level: str, message: str) -> None:
        self.db.execute("update alerts set last_sent=?, level=?, message=? where key=?", (_now(), level, message, key))

    def active_alerts(self) -> list[tuple[str, str]]:
        return self.db.execute("select key, message from alerts where resolved_at is null").fetchall()

    def resolve(self, key: str) -> None:
        self.db.execute("update alerts set resolved_at=? where key=?", (_now(), key))

    # --- отметки
    def get(self, key: str):
        r = self.db.execute("select value from kv where key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def set(self, key: str, value) -> None:
        self.db.execute("""insert into kv (key, value, updated_at) values (?,?,?)
                           on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at""",
                        (key, json.dumps(value), _now()))
