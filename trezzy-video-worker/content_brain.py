# -*- coding: utf-8 -*-
"""
TREZZY content brain — turns a simple topic into a full TREZZY short-video plan.

This is a deterministic, template-driven planner. No OpenAI calls. Later we'll
swap `make_plan()` for an LLM-backed version while keeping the same return shape.

Public surface:
    make_plan(topic, product_name, target_audience, style, format, seed) -> dict
    SUPPORTED_FORMATS: set[str]
"""

from __future__ import annotations

import random
import re
from typing import List, Optional

SUPPORTED_FORMATS = {
    "single_review",
    "top_list",
    "mood_story",
    "celebrity_style",
    "problem_solution",
    "luxury_quote",
    "date_night",
    "office_rich",
    "quiet_luxury",
    "perfume_for_mood",
    "ai_ugc_ad",
}

DEFAULT_CTA = "Найди свой аромат на TREZZY"

# ----------------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------------

_HOOKS = {
    "single_review": [
        "Аромат, который пахнет дорого",
        "Один аромат — и тебя запоминают",
        "Это не парфюм. Это подпись.",
        "Аромат для тех, кто умеет молчать",
        "{product_name} — этот аромат говорит за тебя",
        "{product_name}. Запах, который не забывают.",
    ],
    "top_list": [
        "Три аромата. Один характер.",
        "Подборка, которая работает за тебя",
        "Эти три флакона нужны каждому",
        "Топ ароматов под {topic}",
        "Три флакона. Три настроения.",
    ],
    "mood_story": [
        "Ты входишь — и комната замолкает",
        "Запах дорогого отеля после дождя",
        "Шёпот за плечом, который нравится",
        "Так пахнет уверенность в темноте",
        "Аромат, который остаётся в комнате",
    ],
    "celebrity_style": [
        "Так пахнет образ, который запоминают",
        "Аромат в стиле {topic}",
        "Это аромат человека, который умеет паузу",
        "Так пахнет икона",
        "Запах архетипа, не модного парфюма",
    ],
    "problem_solution": [
        "Тебя не запоминают? Дело не в одежде.",
        "Хочется выглядеть дороже? Начни с аромата.",
        "Костюм можно скопировать. Аромат — нет.",
        "Перестань пахнуть как все",
        "{topic}? Поменяй подпись.",
    ],
    "luxury_quote": [
        "«Роскошь — это тишина, которую слышат»",
        "«Запах — это первое, что о тебе помнят»",
        "«Хороший аромат — это пауза в разговоре»",
        "«Парфюм — это то, что нельзя сфотографировать»",
        "«Тишина пахнет дороже, чем громкость»",
    ],
    "date_night": [
        "Аромат, ради которого хочется подойти ближе",
        "Так пахнет вечер, в который не хочется домой",
        "Запах, после которого пишут первыми",
        "Один аромат меняет, как тебя касаются",
        "Аромат для свидания, которое запомнят",
    ],
    "office_rich": [
        "Так пахнут в офисах, где принимают решения",
        "Аромат, после которого спрашивают «что это?»",
        "Костюм + этот аромат = другая лига",
        "Запах человека, у которого нет вопросов к зарплате",
        "Аромат на каждый день — но не как у всех",
    ],
    "quiet_luxury": [
        "Аромат без логотипа. Только характер.",
        "Тихая роскошь, которую слышно",
        "Парфюм для тех, кому уже не нужно доказывать",
        "Без шлейфа на километр. Только на тебе.",
        "Минимализм, который пахнет дорого",
    ],
    "perfume_for_mood": [
        "Аромат под настроение «{topic}»",
        "Подбери запах под состояние",
        "Так пахнет {topic}",
        "Один аромат — одно состояние",
        "Запах, который ставит правильное настроение",
    ],
    "ai_ugc_ad": [
        "Стоп. Понюхай вот это.",
        "Я думала, это парфюм за 30к",
        "Парень спросил, чем я пахну",
        "Это не реклама. Это аромат за свои деньги.",
        "Окей, теперь серьёзно про этот аромат",
        "POV: ты нашёл свой аромат",
        "{product_name}? Так пахнут люди из кино.",
    ],
}

