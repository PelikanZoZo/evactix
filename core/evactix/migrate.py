"""Применяет SQL-миграции по порядку и выставляет пароли ролей. Запускается от владельца базы."""
import os
import sys
from importlib import resources

import psycopg
from psycopg import sql

from .config import settings


def migrate(conn: psycopg.Connection) -> list[str]:
    conn.execute("create table if not exists schema_migrations (name text primary key, applied_at timestamptz not null default now())")
    done = {r[0] for r in conn.execute("select name from schema_migrations")}
    applied = []
    files = sorted(p for p in resources.files("evactix.migrations").iterdir() if p.name.endswith(".sql"))
    for f in files:
        if f.name in done:
            continue
        conn.execute(f.read_text(encoding="utf-8"))
        conn.execute("insert into schema_migrations(name) values (%s)", (f.name,))
        applied.append(f.name)
    return applied


def set_role_passwords(conn: psycopg.Connection) -> list[str]:
    changed = []
    for role, env in (("collector", "COLLECTOR_DB_PASSWORD"), ("ana", "ANA_DB_PASSWORD")):
        pw = os.getenv(env, "")
        if pw:
            conn.execute(sql.SQL("alter role {} with login password {}").format(sql.Identifier(role), sql.Literal(pw)))
            changed.append(role)
    return changed


def main() -> int:
    s = settings()
    with psycopg.connect(s.dsn(), autocommit=True) as conn:
        applied = migrate(conn)
        roles = set_role_passwords(conn)
        from .catalog import sync_catalog
        sync_catalog(conn)
    print(f"Миграции: {', '.join(applied) or 'новых нет'}; пароли ролей: {', '.join(roles) or 'не заданы'}; справочники обновлены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
