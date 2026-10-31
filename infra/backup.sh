#!/usr/bin/env bash
# Бэкап базы Evactix: сжатый дамп на диск сервера, хранится 14 дней.
set -euo pipefail
cd /opt/evactix
ts=$(date -u +%Y%m%d-%H%M%S)
docker compose exec -T db pg_dump -U evactix -d evactix --format=custom > "backups/evactix-$ts.dump"
gzip -f "backups/evactix-$ts.dump"
find backups -name 'evactix-*.dump.gz' -mtime +14 -delete
echo "$(date -u +%FT%TZ) бэкап готов: evactix-$ts.dump.gz ($(du -h backups/evactix-$ts.dump.gz | cut -f1))"
