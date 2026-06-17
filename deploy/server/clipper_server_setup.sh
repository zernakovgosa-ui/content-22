#!/usr/bin/env bash
# ============================================================================
#  Установка клиппера на сервер (Ubuntu). Идемпотентно.
#  Ожидает /root/clipper_transfer.tgz (код+конфиги, без медиа).
#  Ставит python venv + ffmpeg, распаковывает в /opt/clipper, ставит зависимости.
# ============================================================================
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "==> [1/6] Система: python venv, ffmpeg"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip ffmpeg

echo "==> [2/6] Распаковка в /opt/clipper"
mkdir -p /opt/clipper
tar xzf /root/clipper_transfer.tgz -C /opt/clipper
mkdir -p /opt/clipper/clipper/sources/booster /opt/clipper/clipper/output

echo "==> [3/6] venv"
cd /opt/clipper
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q

echo "==> [4/6] Зависимости (wheel-only + yt-dlp)"
.venv/bin/pip install -q -r trezzy-video-worker/requirements.txt
.venv/bin/pip install -q yt-dlp

echo "==> [5/6] Проверка импорта графа"
cd /opt/clipper
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); import clipper.server; from packages.video import render_clips; print('IMPORT_OK')"

echo "==> [6/6] Готово"
echo "CLIPPER_SETUP_DONE"