_SCRIPTS = {
    "single_review": [
        "Это аромат для тех, кто хочет выглядеть спокойно, дорого и уверенно. Он не кричит, но его точно запоминают.",
        "Не громкая премьера. Тёплый шлейф, который слышат рядом с тобой. Не сразу, но точно.",
        "Один аромат меняет то, как тебя считывают. Это не парфюм. Это твоя подпись.",
        "Сначала — спокойно. Потом — тепло. В конце — характер, который остаётся в комнате.",
        "{product_name} — это не громкая премьера. Это уверенность, которую слышат рядом с тобой.",
    ],
    "top_list": [
        "Первый — для уверенности. Второй — для встреч. Третий — на вечер.",
        "Утро — лёгкий и чистый. День — собранный и тёплый. Вечер — глубокий и тёмный.",
        "Три флакона. Три настроения. Один человек.",
        "Один — для офиса. Один — для свидания. Один — для тишины.",
    ],
    "mood_story": [
        "Тёплый свет. Тихая музыка. Шёлк рубашки. И аромат, который держит сцену лучше тебя.",
        "Это не парфюм. Это атмосфера, которая идёт за тобой два метра.",
        "Пустая улица. Дорогой пиджак. Запах, который остаётся в подъезде.",
        "Вечер. Низкий свет. Тёплый шлейф, который не отпускает.",
    ],
    "celebrity_style": [
        "Молчит. Заходит. Слышен. Аромат, который мог бы носить кто угодно, но идёт только тем, кто умеет паузу.",
        "Это аромат человека, у которого нет нужды доказывать. Только присутствие.",
        "Без громких аккордов. Без модных нот. Просто характер, который читается.",
    ],
    "problem_solution": [
        "Костюм можно скопировать. Машину — взять в аренду. Аромат — это то, что не подделаешь. Это подпись.",
        "Если тебя не считывают — дело не в одежде. Дело в том, как ты пахнешь.",
        "Поменяй аромат — поменяется первое впечатление. Поменяется и всё остальное.",
    ],
    "luxury_quote": [
        "Не громкость. Не блеск. Просто пауза, которую слышат. Это и есть роскошь.",
        "Настоящая роскошь не объясняет себя. Она просто рядом. Как этот шлейф.",
        "Дорого — это не цена. Это спокойствие, которое читается без слов.",
        "Парфюм — это то, что остаётся после того, как ты ушёл из комнаты.",
    ],
    "date_night": [
        "Тёплый шлейф у шеи. Низкий свет. Аромат, после которого вечер длится дольше.",
        "Это не парфюм. Это причина подойти ближе. И остаться.",
        "Запах, который читают через стол. Через танец. Через паузу.",
        "Один аромат — и вечер собирается сам. Без объяснений.",
    ],
    "office_rich": [
        "Спокойный. Чистый. Дорогой. Аромат для тех, кто заходит в переговорку первым.",
        "Это не «парфюм на каждый день». Это сигнал, который считывают коллеги и клиенты.",
        "Бергамот сверху. Кожа внизу. Между — характер, который не нужно объяснять.",
        "Аромат, который пахнет как уверенность в зарплате.",
    ],
    "quiet_luxury": [
        "Без логотипа. Без громких аккордов. Только характер, который читается с двух метров.",
        "Это аромат для тех, кому уже не нужно доказывать. Просто присутствие.",
        "Тихий, чистый, точный. Так пахнут вещи, которые служат годами.",
        "Минимализм — это не пусто. Это когда ничего лишнего, но всё на месте.",
    ],
    "perfume_for_mood": [
        "Под настроение «{topic}» нужен свой запах. Тёплый, точный, не громкий.",
        "Состояние — это не одежда. Это аромат, который ты выбрал сегодня утром.",
        "Один аромат собирает день. Второй — собирает вечер. Не путай.",
        "Запах под «{topic}» — это короче, чем плейлист, и точнее, чем слова.",
    ],
    "ai_ugc_ad": [
        "Слушай. Один раз нанесла. Парень спросил трижды. Это не парфюм за 30к. Это TREZZY.",
        "Я не реклама. Я носила его две недели. Тёплый. Дорогой. Не громкий. Спрашивают каждый раз.",
        "Этот аромат пахнет как чужие деньги. Но стоит как свои. Серьёзно, попробуй.",
        "Друг подошёл, спросил. Коллега подошла, спросила. Мама подошла, спросила. Один флакон.",
        "POV: ты в лифте. Незнакомец оборачивается. Дело не в одежде. Дело в этом аромате.",
    ],
}

