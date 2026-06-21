#!/usr/bin/env bash
# Обновить клиппер на сервере: подтянуть код → проверить импорт → перезапустить.
# Если импорт упал (синтаксис/зависимость) — НЕ перезапускаем, старый код живёт.
# Запуск:  bash /opt/content/deploy/server/update.sh
set -e
cd /opt/content
echo "== подтягиваю код =="
git pull --ff-only
echo "== OCR-движок для блюра казино (buster), ставлю если нет =="
.venv/bin/python -c "import rapidocr_onnxruntime" 2>/dev/null \
  || .venv/bin/pip install -q rapidocr-onnxruntime \
  || echo "  rapidocr не установился — блюр будет пропускаться, рендер не пострадает"
echo "== проверяю импорт (синтаксис) =="
PYTHONPATH=/opt/content .venv/bin/python -c "import clipper.server, clipper.downloader, clipper.planner, packages.video.clip_renderer, packages.video.transcribe, packages.agents.llm_client, packages.video.casino_blur, packages.video.brand_filter"
echo "== импорт OK, перезапускаю клиппер =="
systemctl restart clipper
sleep 2
echo "СТАТУС: $(systemctl is-active clipper)"
