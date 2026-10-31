"""Применяет SQL-миграции по порядку и выставляет пароли ролей. Запускается от владельца базы."""
import os
import sys
from importlib import resources

import psycopg
from psycopg import sql

from . import __version__
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


def write_system_info(conn: psycopg.Connection) -> None:
    info = {"product": "Evactix", "release": __version__, "commit": os.getenv("EVACTIX_COMMIT", "dev")[:12]}
    for k, v in info.items():
        conn.execute("""insert into system_info (key, value, updated_at) values (%s,%s,now())
                        on conflict (key) do update set value=excluded.value, updated_at=now()""", (k, v))
    conn.execute("""insert into system_info (key, value, updated_at) values ('deployed_at', now()::text, now())
                    on conflict (key) do update set value=excluded.value, updated_at=now()""")


def main() -> int:
    s = settings()
    with psycopg.connect(s.dsn(), autocommit=True) as conn:
        applied = migrate(conn)
        roles = set_role_passwords(conn)
        from .catalog import sync_catalog
        sync_catalog(conn)
        write_system_info(conn)
    print(f"Evactix v{__version__}. Миграции: {', '.join(applied) or 'новых нет'}; пароли ролей: {', '.join(roles) or 'не заданы'}; справочники обновлены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