_VIBE_POOLS = {
    "single_review": [
        "спокойствие", "дорогой шлейф", "уверенность",
        "элегантность", "тёплый шлейф", "характер",
        "глубина", "вкус", "тишина",
    ],
    "top_list": [
        "три флакона", "капсула", "разные настроения",
        "утро", "вечер", "офис", "свидание",
        "выбор", "набор",
    ],
    "mood_story": [
        "атмосфера", "вечер", "шёпот",
        "тепло", "сцена", "шёлк",
        "темнота", "присутствие", "пауза",
    ],
    "celebrity_style": [
        "образ", "икона", "статус",
        "архетип", "стиль", "пауза",
        "молчание", "характер",
    ],
    "problem_solution": [
        "решение", "подпись", "перемена",
        "контраст", "статус", "первое впечатление",
        "выход", "ответ",
    ],
    "luxury_quote": [
        "тишина", "роскошь", "пауза",
        "вкус", "характер", "глубина",
        "след", "присутствие",
    ],
    "date_night": [
        "вечер", "шлейф", "тепло",
        "близость", "пауза", "свет",
        "шёлк", "тёмный бар",
    ],
    "office_rich": [
        "офис", "переговорка", "статус",
        "бергамот", "кожа", "кедр",
        "уверенность", "костюм",
    ],
    "quiet_luxury": [
        "минимализм", "тишина", "белый мускус",
        "ирис", "сандал", "без логотипа",
        "чистый", "точный",
    ],
    "perfume_for_mood": [
        "состояние", "под настроение", "вечер",
        "утро", "тёплый день", "капсула",
        "выбор", "ритуал",
    ],
    "ai_ugc_ad": [
        "POV", "честный обзор", "вирусно",
        "тёплый шлейф", "запах денег", "спрашивают",
        "за свои деньги", "не реклама",
    ],
}

_HASHTAG_BASE = [
    "#trezzy", "#parfum", "#perfume", "#fragrance",
    "#niche", "#парфюмерия",
]

_HASHTAGS_BY_FORMAT = {
    "single_review":    ["#обзор", "#аромат", "#нишеваяпарфюмерия"],
    "top_list":         ["#подборка", "#топароматов", "#капсула"],
    "mood_story":       ["#атмосфера", "#вечер", "#mood"],
    "celebrity_style":  ["#стиль", "#образ", "#икона"],
    "problem_solution": ["#первоевпечатление", "#подпись", "#статус"],
    "luxury_quote":     ["#тихаяроскошь", "#luxury", "#цитата"],
    "date_night":       ["#свидание", "#вечер", "#datenight"],
    "office_rich":      ["#офис", "#бизнес", "#статус"],
    "quiet_luxury":     ["#quietluxury", "#тихаяроскошь", "#минимализм"],
    "perfume_for_mood": ["#настроение", "#mood", "#подбор"],
    "ai_ugc_ad":        ["#ugc", "#pov", "#честныйобзор", "#viral"],
}

_EDIT_NOTES = {
    "single_review": (
        "Single review: фокус на одном флаконе. Очень медленный zoom на бутылке. "
        "Voiceover спокойный, низкий. Музыка — niche perfume ambient, низкий BPM. "
        "Минимум cuts. Один яркий close-up в HOOK и один в CTA."
    ),
    "top_list": (
        "Top list: три коротких блока, по одному cut на каждый аромат. "
        "Музыка чуть бодрее, но всё ещё ambient. Captions крупные, ритмичные. "
        "Каждый флакон — отдельный кадр."
    ),
    "mood_story": (
        "Mood story: cinematic. Длинные takes, медленные движения. "
        "Минимум текста на экране, максимум атмосферы. Voiceover — почти шёпот. "
        "Музыка — кинематографичный ambient или soft piano."
    ),
    "celebrity_style": (
        "Celebrity style: moody low-key. Архетип читается через свет и кадр, "
        "не через имитацию знаменитости. Один medium shot человека, остальное — детали."
    ),
    "problem_solution": (
        "Problem/solution: контраст. Первая половина — тусклее, тише. "
        "Вторая половина — теплее, ярче. Резче cuts на HOOK, чёткий CTA."
    ),
    "luxury_quote": (
        "Luxury quote: цитата на чёрном фоне, очень крупная типографика. "
        "Минимум движения, максимум воздуха. Музыка — soft piano или ambient drone. "
        "Voiceover — медленный, низкий, как шёпот."
    ),
    "date_night": (
        "Date night: тёплая палитра (амбра, медь, тёмное золото). "
        "Свечи, силуэты, шёлк, бар. Captions короткие, ритм медленный. "
        "Музыка — slow R&B / cinematic warm."
    ),
    "office_rich": (
        "Office rich: дневной свет, окно с панорамой, костюм, часы. "
        "Чёткий, собранный визуал. Бергамот / кожа / кедр в нотах. "
        "Музыка — minimal ambient, business-elegant."
    ),
    "quiet_luxury": (
        "Quiet luxury: монохром, белый/бежевый/серый, без логотипов. "
        "Фактуры — лён, шерсть, бумага. Минимум текста, максимум воздуха. "
        "Музыка — почти тишина. Voiceover — один уверенный шёпот."
    ),
    "perfume_for_mood": (
        "Perfume for mood: сцена-настроение под topic. "
        "Один short emotional shot + один продуктовый close-up. "
        "Captions крупные, формулируют состояние одним словом."
    ),
    "ai_ugc_ad": (
        "AI UGC ad: TikTok-native. Хук в первую секунду. "
        "Короткие punchy фразы (3-6 слов на экран). Быстрые cuts. "
        "Поверх scene 2 заменить плейсхолдер на AI-аватар (HeyGen / D-ID) "
        "или живой face cam. Captions CapCut auto. "
        "Музыка — тёплый ambient или slow-mo бит. "
        "Не презентация. Это разговор с камерой."
    ),
}

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _first_sentence(text: str) -> str:
    m = re.match(r"^(.+?[.!?…])(\s|$)", text.strip())
    return m.group(1).strip() if m else text.strip()


