#!/usr/bin/env bash
# ============================================================================
#  HTTPS для дашборда + чистый вход в YouTube (без копирования кода).
#  Поднимает Caddy (авто-сертификат Let's Encrypt) на домене сервера,
#  проксирует на клиппер с тем же паролем, и включает public_base_url, чтобы
#  Google возвращал OAuth-код прямо на сервер.
#  Запуск:  bash /opt/content/deploy/server/enable_https.sh
# ============================================================================
set -e
DOMAIN="v629352.hosted-by-vdsina.com"
export DEBIAN_FRONTEND=noninteractive

echo "==> [1/6] фаервол: открыть 443"
(command -v ufw >/dev/null && ufw status | grep -qi active) && ufw allow 443/tcp || true

echo "==> [2/6] ставлю Caddy (если нет)"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

echo "==> [3/6] пароль (тот же, что был у nginx)"
PASS="$(cat /root/clipper_pass.txt 2>/dev/null || true)"
[ -z "$PASS" ] && { PASS="$(tr -dc A-Za-z0-9 </dev/urandom | head -c 12)"; echo "$PASS" > /root/clipper_pass.txt; }
HASH="$(caddy hash-password --plaintext "$PASS")"

echo "==> [4/6] освобождаю порт 80 (стоп nginx) и пишу Caddyfile"
systemctl stop nginx 2>/dev/null || true
systemctl disable nginx 2>/dev/null || true
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    basic_auth {
        admin $HASH
    }
    reverse_proxy 127.0.0.1:8002
}
CADDY
systemctl restart caddy

echo "==> [5/6] включаю public_base_url (OAuth-redirect на HTTPS)"
python3 - <<PY
import json
p="/opt/content/data/settings.json"
d=json.load(open(p,encoding="utf-8"))
d["public_base_url"]="https://$DOMAIN"
json.dump(d,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
print("   public_base_url = https://$DOMAIN")
PY
systemctl restart clipper

echo "==> [6/6] жду сертификат (Let's Encrypt, ~10-30 сек)..."
sleep 20
echo "================================================================"
echo " ГОТОВО (если сертификат выписался)."
echo " Дашборд:  https://$DOMAIN/   (логин admin, пароль: $PASS)"
echo " OAuth-redirect для Google Cloud:"
echo "     https://$DOMAIN/auth/yt/callback"
echo "================================================================"
echo " Проверь статус Caddy:  systemctl status caddy --no-pager | head -5"
