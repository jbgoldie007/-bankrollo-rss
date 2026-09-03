#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ежедневные дайджесты Telegram-каналов в RSS. Версия "начисто".

Что делает:
1. Каждые 15 минут собирает новые посты 4 каналов через rss-bridge
   в накопительный архив posts_archive.json.
2. Ночью (00:00–05:59 МСК) собирает по одному RSS-item за прошедший
   день на каждый канал: одна новость = одно предложение с эмодзи
   по смыслу в начале, без указания источника ("пишет РБК" и т.п.).
3. Полный текст каждого поста сохраняется в HTML-страницу
   posts/<канал>/<дата>.html — "Ссылка" в дайджесте ведёт на неё
   (открывается без VPN), а уже с этой страницы при желании можно
   перейти в Telegram.
4. Всё старше 30 дней удаляется автоматически: записи в лентах,
   архив, HTML-страницы. Никакого вечного архива.

Фильтрации контента нет — в дайджест попадают все посты канала.
Убираются только технические артефакты: пустые посты (одно медиа без
текста), заглушки "This media is not supported...", пересланные посты
без собственного текста и точные дубли.
"""

import html
import json
import re
import sys
import time
import os
from datetime import datetime, timedelta, date, time as dt_time
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

# --------------------------------------------------------------------------
# НАСТРОЙКИ
# --------------------------------------------------------------------------

# ПОМЕНЯЙТЕ, если у вас другой логин или имя репозитория!
# Это адрес GitHub Pages вашего репозитория. Как включить Pages — см.
# инструкцию, которую прислал Claude вместе с этим файлом.
PUBLIC_BASE_URL = "https://jbgoldie007.github.io/-bankrollo-rss"

CHANNELS = {
    "bankrollo": {
        "source": "https://wtf.roflcopter.fr/rss-bridge/?action=display&bridge=Telegram&username=bankrollo&format=Atom",
        "title": "Bankrollo",
        "feed_file": "feed_bankrollo.xml",
        # Правила исключения — читает LLM, формулируйте по-человечески.
        "exclude_rules": (
            "лотереи и кто что выиграл; "
            "завуалированная реклама банков и корпоративный PR финансовых организаций "
            "(ВТБ, Сбер, РСХБ, Альфа, Т-Банк и т.п.: их проекты, рейтинги, объёмы переводов, "
            "участие в форумах, заявления их топ-менеджеров); "
            "прямая реклама продуктов и сервисов (Яндекс Пэй, Альфа-Капитал и т.п.); "
            "юмор, шутки, «жёлтые» и скабрёзные посты; "
            "курсы валют (доллар, евро, юань, риал и любые другие), если это не скачок более чем на 30%; "
            "бытовая криминальная хроника (убил, застрелил, задержали обычного человека), "
            "если это не резонансное событие и не известная личность; "
            "дешёвые авиабилеты и куда улететь задёшево; "
            "сравнение стоимости жилья в разных городах или странах; "
            "статистика Росстата о зарплатах"
        ),
        # Подстраховка по ключевым словам (регулярные выражения, без учёта регистра).
        "exclude_keywords": [
            r"лотере", r"джекпот", r"росстат",
            r"курс (доллара|евро|юаня|рубля)", r"авиабилет",
        ],
        "exclude_raw_html": [],
    },
    "victorstepanych": {
        "source": "https://wtf.roflcopter.fr/rss-bridge/?action=display&bridge=Telegram&username=victorstepanych&format=Atom",
        "title": "Victor Stepanych",
        "feed_file": "feed_victorstepanych.xml",
        "exclude_rules": (
            "просьбы о помощи и сборы денег — для животных, людей, приютов, храмов, монастырей"
        ),
        "exclude_keywords": [
            r"помог(и|ите)\b", r"нужна помощь", r"сбор средств", r"реквизит",
            r"пожертв", r"приют", r"благотворит",
        ],
        "exclude_raw_html": [],
    },
    "naeconomila": {
        "source": "https://wtf.roflcopter.fr/rss-bridge/?action=display&bridge=Telegram&username=naeconomila&format=Atom",
        "title": "НаЭкономила",
        "feed_file": "feed_naeconomila.xml",
        "exclude_rules": (
            "сертификаты; бесплатные дебетовые карты; бонусы или кэшбэк за оформление кредитной карты; "
            "банк повысил или изменил ставку по накопительному счёту или вкладу; подарочные карты"
        ),
        "exclude_keywords": [
            r"сертификат", r"дебетов", r"кредитн\w* карт", r"накопительн",
            r"\bвклад", r"подарочн\w* карт",
        ],
        "exclude_raw_html": [],
    },
    "condottieros": {
        "source": "https://wtf.roflcopter.fr/rss-bridge/?action=display&bridge=Telegram&username=condottieros&format=Atom",
        "title": "Condottieros",
        "feed_file": "feed_condottieros.xml",
        "exclude_rules": (
            "посты, к которым прикреплён музыкальный файл (песня, трек), и посты, посвящённые песне или музыке"
        ),
        "exclude_keywords": [
            r"file attachments?.*\.(mp3|m4a|ogg|flac|wav)",
        ],
        # Проверяется по сырому HTML поста до очистки (там видны вложения).
        "exclude_raw_html": [
            r"<audio", r"\.(mp3|m4a|ogg|flac|wav)\b",
        ],
    },
}

ARCHIVE_PATH = Path("posts_archive.json")
POSTS_DIR = Path("posts")

KEEP_DAYS = 30            # хранить записи лент, страницы постов и архив 30 дней

DIGEST_WINDOW_START_HOUR = 0
DIGEST_WINDOW_END_HOUR = 12

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS = [
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-20b:free",
]

REQUEST_TIMEOUT = 25
MAX_RETRIES = 3
BACKOFF_BASE = 2

MSK = ZoneInfo("Europe/Moscow")
UTC = ZoneInfo("UTC")

# --------------------------------------------------------------------------
# ЧИСТКА ТЕКСТА (только технический мусор, не контент)
# --------------------------------------------------------------------------

ARTIFACT_PATTERNS = [
    re.compile(r"This media is not supported in your browser\.?\s*", re.IGNORECASE),
    re.compile(r"Media is too big\.?\s*", re.IGNORECASE),
    re.compile(r"VIEW IN TELEGRAM\.?\s*", re.IGNORECASE),
]

FORWARDED_RE = re.compile(r"^\s*forwarded\s+from\b", re.IGNORECASE)

GLOBAL_PREFIXES_TO_STRIP = ["Банки, деньги, два офшора"]

KNOWN_SOURCE_NAMES = [
    "РБК", "Известия", "Ведомости", "Baza", "Mash", "Shot", "Readovka",
    "РИА Новости", "РИА", "ТАСС", "Интерфакс", "Коммерсантъ", "Коммерсант",
    "The Times of India", "Bloomberg", "Reuters", "BBC", "Forbes",
    "The New York Times", "Financial Times", "112",
]
_sources_escaped = "|".join(
    re.escape(s) for s in sorted(KNOWN_SOURCE_NAMES, key=len, reverse=True)
)
SOURCE_DASH_SUFFIX_RE = re.compile(
    rf"\s*[—–-]\s*(?:{_sources_escaped})\.?\s*$", re.IGNORECASE
)
SOURCE_VERB_SUFFIX_RE = re.compile(
    r"[,;]?\s*(?:пишет|пишут|сообщает|сообщают|передаёт|передают|уточняет|уточняют|"
    r"информирует|информируют)\s+[A-ZА-ЯЁ][\w]*(?:\s+[\wА-Яа-яЁё]+){0,4}\.?\s*$",
    re.UNICODE,
)

CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")


def log(msg: str) -> None:
    ts = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S MSK")
    print(f"[{ts}] {msg}", flush=True)


def extract_msgid(link: str):
    if not link:
        return None
    m = re.search(r"/(\d+)(?:[?#].*)?$", link.strip().rstrip("/"))
    return int(m.group(1)) if m else None


def strip_source_attribution(text: str) -> str:
    if not text:
        return text
    for _ in range(2):
        before = text
        text = SOURCE_DASH_SUFFIX_RE.sub("", text)
        text = SOURCE_VERB_SUFFIX_RE.sub("", text)
        text = text.strip()
        if text == before:
            break
    return text


def strip_known_prefixes(text: str) -> str:
    for prefix in GLOBAL_PREFIXES_TO_STRIP:
        pattern = r"^\s*" + re.escape(prefix) + r"\s*[:\-–—,]*\s*"
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip()


def text_fingerprint(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()[:300]


def prepare_text(raw_html: str):
    """Единая чистка текста поста. Возвращает текст либо None,
    если реального текста в посте нет (его нужно пропустить)."""
    text = BeautifulSoup(raw_html or "", "html.parser").get_text(separator=" ", strip=True)
    if FORWARDED_RE.search(text):
        return None
    for pattern in ARTIFACT_PATTERNS:
        text = pattern.sub("", text)
    text = strip_known_prefixes(text)
    text = strip_source_attribution(text)
    text = re.sub(r"\s+", " ", text).strip()
    if "пост без текста" in text.lower():
        return None
    if len(text) < 10:
        return None
    return text


def _looks_russian(texts: list) -> bool:
    non_empty = [t for t in texts if t and t.strip()]
    if not non_empty:
        return True
    with_cyrillic = sum(1 for t in non_empty if CYRILLIC_RE.search(t))
    return with_cyrillic >= max(1, round(len(non_empty) * 0.7))


# ---- Фильтрация по ключевым словам (подстраховка, работает и без LLM) ----

def keyword_excluded(text: str, raw_html: str, cfg: dict) -> bool:
    for pat in cfg.get("exclude_keywords", []):
        if re.search(pat, text, re.IGNORECASE):
            return True
    if raw_html:
        for pat in cfg.get("exclude_raw_html", []):
            if re.search(pat, raw_html, re.IGNORECASE):
                return True
    return False


# ---- Нечёткие дубли: одинаковая новость с чуть другой формулировкой ----

_WORD_RE = re.compile(r"[а-яёa-z0-9]{4,}", re.IGNORECASE)


def word_set(text: str) -> set:
    # Берём первые 5 букв слова как грубую "основу": концерта/концерт, отмены/отменят
    # считаются одним словом — иначе русские окончания ломают сравнение.
    return set(w.lower()[:5] for w in _WORD_RE.findall(text or ""))


def is_near_duplicate(text: str, kept_word_sets: list, threshold: float = 0.55) -> bool:
    ws = word_set(text)
    if len(ws) < 4:
        return False
    for other in kept_word_sets:
        if not other:
            continue
        inter = len(ws & other)
        union = len(ws | other)
        if union and inter / union >= threshold:
            return True
    return False


# ---- Эмодзи в начале строки гарантированно ----

_EMOJI_START_RE = re.compile(
    "^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\U0001F1E6-\U0001F1FF"
    "\u2190-\u21FF\u2300-\u23FF\u25A0-\u25FF]"
)


def ensure_emoji(summary: str) -> str:
    s = (summary or "").strip()
    if not s:
        return s
    if _EMOJI_START_RE.match(s):
        return s
    return "📌 " + s


SKIP_MARK = "SKIP"


def is_skip(value) -> bool:
    return isinstance(value, str) and re.sub(r"[^A-Za-z]", "", value).upper() == SKIP_MARK


# --------------------------------------------------------------------------
# СБОР ПОСТОВ
# --------------------------------------------------------------------------

def fetch_source(url: str):
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DigestBot/2.0)"},
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            log(f"Источник вернул некорректный фид: {url}")
            return None
        return parsed
    except Exception as e:
        log(f"Не удалось получить источник {url}: {e}")
        return None


def load_archive() -> dict:
    if not ARCHIVE_PATH.exists():
        return {}
    try:
        return json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"Не удалось прочитать архив, начинаю с пустого: {e}")
        return {}


def prune_archive(archive: dict) -> dict:
    cutoff = (datetime.now(MSK).date() - timedelta(days=KEEP_DAYS + 1)).isoformat()
    return {k: v for k, v in archive.items() if v.get("date", "9999") >= cutoff}


def collect_posts() -> dict:
    archive = load_archive()
    new_count = 0
    skipped = 0

    seen_fp = {}
    for v in archive.values():
        seen_fp.setdefault(v.get("channel"), set()).add(text_fingerprint(v.get("text", "")))

    for channel_name, cfg in CHANNELS.items():
        parsed = fetch_source(cfg["source"])
        if not parsed or not getattr(parsed, "entries", None):
            continue

        for entry in parsed.entries:
            link = (entry.get("link") or "").strip()
            if not link:
                continue

            msgid = extract_msgid(link)
            key = f"{channel_name}:{msgid}" if msgid is not None else f"{channel_name}:{link}"
            if key in archive:
                continue

            if getattr(entry, "published_parsed", None):
                pub_dt = datetime(*entry.published_parsed[:6], tzinfo=UTC)
            elif getattr(entry, "updated_parsed", None):
                pub_dt = datetime(*entry.updated_parsed[:6], tzinfo=UTC)
            else:
                pub_dt = datetime.now(UTC)

            raw = entry.get("summary", "")
            if not raw and entry.get("content"):
                raw = entry["content"][0].get("value", "")

            text = prepare_text(raw)
            if text is None:
                skipped += 1
                continue

            if keyword_excluded(text, raw, cfg):
                skipped += 1
                continue

            fp = text_fingerprint(text)
            fps = seen_fp.setdefault(channel_name, set())
            if fp in fps:
                skipped += 1
                continue

            archive[key] = {
                "channel": channel_name,
                "date": pub_dt.astimezone(MSK).date().isoformat(),
                "link": link,
                "text": text,
                "msgid": msgid,
                "ts": pub_dt.astimezone(UTC).isoformat(),
            }
            fps.add(fp)
            new_count += 1

    archive = prune_archive(archive)
    ARCHIVE_PATH.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Новых постов: {new_count}. Пропущено (мусор/дубли): {skipped}. В архиве: {len(archive)}.")
    return archive


# --------------------------------------------------------------------------
# РЕЗЮМЕ: ОДНО ПРЕДЛОЖЕНИЕ + ЭМОДЗИ, ОДИН ЗАПРОС НА КАНАЛ В ДЕНЬ
# --------------------------------------------------------------------------

def algorithmic_summary(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Новый пост в канале."
    sentences = re.split(r"(?<=[.!?])\s+", text)
    first = sentences[0] if sentences else text
    if len(first) > 200:
        first = first[:197].rstrip() + "..."
    return strip_source_attribution(first)


CHUNK_SIZE = 12  # постов в одном запросе к LLM — меньше порция, надёжнее ответ


def build_prompt(posts: list, exclude_rules: str) -> str:
    numbered = "\n".join(f"{i + 1}. {p['text'][:600]}" for i, p in enumerate(posts))
    rules_block = ""
    if exclude_rules:
        rules_block = (
            "ПРАВИЛА ИСКЛЮЧЕНИЯ для этого канала — если пост подходит хотя бы под одно, "
            f"вместо резюме верни строку \"{SKIP_MARK}\": {exclude_rules}.\n"
        )
    return (
        "Ты составляешь новостной дайджест Telegram-канала. "
        f"Ниже {len(posts)} постов, пронумерованных по порядку.\n"
        + rules_block +
        f"Также верни \"{SKIP_MARK}\", если пост по смыслу повторяет более ранний пост из этого списка "
        "(та же новость другими словами).\n"
        "Для каждого остального поста напиши РОВНО ОДНО короткое предложение на русском "
        "языке, передающее главное. Сам выдели суть — коротко и понятно. "
        "В начало каждого предложения ОБЯЗАТЕЛЬНО поставь один подходящий по смыслу эмодзи. "
        "Если исходный пост на другом языке — переведи суть на русский. "
        "НЕ указывай источник новости (никаких 'пишет РБК', 'сообщает Baza', "
        "'— Известия' и т.п.) — только само событие.\n"
        "Верни ТОЛЬКО валидный JSON-массив строк без пояснений и markdown, "
        f"ровно {len(posts)} элементов, в том же порядке.\n\n"
        "Посты:\n" + numbered
    )


def _call_openrouter(model: str, prompt: str):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 4000,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/",
        "X-Title": "Telegram Digest Bot",
    }
    return requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT)


def _summarize_chunk(posts: list, exclude_rules: str):
    """Один запрос к LLM на порцию постов. Возвращает список той же длины:
    строка-резюме либо None (пост нужно исключить). При провале — None."""
    prompt = build_prompt(posts, exclude_rules)

    for model in OPENROUTER_MODELS:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = _call_openrouter(model, prompt)
            except Exception as e:
                log(f"OpenRouter: ошибка соединения ({model}, попытка {attempt}): {e}")
                time.sleep(BACKOFF_BASE ** attempt)
                continue

            if resp.status_code == 404:
                log(f"OpenRouter: модель {model} недоступна (404) — следующая модель.")
                break
            if resp.status_code == 429:
                wait = BACKOFF_BASE ** attempt
                log(f"OpenRouter: 429 для {model}, повтор через {wait}s.")
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                log(f"OpenRouter: ошибка {resp.status_code} для {model}: {resp.text[:200]}")
                time.sleep(BACKOFF_BASE ** attempt)
                continue

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()
                items = json.loads(content)
                if not (
                    isinstance(items, list)
                    and len(items) == len(posts)
                    and all(isinstance(s, str) for s in items)
                ):
                    log(f"OpenRouter: неожиданный формат ({model}), пробую ещё.")
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue

                result = []
                for s in items:
                    if is_skip(s):
                        result.append(None)
                    else:
                        result.append(strip_source_attribution(s.strip()))

                kept = [s for s in result if s]
                # Если модель "пропустила" вообще всё — это подозрительно, считаем ответ битым.
                if not kept and len(posts) >= 3:
                    log(f"OpenRouter: модель {model} исключила все посты — не верю, пробую ещё.")
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue
                if kept and not _looks_russian(kept):
                    log(f"OpenRouter: ответ не на русском ({model}), пробую ещё.")
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue

                log(f"OpenRouter: порция из {len(posts)} постов готова (модель {model}), "
                    f"исключено по правилам: {len(posts) - len(kept)}.")
                return result
            except Exception as e:
                log(f"OpenRouter: не удалось разобрать ответ ({model}): {e}")
            time.sleep(BACKOFF_BASE ** attempt)

    return None


def summarize_batch(posts: list, exclude_rules: str):
    """Резюме для всех постов канала за день. Возвращает список той же длины,
    где элемент — строка-резюме либо None (исключить). Если LLM недоступен
    хотя бы для одной порции — None целиком (сработает алгоритмический фолбэк)."""
    if not OPENROUTER_API_KEY:
        log("OPENROUTER_API_KEY не задан — использую алгоритмическое резюме.")
        return None

    result = []
    for i in range(0, len(posts), CHUNK_SIZE):
        chunk = posts[i:i + CHUNK_SIZE]
        chunk_result = _summarize_chunk(chunk, exclude_rules)
        if chunk_result is None:
            log("OpenRouter недоступен — использую алгоритмическое резюме.")
            return None
        result.extend(chunk_result)
    return result


# --------------------------------------------------------------------------
# HTML-СТРАНИЦЫ С ПОЛНЫМ ТЕКСТОМ ПОСТОВ (чтение без VPN)
# --------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; max-width: 720px;
       margin: 0 auto; padding: 16px; line-height: 1.55; color: #1a1a1a; background: #fafafa; }}
h1 {{ font-size: 1.25rem; }}
article {{ background: #fff; border: 1px solid #e5e5e5; border-radius: 10px;
           padding: 14px 16px; margin: 14px 0; }}
article:target {{ border-color: #4a76c9; box-shadow: 0 0 0 2px #4a76c955; }}
.meta {{ font-size: 0.82rem; color: #777; margin-bottom: 6px; }}
.meta a {{ color: #4a76c9; text-decoration: none; }}
p {{ margin: 0; white-space: pre-wrap; }}
</style>
</head>
<body>
<h1>{title}</h1>
{articles}
</body>
</html>
"""

