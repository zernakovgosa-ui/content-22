#!/usr/bin/env bash
# ============================================================================
#  Занести секретные ключи в settings.json на сервере.
#  Значения вводишь ТЫ (копируешь из своего локального data/settings.json).
#  Скрипт ничего никуда не отправляет — пишет только в локальный settings.json.
#  Запуск:  bash /opt/content/deploy/server/set_keys.sh
# ============================================================================
set -e
SET=/opt/content/data/settings.json
[ -f "$SET" ] || echo '{}' > "$SET"

echo "================================================================"
echo " Вставь значение каждого ключа и жми Enter."
echo " Пустой Enter = пропустить (оставить прежнее)."
echo " Значения бери из своего локального файла data\\settings.json"
echo "================================================================"
read -rp "Telegram bot token        : " TG_TOKEN
read -rp "Telegram chat id          : " TG_CHAT
echo "Groq: можно вставить СРАЗУ НЕСКОЛЬКО ключей через пробел или запятую"
echo "(пул ротируется при лимитах бесплатного Groq — чем больше, тем лучше)."
read -rp "Groq API keys             : " GROQ
read -rp "Pexels API key (необяз.)  : " PEXELS

TG_TOKEN="$TG_TOKEN" TG_CHAT="$TG_CHAT" GROQ="$GROQ" PEXELS="$PEXELS" \
python3 - "$SET" <<'PY'
import json, os, sys
p = sys.argv[1]
try:
    d = json.load(open(p, encoding="utf-8"))
except Exception:
    d = {}
def setk(key, env):
    v = (os.environ.get(env) or "").strip()
    if v:
        d[key] = v
setk("telegram_bot_token", "TG_TOKEN")
setk("telegram_chat_id",   "TG_CHAT")
setk("pexels_api_key",     "PEXELS")
groq_raw = (os.environ.get("GROQ") or "").replace(",", " ").split()
if groq_raw:
    d["groq_api_keys"] = groq_raw       # пул для ротации при лимитах Groq
    d["groq_api_key"] = groq_raw[0]     # запасной + для выбора провайдера
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("✅ ключи записаны в settings.json")
PY

systemctl restart clipper
sleep 2
echo "✅ клиппер перезапущен — ключи активны"
echo "Проверка: открой дашборд и кинь ссылку — клипы должны прийти в Telegram."
