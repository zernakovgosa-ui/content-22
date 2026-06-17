# -*- coding: utf-8 -*-
"""Clipper planner — pure logic for the standalone clip-factory tool.

No IO, no network: everything here is deterministic and unit-testable.

  auto_clip_count()  — owner's rule: 3 clips per 10 minutes of source (min 3)
  distribute()       — spread approved clips EVENLY across a category's accounts
  build_schedule()   — turn assignments into a day-by-day plan (max 2 posts/day
                       per account, steady every day until the backlog is done)
  account_health()   — "possible shadowban" heuristic from entered view counts
  HOT_VIEWS          — threshold for the "ролик стреляет" notification (80k)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

HOT_VIEWS = 80_000          # >= this many views → 🚀 notification
SALE_VIEWS = 400_000        # >= this many views → 💰 «канал готов к продаже»
POSTS_PER_DAY = 2           # per account, owner's rule
POST_SLOTS = ("11:00", "18:00")   # local times for the 2 daily slots
HEALTH_MIN_POSTS = 3        # need at least this many measured posts to judge
HEALTH_LOW_VIEWS = 200      # last N posts all under this → warn


# ── Buster clip-payout vertical (чистая логика выплат) ──────────────────────
def payout_for_views(views: Any, table: Sequence[Sequence[int]]) -> int:
    """Выплата за один ролик по таблице порогов — НЕ суммируется.

    table = [[threshold, payout], ...] (например [[100000,15],[250000,25],...]).
    Берётся ВЫСШИЙ достигнутый порог (views >= threshold) — ровно правило Buster
    «просмотры не суммируются». Если ни один порог не достигнут → 0.
    """
    if not isinstance(views, (int, float)) or views < 0:
        return 0
    best = 0
    for row in table or []:
        try:
            thr, pay = int(row[0]), int(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if views >= thr and pay > best:
            best = pay
    return best


def days_left_to_payout(posted_at_iso: Optional[str], window_days: int,
                        now: datetime) -> Optional[int]:
    """Сколько дней осталось в окне сдачи (window_days от публикации).

    Отрицательное = окно уже закрыто (ролик старше). None = нет/битая дата.
    """
    if not posted_at_iso:
        return None
    try:
        dt = datetime.fromisoformat(posted_at_iso)
        # posted_at может нести смещение («…+00:00»), а now обычно наивный →
        # вычитание разнотипных datetime бросает TypeError и роняет выплаты.
        # Сравниваем оба как наивные (для счёта в ДНЯХ смещение несущественно).
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        base = now.replace(tzinfo=None) if now.tzinfo is not None else now
        elapsed = (base - dt).total_seconds() / 86400.0
    except Exception:
        return None
    return int(round(window_days - elapsed))


def is_payout_due(posted_at_iso: Optional[str], window_days: int, now: datetime) -> bool:
    """Окно ещё открыто (ролик не старше window_days) → клип можно сдавать."""
    dl = days_left_to_payout(posted_at_iso, window_days, now)
    return dl is not None and dl >= 0


def buster_earnings(clips: Dict[str, Dict[str, Any]], plan: Sequence[Dict[str, Any]],
                    accounts: Sequence[Dict[str, Any]], table: Sequence[Sequence[int]],
                    category: str = "buster") -> Dict[str, Any]:
    """Заработок по ОПУБЛИКОВАННЫМ buster-клипам (per posted video).

    Возвращает {total, by_account:{acc_id: usd}, rows:[{clip_id, account_id,
    views, payout, posted_at}]}. Внутри одного ролика порог не суммируется
    (payout_for_views); заработки РАЗНЫХ роликов складываются — это законно.
    """
    by_account: Dict[str, int] = {}
    rows: List[Dict[str, Any]] = []
    total = 0
    for p in plan or []:
        if p.get("status") != "posted":
            continue
        clip = (clips or {}).get(p.get("clip_id")) or {}
        if clip.get("category") != category:
            continue
        views = clip.get("views")
        payout = payout_for_views(views, table)   # сам валидирует int/float/мусор
        acc_id = p.get("account_id")
        by_account[acc_id] = by_account.get(acc_id, 0) + payout
        total += payout
        rows.append({"clip_id": p.get("clip_id"), "account_id": acc_id,
                     "views": views, "payout": payout,
                     "posted_at": p.get("posted_at") or p.get("marked_at")})
    return {"total": total, "by_account": by_account, "rows": rows}


def auto_clip_count(duration_s: Optional[float], per_10min: int = 3) -> int:
    """N клипов на 10 минут (per_10min, задаётся в дашборде), но не меньше per_10min.

    Раньше «3 на 10 мин» было зашито жёстко; теперь владелец выбирает сам.
    """
    try:
        per = int(per_10min)
    except (TypeError, ValueError):
        per = 3
    per = max(1, min(per, 20))            # разумные рамки 1..20
    if not duration_s or duration_s <= 0:
        return per
    return max(per, int(round(duration_s / 600.0 * per)))


def distribute(clip_ids: Sequence[str], account_ids: Sequence[str],
               existing_counts: Optional[Dict[str, int]] = None) -> Dict[str, List[str]]:
    """Assign clips to accounts as evenly as possible.

    existing_counts = clips already queued per account (so a rebuild keeps
    balance). Returns {account_id: [clip_id, ...]}.
    """
    out: Dict[str, List[str]] = {a: [] for a in account_ids}
    if not account_ids:
        return out
    counts = {a: int((existing_counts or {}).get(a, 0)) for a in account_ids}
    for cid in clip_ids:
        # Pick the account with the lowest current load (stable order on ties).
        target = min(account_ids, key=lambda a: (counts[a] + len(out[a]), account_ids.index(a)))
        out[target].append(cid)
    return out


def build_schedule(assignments: Dict[str, List[str]], start: date,
                   busy: Optional[Dict[str, Dict[str, int]]] = None,
                   now_hm: Optional[str] = None) -> List[Dict[str, Any]]:
    """Turn {account: [clips]} into plan entries.

    busy = {account_id: {iso_date: n_posts_already_planned}} so new clips fill
    remaining slots instead of double-booking a day.
    now_hm = current 'HH:MM': slots на стартовый день, которые УЖЕ прошли,
    пропускаются — иначе план срабатывает мгновенно пачкой (реальный кейс).

    Returns [{clip_id, account_id, date: 'YYYY-MM-DD', slot: 'HH:MM'}, ...] —
    each account gets at most POSTS_PER_DAY posts per day, every day, until its
    backlog is exhausted.
    """
    plan: List[Dict[str, Any]] = []
    busy = busy or {}
    for acc, clips in assignments.items():
        day = start
        acc_busy = dict(busy.get(acc, {}))
        i = 0
        while i < len(clips):
            taken = int(acc_busy.get(day.isoformat(), 0))
            for slot_idx in range(taken, POSTS_PER_DAY):
                if i >= len(clips):
                    break
                slot = POST_SLOTS[min(slot_idx, len(POST_SLOTS) - 1)]
                if now_hm and day == start and slot <= now_hm:
                    continue   # этот слот сегодня уже прошёл
                plan.append({
                    "clip_id": clips[i],
                    "account_id": acc,
                    "date": day.isoformat(),
                    "slot": slot,
                })
                i += 1
            day = day + timedelta(days=1)
    plan.sort(key=lambda p: (p["date"], p["slot"], p["account_id"]))
    return plan


def account_health(recent_views: Sequence[Optional[int]]) -> str:
    """'ok' | 'warn' from the account's most recent entered view counts.

    warn = at least HEALTH_MIN_POSTS measured posts AND the last
    HEALTH_MIN_POSTS of them are ALL under HEALTH_LOW_VIEWS — the classic
    "просмотры умерли" shadowban smell. Unmeasured posts (None) are skipped.
    """
    measured = [v for v in recent_views if isinstance(v, int)]
    if len(measured) < HEALTH_MIN_POSTS:
        return "ok"
    tail = measured[-HEALTH_MIN_POSTS:]
    return "warn" if all(v < HEALTH_LOW_VIEWS for v in tail) else "ok"


__all__ = ["auto_clip_count", "distribute", "build_schedule", "account_health",
           "HOT_VIEWS", "SALE_VIEWS", "POSTS_PER_DAY", "POST_SLOTS",
           "payout_for_views", "days_left_to_payout", "is_payout_due", "buster_earnings"]