ARTICLE_TEMPLATE = """<article id="p{msgid}">
<div class="meta">{time_str} · <a href="{tg_link}" rel="noopener">Открыть в Telegram</a></div>
<p>{text}</p>
</article>"""


def write_day_page(channel_name: str, channel_title: str, target_date: str, posts: list) -> None:
    day_dir = POSTS_DIR / channel_name
    day_dir.mkdir(parents=True, exist_ok=True)
    articles = []
    for p in posts:
        try:
            t = datetime.fromisoformat(p["ts"]).astimezone(MSK).strftime("%H:%M")
        except Exception:
            t = ""
        articles.append(ARTICLE_TEMPLATE.format(
            msgid=p["msgid"] if p["msgid"] is not None else "x",
            time_str=t,
            tg_link=html.escape(p["link"], quote=True),
            text=html.escape(p["text"]),
        ))
    page = PAGE_TEMPLATE.format(
        title=f"{channel_title} — посты за {format_date_ru(target_date)}",
        articles="\n".join(articles),
    )
    (day_dir / f"{target_date}.html").write_text(page, encoding="utf-8")


def prune_old_pages() -> None:
    if not POSTS_DIR.exists():
        return
    cutoff = datetime.now(MSK).date() - timedelta(days=KEEP_DAYS)
    removed = 0
    for f in POSTS_DIR.glob("*/*.html"):
        try:
            page_date = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if page_date < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    if removed:
        log(f"Удалено старых страниц постов: {removed}.")