def _pick_template(rng: random.Random, templates: List[str], available: dict) -> str:
    """Pick a template whose placeholders are all satisfied by `available`.
    Falls back to placeholder-free templates, then to any template.
    """
    have = {k for k, v in available.items() if v}
    eligible = [t for t in templates if set(_PLACEHOLDER_RE.findall(t)).issubset(have)]
    if not eligible:
        eligible = [t for t in templates if not _PLACEHOLDER_RE.search(t)]
    if not eligible:
        eligible = templates
    return rng.choice(eligible)


def _fill(template: str, **kwargs) -> str:
    out = template
    for k, v in kwargs.items():
        if v is not None:
            out = out.replace("{" + k + "}", str(v))
    return out


def _audience_hashtags(audience: Optional[str]) -> List[str]:
    if not audience:
        return []
    a = audience.lower()
    tags: List[str] = []
    if "мужчин" in a or "men" in a or "парн" in a:
        tags.append("#длямужчин")
    if "женщин" in a or "women" in a or "девуш" in a:
        tags.append("#дляженщин")
    return tags


def _gen_hashtags(fmt: str, audience: Optional[str], product_name: Optional[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    pool = list(_HASHTAG_BASE) + list(_HASHTAGS_BY_FORMAT.get(fmt, [])) + _audience_hashtags(audience)
    if product_name:
        slug = re.sub(r"\s+", "", product_name.strip()).lower()
        if slug:
            pool.append("#" + slug)
    for h in pool:
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out[:12]


def _gen_caption(rng: random.Random, hook: str, script: str, cta: str) -> str:
    first = _first_sentence(script)
    variants = [
        f"{hook}\n\n{first}\n\n{cta}.",
        f"{hook}.\n\n{cta}.",
        f"{hook}\n\nНайди свой запах на TREZZY.",
    ]
    return rng.choice(variants)


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

def make_plan(
    topic: str,
    product_name: Optional[str] = None,
    target_audience: Optional[str] = None,
    style: str = "premium luxury perfume",
    format: str = "single_review",
    seed: Optional[int] = None,
) -> dict:
    """Build a complete TREZZY short-video content plan from a simple topic.

    Returns a dict with: hook, title, script, vibe_tags, cta, caption,
    hashtags, edit_notes, format, topic.
    """
    fmt = format if format in SUPPORTED_FORMATS else "single_review"
    rng = random.Random(seed)

    available = {
        "topic": topic,
        "product_name": product_name,
        "target_audience": target_audience,
        "style": style,
    }

    hook_raw = _pick_template(rng, _HOOKS[fmt], available)
    script_raw = _pick_template(rng, _SCRIPTS[fmt], available)

    hook = _fill(hook_raw, **available).strip()
    script = _fill(script_raw, **available).strip()

    vibe_tags = rng.sample(_VIBE_POOLS[fmt], k=3)
    cta = DEFAULT_CTA
    caption = _gen_caption(rng, hook, script, cta)
    hashtags = _gen_hashtags(fmt, target_audience, product_name)
    edit_notes = _EDIT_NOTES[fmt]

    return {
        "hook": hook,
        "title": "TREZZY",
        "script": script,
        "vibe_tags": vibe_tags,
        "cta": cta,
        "caption": caption,
        "hashtags": hashtags,
        "edit_notes": edit_notes,
        "format": fmt,
        "topic": topic,
    }
