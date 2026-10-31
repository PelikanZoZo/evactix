"""Обновляет /opt/evactix/.env при выкатке. Читает KEY=VALUE из stdin.

- Переданные значения записывает (пустые не затирают существующие).
- Пароли ролей collector и ana генерирует один раз, если их ещё нет.
- Ничего не печатает, кроме списка ключей.
"""
import os
import secrets
import sys

path = sys.argv[1]
env: dict[str, str] = {}
if os.path.exists(path):
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v

for line in sys.stdin:
    line = line.rstrip("\n")
    if "=" in line:
        k, v = line.split("=", 1)
        if v:
            env[k] = v

for k in ("COLLECTOR_DB_PASSWORD", "ANA_DB_PASSWORD"):
    if not env.get(k):
        env[k] = secrets.token_urlsafe(32)

tmp = path + ".tmp"
old = os.umask(0o077)
with open(tmp, "w", encoding="utf-8") as f:
    for k in sorted(env):
        f.write(f"{k}={env[k]}\n")
os.umask(old)
try:
    import pwd
    u = pwd.getpwnam("deploy")
    os.chown(tmp, u.pw_uid, u.pw_gid)
except (KeyError, ImportError):
    pass
os.replace(tmp, path)
print("Ключи в .env:", ", ".join(sorted(env)))
