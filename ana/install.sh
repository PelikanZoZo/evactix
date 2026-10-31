#!/bin/bash
# Устанавливает Ану в /opt/evactix-ana. Запускается от root при выкатке.
# Из stdin читает KEY=VALUE (Telegram, версия). Ключ к базе ядра берёт из /opt/evactix/.env.
set -e
SRC=/tmp/evactix-ana
DST=/opt/evactix-ana

mkdir -p "$DST"
rm -rf "$DST/ana"
cp -r "$SRC/ana" "$SRC/pyproject.toml" "$SRC/Dockerfile" "$SRC/.dockerignore" "$SRC/docker-compose.yml" "$DST/"
chown -R deploy:deploy "$DST"

core_pw=$(grep '^ANA_DB_PASSWORD=' /opt/evactix/.env | head -1 | cut -d= -f2-)
[ -n "$core_pw" ] || { echo "В настройках ядра нет ключа для Аны"; exit 1; }

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
echo "Ана: файлы в $DST, ключи в .env: $(printf '%s ' $(printf '%s\n' "${!env[@]}" | sort))"
