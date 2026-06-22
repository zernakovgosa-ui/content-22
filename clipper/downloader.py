# -*- coding: utf-8 -*-
"""Загрузка исходников с YouTube для нарезчика — устойчиво к РФ-троттлингу.

Проблема: в РФ DPI душит TLS-соединения к *.googlevideo.com (по SNI), поэтому
прямой yt-dlp падает с «_ssl handshake timed out». Решение — качать НЕ напрямую,
а через сервис-резолвер, который тянет видео на своём сервере и ОТДАЁТ БАЙТЫ через
свой домен (твой канал соединяется с доменом сервиса, а не с googlevideo → DPI
нечего душить).

Цепочка методов (по убыванию надёжности обхода), каждый перебирает свои инстансы
и при любой ошибке тихо уходит к следующему:
  1. cobalt   — POST /, alwaysProxy:true → status:"tunnel", готовый mp4 с домена
                инстанса (сервер сам склеивает дорожки). Лучший: до 1080p, без склейки.
  2. invidious— /latest_version?id=..&itag=22&local=true → проксированный muxed mp4.
  3. piped    — /streams/{id} → проксированные дорожки (склейка нашим ffmpeg при нужде).
  4. yt-dlp   — прямой (force-IPv4); сработает только с VPN или прокси (https_proxy).

Чистый stdlib (urllib) + уже стоящий yt-dlp; склейка — нашим ffmpeg (imageio).
Списки инстансов и порядок методов переопределяются через data/settings.json без
правки кода. Возвращаем {"path","file","title","description","duration"}.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

YT_URL_RE = re.compile(
    r"(https?://)?(www\.|m\.)?(youtube\.com/(watch\?|shorts/|live/)|youtu\.be/)", re.I)
VIDEO_ID_RE = re.compile(
    r"(?:v=|/shorts/|/live/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
RESOLVE_TIMEOUT = 30          # таймаут на резолв одного инстанса (быстро отсеять мёртвые)
DOWNLOAD_TIMEOUT = 600        # лимит на скачивание потока байт
MIN_OK_BYTES = 200 * 1024     # меньше — точно не видео (часто HTML-ошибка прокси)

# Дефолтные публичные инстансы (живые на 2026-06; переопределяются в settings.json).
# Публичные инстансы НЕ стабильны — поэтому каждый метод перебирает список.
DEFAULT_COBALT = [
    "https://fox.kittycat.boo",
    "https://cobaltapi.kittycat.boo",
    "https://api.cobalt.blackcat.sweeux.org",
    "https://rue-cobalt.xenon.zone",
    "https://cobalt-backend.canine.tools",
]
DEFAULT_INVIDIOUS = [
    "https://invidious.nerdvpn.de",
    "https://inv.nadeko.net",
    "https://invidious.jing.rocks",
    "https://yewtu.be",
    "https://invidious.privacyredirect.com",
]
DEFAULT_PIPED = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.private.coffee",
    "https://pipedapi.reallyaweso.me",
    "https://pipedapi.darkness.services",
]
DEFAULT_CHAIN = ["cobalt", "invidious", "piped", "ytdlp"]


def looks_like_youtube(url: str) -> bool:
    return bool(YT_URL_RE.search((url or "").strip()))


def _video_id(url: str) -> Optional[str]:
    m = VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


# ── HTTP helpers (везде браузерный UA — дефолтный urllib ловит CF 1010/403) ──
def _ua(settings: Dict[str, Any]) -> str:
    return (settings or {}).get("http_user_agent") or DEFAULT_UA


def _request(url: str, ua: str, data: Optional[bytes] = None,
             headers: Optional[Dict[str, str]] = None,
             method: Optional[str] = None) -> urllib.request.Request:
    h = {"User-Agent": ua, "Accept": "*/*"}
    if headers:
        h.update(headers)
    return urllib.request.Request(url, data=data, headers=h, method=method)


def _get_json(url: str, ua: str, timeout: int) -> Dict[str, Any]:
    with urllib.request.urlopen(_request(url, ua), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post_json(url: str, body: Dict[str, Any], ua: str, timeout: int,
               extra: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    with urllib.request.urlopen(_request(url, ua, data=data, headers=headers, method="POST"),
                                timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _oembed_title(url: str, ua: str) -> str:
    """Название ролика через oEmbed (youtube.com обычно душится мягче, чем googlevideo)."""
    try:
        api = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
        return (_get_json(api, ua, 15).get("title") or "").strip()
    except Exception:
        return ""


def _safe_name(title: str, vid: str) -> str:
    base = re.sub(r'[\\/:*?"<>|]+', " ", (title or "").strip())
    base = re.sub(r"\s+", " ", base).strip()[:70]
    if not base:
        base = f"yt_{vid}"
    return f"{base} [{vid}].mp4"


# ── ffmpeg (для склейки раздельных дорожек invidious/piped) ─────────────────
def _ffmpeg_dir() -> Optional[str]:
    """Папка с ffmpeg.exe для yt-dlp (бинарь imageio называется иначе → копируем)."""
    try:
        import imageio_ffmpeg
        import shutil
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
        bindir = Path(__file__).resolve().parent / "data" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        dst = bindir / "ffmpeg.exe"
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
        return str(bindir)
    except Exception:
        return None


def _ffmpeg_exe() -> Optional[str]:
    d = _ffmpeg_dir()
    if d and (Path(d) / "ffmpeg.exe").exists():
        return str(Path(d) / "ffmpeg.exe")
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _merge_av(video: Path, audio: Path, out: Path) -> bool:
    """Склейка видео+аудио без перекодирования (-c copy). True при успехе."""
    ff = _ffmpeg_exe()
    if not ff:
        return False
    p = subprocess.run([ff, "-y", "-i", str(video), "-i", str(audio),
                        "-c", "copy", "-movflags", "+faststart", str(out)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.returncode == 0 and out.exists() and out.stat().st_size > MIN_OK_BYTES


# ── скачивание потока байт ───────────────────────────────────────────────────
def _looks_like_media(dest: Path) -> bool:
    """Грубая проверка, что скачали видео, а не HTML/JSON-страницу ошибки прокси
    (часто отдаётся с Content-Type октет-стрим/без типа и проходит гейт по размеру)."""
    try:
        with open(dest, "rb") as f:
            head = f.read(64)
    except Exception:
        return False
    return head.lstrip()[:1] not in (b"<", b"{")


def _stream(url: str, dest: Path, ua: str, progress_cb=None,
            timeout: int = DOWNLOAD_TIMEOUT, refresh=None, max_attempts: int = 8) -> bool:
    """Скачать URL в файл с устойчивым РЕЗЮМОМ.

    Догрузка хвоста (Range) с ТОГО ЖЕ URL байт-консистентна (тот же поток), поэтому
    большой 1080p-файл докачивается за несколько попыток на рвущемся канале, а не
    качается вечно с нуля (реальный баг на 3 ГБ ролике). Туннель cobalt пересоздаём
    (refresh) ТОЛЬКО если текущий URL сдох/застрял 2 раза подряд — и тогда качаем С
    НУЛЯ (новый туннель = другие смещения, дозапись хвоста туда била бы mp4).
    Неполный/битый файл в итоге отбраковываем и удаляем.
    """
    def _fail() -> bool:
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        return False

    if dest.exists():
        try:
            dest.unlink()   # стартуем чисто
        except Exception:
            pass
    cur_url = url
    done = 0
    total = 0
    same_url_fails = 0
    for attempt in range(1, max_attempts + 1):
        # Пересоздаём туннель только после 2 подряд обрывов/застоев на текущем URL —
        # старые байты несовместимы с новым туннелем, поэтому начинаем с нуля.
        if refresh and same_url_fails >= 2:
            new = refresh()
            if new:
                cur_url, done, total, same_url_fails = new, 0, 0, 0
        if not cur_url:
            break
        prev_done = done
        use_range = done > 0
        headers = {"Range": f"bytes={done}-"} if use_range else {}
        try:
            with urllib.request.urlopen(_request(cur_url, ua, headers=headers), timeout=timeout) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype or "application/json" in ctype:
                    return _fail()                     # прокси отдал страницу/JSON ошибки
                status = getattr(r, "status", 200) or 200
                if status == 206 and use_range:        # докачка поддержана → дописываем хвост
                    m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range") or "")
                    if m:
                        total = int(m.group(1))
                    mode = "ab"
                else:                                  # Range не поддержан/первый заход → с нуля
                    done = 0
                    total = int(r.headers.get("Content-Length")
                                or r.headers.get("Estimated-Content-Length") or 0)
                    mode = "wb"
                with open(dest, mode) as f:
                    while True:
                        chunk = r.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            try:
                                progress_cb(min(99, int(done * 100 / total)))
                            except Exception:
                                pass
            # поток закрылся без исключения: прогресс был → продолжаем тот же URL,
            # застой (0 байт) → засчитываем как провал URL (после 2 пересоздадим туннель)
            same_url_fails = 0 if done > prev_done else same_url_fails + 1
        except Exception as e:
            done = dest.stat().st_size if dest.exists() else 0
            same_url_fails += 1
            print(f"[downloader] обрыв ({str(e)[:50]}) — догрузка с {done} б "
                  f"(попытка {attempt}/{max_attempts})", flush=True)
            continue
        # Поток закончился — полный ли файл?
        complete = (total and done >= int(total * 0.99)) or (not total and done >= MIN_OK_BYTES)
        if complete:
            if (dest.exists() and dest.stat().st_size >= MIN_OK_BYTES
                    and _looks_like_media(dest)):
                return True
            return _fail()
        print(f"[downloader] неполно {done}/{total or '?'} б — догружаю "
              f"(попытка {attempt}/{max_attempts})", flush=True)
    if (dest.exists() and dest.stat().st_size >= MIN_OK_BYTES
            and (not total or done >= total * 0.95) and _looks_like_media(dest)):
        return True
    return _fail()


# ── Метод 1: cobalt (POST /, alwaysProxy → tunnel) ──────────────────────────
_LAST_COBALT_HOST: Optional[str] = None   # последний рабочий cobalt-инстанс → пробуем первым


def _via_cobalt(url: str, dest_dir: Path, settings: Dict[str, Any],
                progress_cb=None) -> Optional[Dict[str, Any]]:
    global _LAST_COBALT_HOST
    ua = _ua(settings)
    vid = _video_id(url) or "video"
    self_host = (settings.get("cobalt_self_host") or "").strip().rstrip("/")
    instances = settings.get("cobalt_instances") or DEFAULT_COBALT
    # Свой инстанс — ПЕРВЫМ и доверенным (без turnstile-пробы, с запасом на холодный старт).
    hosts = ([self_host] if self_host else []) + [h.rstrip("/") for h in instances if h]
    if _LAST_COBALT_HOST and _LAST_COBALT_HOST in hosts:   # рабочий с прошлого раза — вперёд
        hosts = [_LAST_COBALT_HOST] + [h for h in hosts if h != _LAST_COBALT_HOST]
    api_key = (settings.get("cobalt_api_key") or "").strip()
    max_h = str(settings.get("max_height", 1080) or 1080)
    # vp9 (не h264): у YouTube h264 часто capается на 720p, а 1080p+ есть только в
    # vp9/av1. Берём vp9 → настоящий 1080p исходник → после вертикального кропа
    # картинка резче (h264 давал мыльные 720). Кодек/качество настраиваются в settings.
    vcodec = (settings.get("yt_video_codec") or "vp9").strip()
    body = {"url": url, "videoQuality": max_h, "youtubeVideoCodec": vcodec,
            "youtubeVideoContainer": "auto", "downloadMode": "auto",
            "filenameStyle": "basic", "alwaysProxy": True}
    extra = {"Authorization": f"Api-Key {api_key}"} if api_key else None

    # Живые инстансы ищем ПАРАЛЛЕЛЬНО — иначе ждём таймаут на каждом мёртвом по очереди
    # (это и есть «долго ничего не происходит»). Свой инстанс берём без пробы.
    def _probe(host: str) -> Optional[str]:
        if host == self_host:
            return host
        try:
            info = _get_json(host + "/", ua, 8)
            if (info.get("cobalt") or {}).get("turnstileSitekey"):
                return None
            return host
        except Exception:
            return None

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(hosts) or 1)) as pool:
        alive = [h for h in pool.map(_probe, hosts) if h]
    if not alive:
        return None
    print(f"[clipper] cobalt: живых инстансов {len(alive)}/{len(hosts)}", flush=True)

    for host in alive:
        try:
            def _fresh_tunnel(_host=host):
                """Свежий туннель-URL (cobalt подписывает их с коротким сроком)."""
                resp = _post_json(_host + "/", body, ua, RESOLVE_TIMEOUT, extra)
                status = resp.get("status")
                if status in ("tunnel", "redirect"):
                    return resp.get("url")
                if status == "picker":
                    items = resp.get("picker") or []
                    vids = [it.get("url") for it in items if it.get("type") in (None, "video")]
                    return (vids or [it.get("url") for it in items])[0] if items else None
                return None   # local-processing/error → этот инстанс не отдаёт готовый mp4

            url0 = _fresh_tunnel()
            if not url0:
                continue
            dest = dest_dir / _safe_name(_oembed_title(url, ua), vid)
            # Докачка с возобновлением: при обрыве _stream сам берёт свежий туннель.
            if _stream(url0, dest, ua, progress_cb, refresh=_fresh_tunnel):
                _LAST_COBALT_HOST = host
                print(f"[clipper] cobalt ✓ через {host}", flush=True)
                return {"path": str(dest), "file": dest.name,
                        "title": dest.stem.rsplit(" [", 1)[0],
                        "description": "", "duration": None}
        except Exception as e:
            print(f"[clipper] cobalt {host}: {str(e)[:80]}", flush=True)
            continue
    return None


# ── Метод 2: invidious (local=true — проксированный; 1080p = склейка дорожек) ─
def _via_invidious(url: str, dest_dir: Path, settings: Dict[str, Any],
                   progress_cb=None) -> Optional[Dict[str, Any]]:
    vid = _video_id(url)
    if not vid:
        return None
    ua = _ua(settings)
    max_h = int(settings.get("max_height", 1080) or 1080)
    instances = settings.get("invidious_instances") or DEFAULT_INVIDIOUS

    def _proxy(u: str) -> str:
        """Гонит media-URL через сам инстанс (local=true) — байты минуют googlevideo."""
        if "googlevideo.com" in u and "host=" not in u:
            # /api/v1 уже отдаёт проксируемый путь, но на всякий случай добавим local
            pass
        return u + ("&local=true" if "?" in u else "?local=true")

    def _h(fmt: Dict[str, Any]) -> int:
        m = re.match(r"(\d+)", str(fmt.get("qualityLabel") or fmt.get("resolution") or ""))
        return int(m.group(1)) if m else 0

    for inst in instances:
        host = inst.rstrip("/")
        try:
            meta = _get_json(f"{host}/api/v1/videos/{vid}", ua, RESOLVE_TIMEOUT)
            title = (meta.get("title") or "").strip()
            desc = (meta.get("description") or "").strip()[:2000]
            dur = meta.get("lengthSeconds")
            dest = dest_dir / _safe_name(title, vid)

            # 1080p+ живёт в adaptiveFormats (раздельные video-only + audio-only) →
            # качаем лучшее видео ≤max_h (mp4/avc) + лучшее аудио и склеиваем ffmpeg.
            af = meta.get("adaptiveFormats") or []
            vids = [f for f in af if "video/mp4" in str(f.get("type", "")) and f.get("url")
                    and _h(f) <= max_h]
            vids.sort(key=_h, reverse=True)
            auds = [f for f in af if "audio/mp4" in str(f.get("type", "")) and f.get("url")]
            auds.sort(key=lambda f: int(f.get("bitrate") or 0), reverse=True)
            if vids and auds and _h(vids[0]) >= 1080:
                vtmp = dest_dir / f"_{vid}_v.mp4"
                atmp = dest_dir / f"_{vid}_a.m4a"
                if (_stream(_proxy(vids[0]["url"]), vtmp, ua, progress_cb)
                        and _stream(_proxy(auds[0]["url"]), atmp, ua)
                        and _merge_av(vtmp, atmp, dest)):
                    for t in (vtmp, atmp):
                        try:
                            t.unlink()
                        except Exception:
                            pass
                    print(f"[clipper] invidious ✓ через {host} ({_h(vids[0])}p+audio склейка)", flush=True)
                    return {"path": str(dest), "file": dest.name, "title": title,
                            "description": desc, "duration": dur}
                for t in (vtmp, atmp):
                    try:
                        t.unlink()
                    except Exception:
                        pass

            # Фолбэк: muxed itag 22 (720p) / 18 (360p) — один файл, без склейки.
            for itag in (22, 18):
                furl = f"{host}/latest_version?id={vid}&itag={itag}&local=true"
                if _stream(furl, dest, ua, progress_cb):
                    print(f"[clipper] invidious ✓ через {host} (itag {itag})", flush=True)
                    return {"path": str(dest), "file": dest.name, "title": title,
                            "description": desc, "duration": dur}
        except Exception as e:
            print(f"[clipper] invidious {host}: {str(e)[:80]}", flush=True)
            continue
    return None


# ── Метод 3: piped (/streams/{id} — url уже проксированы инстансом) ──────────
def _is_googlevideo(u: str) -> bool:
    return "googlevideo.com" in (u or "")


def _via_piped(url: str, dest_dir: Path, settings: Dict[str, Any],
               progress_cb=None) -> Optional[Dict[str, Any]]:
    vid = _video_id(url)
    if not vid:
        return None
    ua = _ua(settings)
    max_h = int(settings.get("max_height", 1080) or 1080)
    instances = settings.get("piped_api_instances") or DEFAULT_PIPED
    for inst in instances:
        host = inst.rstrip("/")
        try:
            data = _get_json(f"{host}/streams/{vid}", ua, RESOLVE_TIMEOUT)
            title = (data.get("title") or "").strip()
            desc = (data.get("description") or "").strip()[:2000]
            dur = data.get("duration")
            vstreams = data.get("videoStreams") or []
            astreams = data.get("audioStreams") or []

            def _h(s):  # высота из quality "1080p"/"720p60"
                m = re.match(r"(\d+)", str(s.get("quality") or ""))
                return int(m.group(1)) if m else 0

            # 1) muxed (videoOnly==false) — один файл, без склейки
            muxed = [s for s in vstreams if not s.get("videoOnly")
                     and not _is_googlevideo(s.get("url")) and _h(s) <= max_h]
            muxed.sort(key=_h, reverse=True)
            dest = dest_dir / _safe_name(title, vid)
            if muxed and _stream(muxed[0]["url"], dest, ua, progress_cb):
                print(f"[clipper] piped ✓ через {host} (muxed {_h(muxed[0])}p)", flush=True)
                return {"path": str(dest), "file": dest.name, "title": title,
                        "description": desc, "duration": dur}
            # 2) adaptive: лучшее видео ≤max_h (mp4) + лучшее аудио, склейка ffmpeg
            vids = [s for s in vstreams if s.get("videoOnly")
                    and not _is_googlevideo(s.get("url")) and _h(s) <= max_h
                    and "mp4" in str(s.get("format", "")).lower()]
            vids.sort(key=_h, reverse=True)
            auds = [s for s in astreams if not _is_googlevideo(s.get("url"))]
            auds.sort(key=lambda s: int(s.get("bitrate") or 0), reverse=True)
            if vids and auds:
                vtmp = dest_dir / f"_{vid}_v.mp4"
                atmp = dest_dir / f"_{vid}_a.m4a"
                if (_stream(vids[0]["url"], vtmp, ua, progress_cb)
                        and _stream(auds[0]["url"], atmp, ua)
                        and _merge_av(vtmp, atmp, dest)):
                    for t in (vtmp, atmp):
                        try:
                            t.unlink()
                        except Exception:
                            pass
                    print(f"[clipper] piped ✓ через {host} ({_h(vids[0])}p+audio склейка)", flush=True)
                    return {"path": str(dest), "file": dest.name, "title": title,
                            "description": desc, "duration": dur}
                for t in (vtmp, atmp):
                    try:
                        t.unlink()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[clipper] piped {host}: {str(e)[:80]}", flush=True)
            continue
    return None


# ── Метод 4: yt-dlp напрямую (последний — нужен VPN/прокси в РФ) ─────────────
def _via_ytdlp(url: str, dest_dir: Path, settings: Dict[str, Any],
               progress_cb=None) -> Dict[str, Any]:
    import yt_dlp

    def _hook(d):
        if progress_cb and d.get("status") == "downloading":
            try:
                done = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    progress_cb(int(done * 100 / total))
            except Exception:
                pass

    opts = {
        "format": ("bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"),
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "%(title).70B [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 60,
        "source_address": "0.0.0.0",   # форс IPv4 — лечит повисший IPv6 в РФ-сетях
        # Несколько player-клиентов: tv_embedded/mediaconnect/android_vr ОБХОДЯТ
        # возрастной гейт без входа; остальные — обычный фолбэк. yt-dlp сам возьмёт
        # тот, что отдаёт потоки → возрастные ролики качаются без «нельзя».
        "extractor_args": {"youtube": {"player_client":
            ["tv_embedded", "mediaconnect", "android_vr", "web_safari", "android", "web"]}},
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_hook],
    }
    ffdir = _ffmpeg_dir()
    if ffdir:
        opts["ffmpeg_location"] = ffdir
    proxy = (settings.get("ytdlp_proxy") or settings.get("https_proxy") or "").strip()
    if proxy:
        opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:
            info = info["entries"][0]
        path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
        if not path.exists():
            cand = Path(ydl.prepare_filename(info))
            path = cand if cand.exists() else path
        if not path.exists():
            raise RuntimeError("файл после загрузки не найден")
        return {"path": str(path), "file": path.name,
                "title": (info.get("title") or "").strip(),
                "description": (info.get("description") or "").strip()[:2000],
                "duration": info.get("duration")}


# ── Оркестратор: перебор методов цепочки ────────────────────────────────────
_METHODS = {"cobalt": _via_cobalt, "invidious": _via_invidious, "piped": _via_piped}


def download_youtube(url: str, dest_dir: str | Path, progress_cb=None,
                     settings: Optional[Dict[str, Any]] = None,
                     should_cancel=None) -> Dict[str, Any]:
    """Скачать ролик → {"path","file","title","description","duration"}.

    Перебирает цепочку методов (settings["yt_download_chain"] или DEFAULT_CHAIN).
    Проксирующие резолверы идут первыми (обход РФ-DPI), yt-dlp — последним.
    Бросает RuntimeError с человеческим текстом, если не смог НИ ОДИН метод.
    """
    settings = settings or {}
    url = (url or "").strip()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    chain = settings.get("yt_download_chain") or DEFAULT_CHAIN
    last_ytdlp_err = ""

    for method in chain:
        if should_cancel and should_cancel():
            raise RuntimeError("отменено пользователем")
        try:
            if method == "ytdlp":
                return _via_ytdlp(url, dest_dir, settings, progress_cb)
            fn = _METHODS.get(method)
            if not fn:
                continue
            print(f"[clipper] пробую метод: {method}", flush=True)
            res = fn(url, dest_dir, settings, progress_cb)
            if res:
                return res
            print(f"[clipper] метод {method} не дал результата, дальше", flush=True)
        except Exception as e:
            msg = str(e)
            # Возрастной гейт БОЛЬШЕ НЕ обрывает цепочку: пусть пробуют остальные
            # методы (cobalt с poToken часто берёт 18+, а yt-dlp идёт с клиентами-
            # обходами гейта). Если не смог реально никто — ниже общий текст ошибки.
            last_ytdlp_err = msg[:160]
            print(f"[clipper] метод {method} упал: {msg[:120]}", flush=True)
            continue

    raise RuntimeError(
        "Не удалось скачать ни одним способом (резолверы недоступны/залимичены, "
        "а прямой YouTube душит РФ-DPI" + (f": {last_ytdlp_err}" if last_ytdlp_err else "") + "). "
        "Варианты: 1) включи VPN на весь ПК и нажми ↻ повторить; "
        "2) пропиши рабочий https_proxy в data/settings.json и перезапусти CLIPPER.bat; "
        "3) надёжнее всего — скачай ролик вручную и положи .mp4 в папку sources/бустер, "
        "затем выбери его и нажми Старт.")


__all__ = ["download_youtube", "looks_like_youtube"]
