#!/usr/bin/env bash
# Первичная настройка сервера Evactix (VPS-1). Запускается от root, повторный запуск безопасен.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
APP_DIR=/opt/evactix

log() { echo "==> $*"; }

log "Система"
. /etc/os-release && echo "ОС: $PRETTY_NAME"
echo "Память: $(free -m | awk '/Mem:/{print $2}') МБ, диск: $(df -h / | awk 'NR==2{print $2}')"

log "Пакеты"
apt-get update -qq
apt-get install -y -qq ufw fail2ban chrony unattended-upgrades ca-certificates curl gzip >/dev/null

log "Время: UTC и синхронизация (важно для событий)"
timedatectl set-timezone UTC
systemctl enable --now chrony >/dev/null 2>&1 || true

log "Автообновления безопасности"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

log "Swap 2 ГБ (если нет)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -q -w vm.swappiness=10; grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf

log "Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || {
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
      https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-x86_64
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  }
fi
docker --version; docker compose version
systemctl enable --now docker >/dev/null

log "Пользователь deploy (вход только по ключу)"
id deploy >/dev/null 2>&1 || useradd -m -s /bin/bash deploy
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
if [ -f /root/.ssh/authorized_keys ]; then
  install -m 600 -o deploy -g deploy /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
fi

if [ "${KEEP_PASSWORD:-0}" = 1 ]; then
  log "SSH: вход по паролю оставлен (временный сервер; защищает fail2ban)"
  rm -f /etc/ssh/sshd_config.d/10-evactix.conf
else
  log "SSH: только ключи"
  cat > /etc/ssh/sshd_config.d/10-evactix.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
fi
sshd -t && systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true

log "Брандмауэр: открыты только 22, 80, 443"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status | head -8

log "fail2ban"
systemctl enable --now fail2ban >/dev/null

log "Папки приложения"
install -d -m 750 -o deploy -g deploy "$APP_DIR" "$APP_DIR/backups"

log "Ежедневный бэкап базы в 03:15 UTC (временно на диск сервера, до S3)"
cat > /etc/cron.d/evactix-backup <<CRON
15 3 * * * deploy $APP_DIR/backup.sh >> $APP_DIR/backups/backup.log 2>&1
CRON
chmod 644 /etc/cron.d/evactix-backup

log "Готово"