def page_url(channel_name: str, target_date: str, msgid) -> str:
    base = PUBLIC_BASE_URL.rstrip("/")
    anchor = f"#p{msgid}" if msgid is not None else ""
    return f"{base}/posts/{channel_name}/{target_date}.html{anchor}"


# --------------------------------------------------------------------------
# СБОРКА RSS
# --------------------------------------------------------------------------

def format_date_ru(iso_date_str: str) -> str:
    return date.fromisoformat(iso_date_str).strftime("%d.%m.%Y")


def digest_exists_in_feed(feed_path: Path, guid: str) -> bool:
    if not feed_path.exists():
        return False
    parsed = feedparser.parse(str(feed_path))
    return any(e.get("id") == guid or e.get("guid") == guid for e in parsed.entries)


def load_existing_items(feed_path: Path, cutoff_date: date, default_link: str) -> list:
    items = []
    if not feed_path.exists():
        return items
    parsed = feedparser.parse(str(feed_path))
    for e in parsed.entries:
        guid = e.get("id") or e.get("guid") or e.get("link")
        pub_dt = None
        if getattr(e, "published_parsed", None):
            pub_dt = datetime(*e.published_parsed[:6], tzinfo=UTC)
        elif getattr(e, "updated_parsed", None):
            pub_dt = datetime(*e.updated_parsed[:6], tzinfo=UTC)
        if pub_dt is None or pub_dt.date() < cutoff_date:
            continue
        items.append({
            "guid": guid,
            "title": e.get("title", ""),
            "link": e.get("link", default_link),
            "description": e.get("description", e.get("summary", "")),
            "pub_dt": pub_dt,
        })
    return items


