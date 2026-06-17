#!/usr/bin/env bash
# Обновить клиппер на сервере: подтянуть код → проверить импорт → перезапустить.
# Если импорт упал (синтаксис/зависимость) — НЕ перезапускаем, старый код живёт.
# Запуск:  bash /opt/content/deploy/server/update.sh
set -e
cd /opt/content
echo "== подтягиваю код =="
git pull --ff-only
echo "== проверяю импорт (синтаксис) =="
PYTHONPATH=/opt/content .venv/bin/python -c "import clipper.server, packages.video.clip_renderer, clipper.planner, packages.agents.llm_client"
echo "== импорт OK, перезапускаю клиппер =="
systemctl restart clipper
sleep 2
echo "СТАТУС: $(systemctl is-active clipper)"
