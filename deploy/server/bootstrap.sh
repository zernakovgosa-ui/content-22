#!/usr/bin/env bash
# ============================================================================
#  ПОЛНЫЙ ДЕПЛОЙ контент-завода на VPS (Ubuntu 22.04, root). Идемпотентно.
#  Поднимает:
#    • cobalt-качалку (docker)         — скачивание роликов через сервер
#    • clipper (systemd, 24/7)         — нарезка/бот/расписание/статистика
#    • nginx + basic-auth (порт 80)    — дашборд открывается с ЛЮБОГО ПК под паролем
#  Запуск:  bash /opt/content/deploy/server/bootstrap.sh
#  Лог:     /root/deploy.log
# ============================================================================
set -uo pipefail
LOG=/root/deploy.log; exec > >(tee -a "$LOG") 2>&1
echo "================ DEPLOY START $(date -u) ================"
REPO_URL="https://github.com/zernakovgosa-ui/content-22.git"
APP=/opt/content
PUBIP="$(curl -fsS https://api.ipify.org 2>/dev/null || echo 89.124.67.18)"
export DEBIAN_FRONTEND=noninteractive

echo "==> [1/10] swap 2G (критично для 1 ГБ RAM)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null || true
fi

echo "==> [2/10] системные пакеты"
apt-get update -y
apt-get install -y git curl ca-certificates gnupg python3 python3-venv python3-pip \
                   ffmpeg libglib2.0-0 nginx apache2-utils unzip

echo "==> [3/10] docker"
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh
systemctl enable --now docker >/dev/null 2>&1 || true

echo "==> [4/10] код (pull / уже распакован / clone)"
if [ -d "$APP/.git" ]; then
  git -C "$APP" pull --ff-only || true
elif [ -f "$APP/clipper/server.py" ]; then
  echo "    код уже на месте (распакован из архива) — пропускаю clone"
else
  git clone "$REPO_URL" "$APP"
fi

echo "==> [5/10] cobalt-качалка (docker)"
bash "$APP/deploy/cobalt/setup.sh" || echo "!! cobalt setup с ошибкой — см. выше"
COBALT_KEY="$(cat /opt/cobalt/api_key.txt 2>/dev/null || echo '')"

echo "==> [6/10] python-окружение клиппера"
cd "$APP"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -q fastapi==0.115.0 "uvicorn[standard]==0.30.6" pydantic==2.9.2 \
    python-dotenv==1.0.1 "pillow>=11" imageio-ffmpeg opencv-python-headless yt-dlp \
    rapidocr-onnxruntime python-multipart \
    bgutil-ytdlp-pot-provider yt-dlp-ejs   # OCR(блюр) + аплоад музыки + poToken + nsig

echo "==> [6b/10] poToken (bgutil docker) + nsig (Deno) — yt-dlp качает YouTube НАПРЯМУЮ"
# bgutil: HTTP-сервер генерит poToken (botguard) для yt-dlp на :4416
docker rm -f bgutil-provider >/dev/null 2>&1 || true
docker run -d --name bgutil-provider --restart unless-stopped -p 4416:4416 \
    brainicism/bgutil-ytdlp-pot-provider >/dev/null 2>&1 \
    || echo "  !! bgutil docker не поднялся — yt-dlp может не брать защищённые ролики"
# Deno: JS-рантайм для решения n-challenge (nsig). Без него ссылки на форматы не
# расшифровываются → yt-dlp ловит «Requested format is not available».
if [ ! -x /usr/local/bin/deno ]; then
  curl -fsSL -o /tmp/deno.zip \
    https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
    && unzip -o /tmp/deno.zip -d /usr/local/bin/ && chmod +x /usr/local/bin/deno \
    || echo "  !! Deno не установился — nsig не решится, прямой yt-dlp ослабнет"
fi

echo "==> [7/10] рабочие папки + settings.json"
mkdir -p "$APP/clipper/data" "$APP/clipper/output/jobs" "$APP/data" \
         "$APP/clipper/sources/видосы" "$APP/clipper/sources/фильмы" "$APP/clipper/sources/сериалы"
SET="$APP/data/settings.json"; [ -f "$SET" ] || echo '{}' > "$SET"
./.venv/bin/python - "$SET" "$PUBIP" "$COBALT_KEY" <<'PY'
import json,sys
p,ip,key=sys.argv[1],sys.argv[2],sys.argv[3]
try: d=json.load(open(p,encoding='utf-8'))
except Exception: d={}
d["yt_download_chain"]=["ytdlp","cobalt","invidious","piped"]
d["cobalt_self_host"]=f"http://{ip}:9000"
if key: d["cobalt_api_key"]=key
d.setdefault("max_height",1080)
json.dump(d,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)
print("   settings.json пропатчен (cobalt self-host + key)")
PY

echo "==> [8/10] systemd-сервис клиппера (24/7)"
cat > /etc/systemd/system/clipper.service <<UNIT
[Unit]
Description=Clipper content factory
After=network-online.target docker.service
Wants=network-online.target
[Service]
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python -m uvicorn clipper.server:app --host 127.0.0.1 --port 8002
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now clipper

echo "==> [9/10] nginx + пароль (доступ с любого ПК)"
if [ -f /root/clipper_pass.txt ]; then PASS="$(cat /root/clipper_pass.txt)"; else PASS="$(tr -dc A-Za-z0-9 </dev/urandom | head -c 12)"; echo "$PASS" > /root/clipper_pass.txt; fi
htpasswd -bc /etc/nginx/.htpasswd admin "$PASS" >/dev/null 2>&1
cat > /etc/nginx/sites-available/clipper <<NGINX
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 0;
    location / {
        auth_basic "Clipper";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host \$host;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/clipper /etc/nginx/sites-enabled/clipper
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo "==> [10/10] фаервол"
if command -v ufw >/dev/null && ufw status | grep -qi active; then ufw allow 80/tcp; ufw allow 9000/tcp; ufw allow 22/tcp; fi

echo "================ ГОТОВО ================"
echo " Дашборд (с любого ПК):  http://$PUBIP/"
echo "   логин: admin    пароль: $(cat /root/clipper_pass.txt)"
echo " Качалка cobalt:         http://$PUBIP:9000/   key=$COBALT_KEY"
echo " Статус: systemctl status clipper --no-pager | docker ps"
echo "DEPLOY_DONE_MARKER"