def rebuild_feed(existing_items: list, new_item: dict, channel_name: str, channel_title: str) -> FeedGenerator:
    fg = FeedGenerator()
    fg.title(f"{channel_title} — ежедневный дайджест")
    fg.link(href=f"https://t.me/{channel_name}", rel="alternate")
    fg.description(f"Автоматический ежедневный дайджест Telegram-канала {channel_title}")
    fg.language("ru")

    all_items = list(existing_items)
    if new_item:
        all_items.append(new_item)

    seen = set()
    unique = []
    for it in all_items:
        if it["guid"] in seen:
            continue
        seen.add(it["guid"])
        unique.append(it)
    unique.sort(key=lambda x: x["pub_dt"], reverse=True)

    for it in unique:
        fe = fg.add_entry()
        fe.id(it["guid"])
        fe.guid(it["guid"], permalink=False)
        fe.title(it["title"])
        fe.link(href=it["link"])
        fe.description(it["description"])
        fe.pubDate(it["pub_dt"])
    return fg


def write_feed_atomically(fg: FeedGenerator, feed_path: Path) -> None:
    tmp = feed_path.with_suffix(".tmp")
    fg.rss_file(str(tmp), pretty=True)
    try:
        ET.parse(tmp)
    except ET.ParseError as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{feed_path}: сгенерированный XML не прошёл проверку: {e}") from e
    tmp.replace(feed_path)


