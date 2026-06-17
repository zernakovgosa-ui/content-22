#!/usr/bin/env bash
# ============================================================================
#  Установщик личной cobalt-качалки на VPS (Ubuntu 22.04, root).
#  Идемпотентно — можно запускать повторно.
#  Поднимает: cobalt (9000) + yt-session-generator (poToken) + watchtower.
#  Запуск:  sudo bash setup.sh
# ============================================================================
set -euo pipefail

echo "==> [0/6] Базовые пакеты"
command -v curl >/dev/null 2>&1 || { apt-get update -y && apt-get install -y curl; }

echo "==> [1/6] Публичный IP"
PUBIP="$(curl -fsS https://api.ipify.org || curl -fsS https://ifconfig.me || true)"
[ -z "${PUBIP:-}" ] && PUBIP="89.124.67.18"
echo "    IP = $PUBIP"

echo "==> [2/6] Swap 2G (критично для 1 ГБ RAM — иначе ffmpeg/chromium валят сервер по памяти)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null || true
  echo "    swap 2G подключён"
else
  echo "    swap уже есть — пропускаю"
fi

echo "==> [3/6] Docker + compose-плагин"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
else
  echo "    docker уже есть"
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "    compose-плагина нет — ставлю"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y && apt-get install -y docker-compose-plugin || curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

echo "==> [4/6] Конфиг в /opt/cobalt"
mkdir -p /opt/cobalt && cd /opt/cobalt
if [ -f api_key.txt ]; then KEY="$(cat api_key.txt)"; else KEY="$(cat /proc/sys/kernel/random/uuid)"; echo "$KEY" > api_key.txt; fi
cat > keys.json <<JSON
{ "$KEY": { "name": "clipper", "limit": "unlimited" } }
JSON
cat > docker-compose.yml <<YML
services:
  cobalt:
    image: ghcr.io/imputnet/cobalt:11
    init: true
    read_only: true
    restart: unless-stopped
    container_name: cobalt
    ports: ["9000:9000/tcp"]
    environment:
      API_URL: "http://$PUBIP:9000/"
      YOUTUBE_SESSION_SERVER: "http://yt-session-generator:8080/"
      API_KEY_URL: "file:///keys.json"
      API_AUTH_REQUIRED: "1"
      DURATION_LIMIT: "18000"
      RATELIMIT_MAX: "200"
      TUNNEL_RATELIMIT_MAX: "200"
    volumes: [ "./keys.json:/keys.json:ro" ]
    labels: [ "com.centurylinklabs.watchtower.scope=cobalt" ]
  yt-session-generator:
    image: ghcr.io/imputnet/yt-session-generator:webserver
    init: true
    restart: unless-stopped
    container_name: yt-session-generator
    labels: [ "com.centurylinklabs.watchtower.scope=cobalt" ]
  watchtower:
    image: ghcr.io/containrrr/watchtower
    restart: unless-stopped
    command: --cleanup --scope cobalt --interval 900 --include-restarting
    volumes: [ "/var/run/docker.sock:/var/run/docker.sock" ]
YML

echo "==> [5/6] Фаервол: открыть 9000 (порт 22 не трогаю)"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -qi active; then ufw allow 9000/tcp || true; fi

echo "==> [6/6] Старт контейнеров"
docker compose up -d
sleep 6
docker compose ps || true

echo
echo "============================================================"
echo " ГОТОВО.  Качалка:  http://$PUBIP:9000/"
echo " API-ключ:  $KEY"
echo
echo " На ПК в data/settings.json пропиши:"
echo "   \"cobalt_self_host\": \"http://$PUBIP:9000\","
echo "   \"cobalt_api_key\":   \"$KEY\","
echo " и перезапусти CLIPPER.bat"
echo "============================================================"
