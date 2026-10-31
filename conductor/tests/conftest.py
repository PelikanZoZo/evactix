import json
import os
import pathlib
import uuid

import httpx
import psycopg
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORE_MIGRATIONS = ROOT / "core" / "evactix" / "migrations"
ADMIN_DSN = os.getenv("TEST_ADMIN_DSN", "host=127.0.0.1 port=5432 user=evactix dbname=postgres")


@pytest.fixture
def dbname():
    name = f"cond_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as a:
        a.execute(f'create database "{name}"')
    dsn = ADMIN_DSN.replace("dbname=postgres", f"dbname={name}")
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("create table if not exists schema_migrations (name text primary key)")
        for f in sorted(CORE_MIGRATIONS.glob("*.sql")):
            c.execute(f.read_text(encoding="utf-8").replace("on database evactix", f'on database "{name}"'))
        c.execute("""insert into sources (id, name, source_class, base_confidence, license, max_age) values
                     ('cbr.fx','ЦБ РФ: курсы','open',0.98,'x','2 days'), ('xmlcalendar','Календарь','open',0.95,'x','8 days')""")
        c.execute("""insert into metrics (id, value_type, unit, description) values
                     ('fx.rate_rub','money','RUB','курс'), ('calendar.day_type','category',null,'тип дня')""")
    yield name
    with psycopg.connect(ADMIN_DSN, autocommit=True) as a:
        a.execute(f'drop database "{name}" with (force)')


@pytest.fixture
def admin(dbname):
    with psycopg.connect(ADMIN_DSN.replace("dbname=postgres", f"dbname={dbname}"), autocommit=True) as c:
        yield c


@pytest.fixture
def conn(dbname):
    """Подключение под ролью conductor со своей схемой."""
    from conductor.service import migrate
    with psycopg.connect(f"host=127.0.0.1 port=5432 user=conductor dbname={dbname}", autocommit=True) as c:
        migrate(c)
        yield c


@pytest.fixture
def settings(dbname):
    from conductor.config import Settings
    return Settings(db_host="127.0.0.1", db_name=dbname, db_user="conductor", db_password="",
                    telegram_token="TEST", owner_chat="1001", deepseek_key="k")


class Script:
    """Подставной DeepSeek: отдаёт заранее заданные ответы, запоминает запросы."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def handler(self, req: httpx.Request):
        body = json.loads(req.content)
        self.requests.append(body)
        if not self.replies:
            return httpx.Response(500)
        r = self.replies.pop(0)
        if isinstance(r, int):
            return httpx.Response(r)
        return httpx.Response(200, json={"choices": [{"message": r}]})


def tool_call(name, args, cid="c1"):
    return {"content": "", "tool_calls": [{"id": cid, "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


@pytest.fixture
def make_llm():
    from conductor.llm import DeepSeek

    def make(replies):
        s = Script(replies)
        return DeepSeek("k", http=httpx.Client(transport=httpx.MockTransport(s.handler))), s
    return make