def try_build_digest(archive: dict) -> None:
    now_msk = datetime.now(MSK)

    if not (DIGEST_WINDOW_START_HOUR <= now_msk.hour < DIGEST_WINDOW_END_HOUR):
        log("Не окно сборки дайджеста (00:00–11:59 MSK) — только сбор постов.")
        return

    target_date = (now_msk.date() - timedelta(days=1)).isoformat()
    cutoff_date = now_msk.date() - timedelta(days=KEEP_DAYS)

    for channel_name, cfg in CHANNELS.items():
        feed_path = Path(cfg["feed_file"])
        guid = f"digest-{channel_name}-{target_date}"

        if digest_exists_in_feed(feed_path, guid):
            log(f"[{channel_name}] Дайджест за {target_date} уже есть — пропускаю.")
            continue

        posts_raw = [
            v for v in archive.values()
            if v.get("date") == target_date and v.get("channel") == channel_name
        ]

        # Повторная чистка на случай записей, собранных старыми версиями скрипта:
        # технический мусор, ключевые слова канала, точные и нечёткие дубли.
        posts_raw.sort(key=lambda p: (p["msgid"] if p["msgid"] is not None else 0, p["ts"]))
        posts, seen_fp, kept_words = [], set(), []
        dropped_kw = dropped_dup = 0
        for p in posts_raw:
            text = prepare_text(p.get("text", ""))
            if text is None:
                continue
            if keyword_excluded(text, "", cfg):
                dropped_kw += 1
                continue
            fp = text_fingerprint(text)
            if fp in seen_fp or is_near_duplicate(text, kept_words):
                dropped_dup += 1
                continue
            seen_fp.add(fp)
            kept_words.append(word_set(text))
            p2 = dict(p)
            p2["text"] = text
            posts.append(p2)

        if dropped_kw or dropped_dup:
            log(f"[{channel_name}] Исключено по ключевым словам: {dropped_kw}, дублей: {dropped_dup}.")

        if not posts:
            log(f"[{channel_name}] Нет постов за {target_date} — {feed_path} не трогаю.")
            continue

        log(f"[{channel_name}] Дайджест за {target_date}: {len(posts)} посто(в) на входе.")

        summaries = summarize_batch(posts, cfg.get("exclude_rules", ""))
        if summaries is None:
            summaries = [algorithmic_summary(p["text"]) for p in posts]

        # Оставляем только посты, которые LLM не исключил по правилам канала.
        kept_pairs = [(p, s) for p, s in zip(posts, summaries) if s]
        if not kept_pairs:
            log(f"[{channel_name}] После фильтрации не осталось постов за {target_date} — {feed_path} не трогаю.")
            continue
        posts = [p for p, _ in kept_pairs]
        summaries = [ensure_emoji(s) for _, s in kept_pairs]
        log(f"[{channel_name}] В дайджест попадает: {len(posts)} посто(в).")

        # Страница с полными текстами — на неё ведут ссылки из дайджеста.
        try:
            write_day_page(channel_name, cfg["title"], target_date, posts)
        except Exception as e:
            log(f"[{channel_name}] Не удалось записать страницу постов: {e}")

        parts = []
        for p, s in zip(posts, summaries):
            url = page_url(channel_name, target_date, p["msgid"]) if PUBLIC_BASE_URL else p["link"]
            parts.append(f'{html.escape(s)} <a href="{html.escape(url, quote=True)}">Ссылка</a>')
        description = "<br/><br/>".join(parts)

        new_item = {
            "guid": guid,
            "title": f"{cfg['title']} — новости за {format_date_ru(target_date)}",
            "link": f"https://t.me/{channel_name}",
            "description": description,
            "pub_dt": datetime.combine(date.fromisoformat(target_date), dt_time(23, 59, 0), tzinfo=MSK),
        }

        try:
            existing = load_existing_items(feed_path, cutoff_date, f"https://t.me/{channel_name}")
            fg = rebuild_feed(existing, new_item, channel_name, cfg["title"])
            write_feed_atomically(fg, feed_path)
            log(f"[{channel_name}] {feed_path} обновлён.")
        except Exception as e:
            log(f"[{channel_name}] ОШИБКА записи {feed_path}: {e}. Файл не изменён.")
            continue

    prune_old_pages()


# --------------------------------------------------------------------------
# ТОЧКА ВХОДА
# --------------------------------------------------------------------------

def main() -> int:
    log("Сбор постов…")
    archive = collect_posts()
    try_build_digest(archive)
    log("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
