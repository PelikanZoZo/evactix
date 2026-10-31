#!/bin/bash
# Устанавливает Дирижёра в /opt/evactix-conductor. Запускается от root при выкатке.
# Из stdin читает KEY=VALUE (Telegram, версия). Ключ к базе ядра берёт из /opt/evactix/.env.
set -e
SRC=/tmp/evactix-conductor
DST=/opt/evactix-conductor

mkdir -p "$DST"
rm -rf "$DST/conductor"
cp -r "$SRC/conductor" "$SRC/pyproject.toml" "$SRC/Dockerfile" "$SRC/.dockerignore" "$SRC/docker-compose.yml" "$DST/"
chown -R deploy:deploy "$DST"

core_pw=$(grep '^CONDUCTOR_DB_PASSWORD=' /opt/evactix/.env | head -1 | cut -d= -f2-)
[ -n "$core_pw" ] || { echo "В настройках ядра нет ключа для Дирижёра"; exit 1; }

# Собираем .env: старые значения + новые из stdin (пустые не затирают) + ключ ядра
declare -A env
if [ -f "$DST/.env" ]; then
  while IFS='=' read -r k v; do [ -n "$k" ] && env[$k]="$v"; done < "$DST/.env"
fi
while IFS='=' read -r k v; do [ -n "$k" ] && [ -n "$v" ] && env[$k]="$v"; done
env[CORE_DB_PASSWORD]="$core_pw"

umask 077
tmp="$DST/.env.tmp"
: > "$tmp"
for k in $(printf '%s\n' "${!env[@]}" | sort); do printf '%s=%s\n' "$k" "${env[$k]}" >> "$tmp"; done
chown deploy:deploy "$tmp"
mv "$tmp" "$DST/.env"
echo "Дирижёр: файлы в $DST, ключи в .env: $(printf '%s ' $(printf '%s\n' "${!env[@]}" | sort))"
