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

echo "== poToken (bgutil) + Deno (nsig) для ПРЯМОГО yt-dlp — ставлю если нет =="
( docker ps --format '{{.Names}}' 2>/dev/null | grep -qx bgutil-provider \
    || docker run -d --name bgutil-provider --restart unless-stopped -p 4416:4416 \
         brainicism/bgutil-ytdlp-pot-provider >/dev/null 2>&1 ) \
  || echo "  bgutil не поднялся — yt-dlp ослабнет на защищённых роликах"
if [ ! -x /usr/local/bin/deno ]; then
  ( apt-get install -y unzip >/dev/null 2>&1
    curl -fsSL -o /tmp/deno.zip \
      https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
    && unzip -o /tmp/deno.zip -d /usr/local/bin/ && chmod +x /usr/local/bin/deno ) \
    || echo "  Deno не поставился — nsig не решится"
fi
.venv/bin/pip show bgutil-ytdlp-pot-provider >/dev/null 2>&1 \
  || .venv/bin/pip install -q bgutil-ytdlp-pot-provider yt-dlp-ejs \
  || echo "  плагины yt-dlp (bgutil/ejs) не доставились"
echo "== python-multipart (загрузка музыки через дашборд) — ставлю если нет =="
.venv/bin/pip show python-multipart >/dev/null 2>&1 \
  || .venv/bin/pip install -q python-multipart \
  || echo "  python-multipart не встал — загрузка музыки не заработает"
echo "== проверяю импорт (синтаксис) =="
PYTHONPATH=/opt/content .venv/bin/python -c "import clipper.server, clipper.downloader, clipper.planner, packages.video.clip_renderer, packages.video.transcribe, packages.agents.llm_client, packages.video.casino_blur, packages.video.brand_filter"
echo "== импорт OK, перезапускаю клиппер =="
systemctl restart clipper
sleep 2
echo "СТАТУС: $(systemctl is-active clipper)"
