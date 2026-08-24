#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ежедневные Telegram-дайджесты в RSS — 4 канала, 4 отдельных файла.

Что делает скрипт при каждом запуске (workflow — каждые 15 минут):

1. СБОР (всегда). Опрашивает источники всех каналов, находит новые посты,
   которых ещё нет в архиве posts_archive.json, чистит текст от служебного
   мусора Telegram (плейсхолдеры вложений, пересланные сообщения), режет
   рекламу/нежелательные темы по правилам канала и добавляет остальное
   в архив. Архив накопительный — это решает проблему "источник отдаёт
   только последние N постов".

2. СБОРКА ДАЙДЖЕСТОВ (только в окне 00:00–05:59 по Москве). Берёт из
   архива посты за ПРЕДЫДУЩИЙ календарный день, для каждого канала
   отдельно просит LLM (OpenRouter, бесплатно) выдать резюме ОДНИМ
   запросом на весь день. Часть каналов настроена на "объединение" —
   несколько постов об одном событии сводятся в один абзац с
   несколькими ссылками. Если LLM недоступен — алгоритмический фолбэк
   (без объединения), пайплайн не падает из-за внешнего API.
   Каждый канал пишет в СВОЙ файл feed_<канал>.xml — сбой одного канала
   не затрагивает остальные три.

feed_*.xml никогда не перезаписывается "сырыми" данными — сначала пишется
временный файл, проверяется на валидность XML, и только потом заменяет
старый.
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

MSK = ZoneInfo("Europe/Moscow")
UTC = ZoneInfo("UTC")

# --------------------------------------------------------------------------
# ГЛОБАЛЬНАЯ ОЧИСТКА ТЕКСТА — применяется одинаково ко ВСЕМ каналам
# --------------------------------------------------------------------------

# Служебные фразы, которые RSS-мосты добавляют вместо/вокруг вложений.
# Их всегда убираем, а сам текст поста (если он есть рядом) оставляем.
ARTIFACT_PATTERNS = [
    re.compile(r"This media is not supported in your browser\.?\s*VIEW IN TELEGRAM", re.IGNORECASE),
    re.compile(r"(?:Media is too big\.?\s*VIEW IN TELEGRAM\s*){1,}", re.IGNORECASE),
]

# Широкий диапазон эмодзи-символов для удаления В НАЧАЛЕ строки.
LEADING_EMOJI_RE = re.compile(
    r"^(?:[\U0001F000-\U0001FFFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u200d]|\s)+"
)


def strip_leading_emoji(text: str) -> str:
    if not text:
        return text
    stripped = LEADING_EMOJI_RE.sub("", text).strip()
    return stripped if stripped else text.strip()


def is_forwarded_post(text: str) -> bool:
    """Пересланные посты нельзя нормально атрибутировать — исключаем их."""
    head = (text or "").strip()[:80].lower()
    return head.startswith("forwarded from") or "forwarded from" in head


def strip_known_prefixes(text: str, prefixes: list) -> str:
    """Убирает известную подпись/тэглайн канала, если пост начинается с неё."""
    for prefix in prefixes or []:
        pattern = re.compile(r"^\s*" + re.escape(prefix) + r"\s*[:\-—–,.]*\s*", re.IGNORECASE)
        text = pattern.sub("", text, count=1)
    return text.strip()


def clean_text(raw_html: str) -> str:
    text = BeautifulSoup(raw_html or "", "html.parser").get_text(separator=" ", strip=True)
    for pattern in ARTIFACT_PATTERNS:
        text = pattern.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --------------------------------------------------------------------------
# НАСТРОЙКИ КАНАЛОВ
# --------------------------------------------------------------------------

CHANNELS = {
    "bankrollo": {
        "sources": [
            "https://wtf.roflcopter.fr/rss-bridge/?action=display&bridge=Telegram&username=bankrollo&format=Atom",
            "http://tg.i-c-a.su/rss/bankrollo",
        ],
        "filter": None,  # все посты
        "title_suffix": "Bankrollo",
        "feed_file": "feed_bankrollo.xml",
        "combine": False,
        "text_prefix_strip": [],
        "extra_instructions": "",
    },
    "victorstepanych": {
        "sources": [
            "https://wtf.roflcopter.fr/rss-bridge/?action=display&bridge=Telegram&username=victorstepanych&format=Atom",
            "http://tg.i-c-a.su/rss/victorstepanych",
        ],
        "filter": lambda text: (
            bool(text.strip()) and len(text) > 50
            and not any(
                kw in text.lower()
                for kw in [
                    "помогите", "сбор", "реабилитация", "финанс", "пожертвова",
                    "приют", "кормлен", "благотворительность",  # реклама благотворительности
                    "монастыр", "церковь", "храм", "священ", "отец",
                    "крипто", "usdt", "bitcoin", "эфир", "блокчейн", "nft",
                    "карта qore", "kyc", "комиссия", "платёж",
                ]
            )
        ),
        "title_suffix": "Victor Stepanych",
        "feed_file": "feed_victorstepanych.xml",
        "combine": True,  # объединять смежные по смыслу посты в один абзац
        "text_prefix_strip": ["Банки, деньги, два офшора"],
        "extra_instructions": "",
    },
    "naeconomila": {
        "sources": [
            "https://wtf.roflcopter.fr/rss-bridge/?action=display&bridge=Telegram&username=naeconomila&format=Atom",
            "http://tg.i-c-a.su/rss/naeconomila",
        ],
        "filter": lambda text: not any(
            kw in text.lower()
            for kw in [
                "вклад", "вкладов", "откроить карт", "бонус", "сертификат",
                "втб", "халв", "локо", "псб", "газпром", "оператор т2", "барабан",
                "первый заказ",
            ]
        ),
        "title_suffix": "НаЭкономила",
        "feed_file": "feed_naeconomila.xml",
        "combine": False,
        "text_prefix_strip": [],
        "extra_instructions": (
            "Если в посте упоминаются конкретные цифры, суммы, процент, лимиты, "
            "комиссии, тарифы или даты вступления изменений в силу — обязательно "
            "включи их в резюме точными числами из текста поста, не обобщай и не "
            "пропускай их; если для этого не хватает одного предложения, используй "
            "два предложения."
        ),
    },
    "condottieros": {
        "sources": [
            "https://wtf.roflcopter.fr/rss-bridge/?action=display&bridge=Telegram&username=condottieros&format=Atom",
            "http://tg.i-c-a.su/rss/condottieros",
        ],
        "filter": lambda text: (
            len(text) > 100
            and not any(
                phrase in text.lower()
                for phrase in [
                    "добрых", "петух", "дискотек", "вам понравится", "круто сделано",
                    "негр", "жоп", "ебут", "ибица",
                    "частный канал", "лоббистка", "lobbystka_advisory",
                    "проверенной экипировки", "доверяй качеству и репутации",
                    "доставляют подарки дистанционно",
                    "mmorpg", "epsilionwarbot", "рейдами", "pvp", "стартовый набор",
                ]
            )
            and not re.match(r"^(Небо|Морпеха|Кстати|Всем)\s+", text, re.IGNORECASE)
        ),
        "title_suffix": "Condottieros",
        "feed_file": "feed_condottieros.xml",
        "combine": True,  # объединять смежные по смыслу посты в один абзац
        "text_prefix_strip": [],
        "extra_instructions": "",
    },
}

ARCHIVE_PATH = Path("posts_archive.json")

RETENTION_DAYS = 90
ARCHIVE_KEEP_DAYS = 100

DIGEST_WINDOW_START_HOUR = 0
DIGEST_WINDOW_END_HOUR = 6

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


# --------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНОЕ
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S MSK")
    print(f"[{ts}] {msg}", flush=True)


def extract_msgid(link: str):
    if not link:
        return None
    cleaned = link.strip().rstrip("/")
    m = re.search(r"/(\d+)(?:[?#].*)?$", cleaned)
    return int(m.group(1)) if m else None


def format_date_ru(iso_date_str: str) -> str:
    d = date.fromisoformat(iso_date_str)
    return d.strftime("%d.%m.%Y")


def algorithmic_summary(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Новый пост в канале."
    sentences = re.split(r"(?<=[.!?])\s+", text)
    first = sentences[0] if sentences else text
    result = first
    # Если первое предложение короткое или не содержит цифр (часто именно в
    # цифрах — суть новости: суммы, даты, лимиты), добавляем второе — без
    # LLM невозможно понять смысл надёжнее, поэтому лучше перебдеть.
    if len(sentences) > 1:
        has_digit = bool(re.search(r"\d", first))
        if len(first) < 90 or not has_digit:
            result = f"{first} {sentences[1]}".strip()
    if len(result) > 320:
        result = result[:317].rstrip() + "..."
    return strip_leading_emoji(result)


# --------------------------------------------------------------------------
# ЭТАП 1: СБОР ПОСТОВ
# --------------------------------------------------------------------------

def fetch_source(url: str):
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DigestBot/1.0)"},
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            log(f"Источник вернул некорректный фид (bozo) и без записей: {url}")
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
        log(f"Не удалось прочитать {ARCHIVE_PATH}, начинаю с пустого архива: {e}")
        return {}


def prune_archive(archive: dict) -> dict:
    cutoff = (datetime.now(MSK).date() - timedelta(days=ARCHIVE_KEEP_DAYS)).isoformat()
    return {k: v for k, v in archive.items() if v.get("date", "9999-99-99") >= cutoff}


def collect_posts() -> dict:
    archive = load_archive()
    new_count = 0
    skipped_forwarded = 0
    skipped_filtered = 0

    for channel_name, channel_cfg in CHANNELS.items():
        for url in channel_cfg["sources"]:
            parsed = fetch_source(url)
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

                pub_dt = None
                if getattr(entry, "published_parsed", None):
                    pub_dt = datetime(*entry.published_parsed[:6], tzinfo=UTC)
                elif getattr(entry, "updated_parsed", None):
                    pub_dt = datetime(*entry.updated_parsed[:6], tzinfo=UTC)
                else:
                    pub_dt = datetime.now(UTC)

                raw = entry.get("summary", "")
                if not raw and entry.get("content"):
                    raw = entry["content"][0].get("value", "")
                text = clean_text(raw)

                if is_forwarded_post(text):
                    skipped_forwarded += 1
                    continue

                text = strip_leading_emoji(text)
                text = strip_known_prefixes(text, channel_cfg.get("text_prefix_strip", []))
                if not text:
                    text = "(пост без текста, см. оригинал)"

                filter_fn = channel_cfg.get("filter")
                if filter_fn and not filter_fn(text):
                    skipped_filtered += 1
                    continue

                post_date_msk = pub_dt.astimezone(MSK).date().isoformat()

                archive[key] = {
                    "channel": channel_name,
                    "date": post_date_msk,
                    "link": link,
                    "text": text,
                    "msgid": msgid,
                    "ts": pub_dt.astimezone(UTC).isoformat(),
                }
                new_count += 1

    archive = prune_archive(archive)
    ARCHIVE_PATH.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    log(
        f"Собрано новых постов: {new_count}. Пропущено (пересланные): {skipped_forwarded}. "
        f"Пропущено (фильтр канала): {skipped_filtered}. Всего в архиве: {len(archive)}."
    )
    return archive


# --------------------------------------------------------------------------
# ЭТАП 2: РЕЗЮМЕ ЧЕРЕЗ OPENROUTER (ОДИН ЗАПРОС НА ВЕСЬ ДЕНЬ) С ФОЛБЭКОМ
# --------------------------------------------------------------------------

def _call_openrouter_once(model: str, prompt: str):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 3000,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/",
        "X-Title": "Multi-channel Digest Bot",
    }
    resp = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    return resp


def build_prompt(posts: list, combine: bool, extra_instructions: str) -> str:
    numbered = "\n".join(f"{i + 1}. {p['text'][:600]}" for i, p in enumerate(posts))
    base = (
        "Ты помогаешь составить новостной дайджест Telegram-канала. "
        f"Ниже приведены {len(posts)} постов, пронумерованных по порядку. "
    )
    if extra_instructions:
        base += extra_instructions + " "

    if combine:
        base += (
            "Составь резюме БЕЗ эмодзи. Посты, которые говорят об одном и том же "
            "событии или теме, объедини в одно резюме. Посты, "
            "не связанные по смыслу с другими, оформи отдельным резюме. "
            "Каждое резюме — одно предложение, но если новость содержит важные "
            "детали (конкретные цифры, суммы, даты, причины, имена), которые не "
            "поместятся в одно предложение без потери смысла — используй два "
            "предложения. Не жертвуй важными деталями ради краткости. "
            "Верни ТОЛЬКО валидный JSON-массив объектов, без пояснений и "
            'markdown-разметки. Каждый объект вида {"summary": "текст резюме", '
            '"posts": [номера постов от 1 до N, относящихся к этому резюме]}. '
            f"Каждый из {len(posts)} постов должен войти РОВНО в одну группу, "
            "ни один пост не должен быть пропущен или продублирован."
        )
    else:
        base += (
            "Для КАЖДОГО поста напиши резюме на русском языке без эмодзи, "
            "передающее суть новости. По умолчанию — одно предложение, но если "
            "новость содержит важные детали (конкретные цифры, суммы, даты, "
            "причины, имена), которые не поместятся в одно предложение без "
            "потери смысла — используй два предложения. Не жертвуй важными "
            "деталями ради краткости. "
            "Верни ТОЛЬКО валидный JSON-массив строк, без пояснений и "
            f"markdown-разметки, ровно {len(posts)} элементов в том же порядке, "
            "в котором идут посты."
        )
    return base + "\n\nПосты:\n" + numbered


def _parse_flat(content: str, n: int):
    data = json.loads(content)
    if isinstance(data, list) and len(data) == n and all(isinstance(x, str) for x in data):
        return [s.strip() for s in data]
    return None


def _parse_grouped(content: str, n: int):
    data = json.loads(content)
    if not isinstance(data, list):
        return None
    covered = set()
    groups = []
    for item in data:
        if not isinstance(item, dict):
            return None
        summary = item.get("summary")
        idxs_raw = item.get("posts")
        if not isinstance(summary, str) or not isinstance(idxs_raw, list) or not summary.strip():
            return None
        idxs = []
        for x in idxs_raw:
            try:
                idx = int(x)
            except (TypeError, ValueError):
                return None
            idxs.append(idx)
        if not idxs:
            return None
        for i in idxs:
            if i < 1 or i > n or i in covered:
                return None
            covered.add(i)
        groups.append({"summary": summary.strip(), "posts": idxs})
    if covered != set(range(1, n + 1)):
        return None
    return groups


def summarize_batch(posts: list, channel_cfg: dict):
    """Возвращает список строк (обычный режим) или список групп
    {"summary":.., "posts": [...]} (режим combine), либо None при провале."""
    if not OPENROUTER_API_KEY:
        log("OPENROUTER_API_KEY не задан — использую алгоритмическое резюме.")
        return None

    combine = channel_cfg.get("combine", False)
    prompt = build_prompt(posts, combine, channel_cfg.get("extra_instructions", ""))
    parse_fn = _parse_grouped if combine else _parse_flat

    for model in OPENROUTER_MODELS:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = _call_openrouter_once(model, prompt)
            except Exception as e:
                log(f"OpenRouter: ошибка соединения (модель {model}, попытка {attempt}): {e}")
                time.sleep(BACKOFF_BASE ** attempt)
                continue

            if resp.status_code == 404:
                log(f"OpenRouter: модель {model} недоступна (404) — пробую следующую модель.")
                break

            if resp.status_code == 429:
                wait = BACKOFF_BASE ** attempt
                log(f"OpenRouter: лимит запросов (429) для модели {model}, повтор через {wait}s.")
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                log(f"OpenRouter: ошибка {resp.status_code} для модели {model}: {resp.text[:300]}")
                time.sleep(BACKOFF_BASE ** attempt)
                continue

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()
                result = parse_fn(content, len(posts))
                if result is not None:
                    log(f"OpenRouter: резюме успешно получены (модель {model}).")
                    return result
                log(f"OpenRouter: неожиданный/невалидный формат ответа (модель {model}), пробую ещё раз.")
            except Exception as e:
                log(f"OpenRouter: не удалось разобрать ответ (модель {model}): {e}")
            time.sleep(BACKOFF_BASE ** attempt)

    log("OpenRouter: все модели и попытки исчерпаны — использую алгоритмическое резюме.")
    return None


# --------------------------------------------------------------------------
# ЭТАП 3: СБОРКА ДАЙДЖЕСТОВ — ОТДЕЛЬНЫЙ feed_<канал>.xml НА КАЖДЫЙ КАНАЛ
# --------------------------------------------------------------------------

def digest_exists_in_feed(feed_path: Path, guid: str) -> bool:
    if not feed_path.exists():
        return False
    parsed = feedparser.parse(str(feed_path))
    return any(e.get("id") == guid or e.get("guid") == guid for e in parsed.entries)


def format_item_description(posts: list, summaries: list) -> str:
    parts = []
    for post, summary in zip(posts, summaries):
        link_escaped = html.escape(post["link"], quote=True)
        summary_escaped = html.escape(strip_leading_emoji(summary))
        parts.append(f'{summary_escaped} <a href="{link_escaped}">Ссылка</a>')
    return "<br/><br/>".join(parts)


def format_item_description_grouped(posts: list, groups: list) -> str:
    parts = []
    for group in groups:
        summary_escaped = html.escape(strip_leading_emoji(group["summary"]))
        links_html = " ".join(
            f'<a href="{html.escape(posts[idx - 1]["link"], quote=True)}">Ссылка</a>'
            for idx in group["posts"]
        )
        parts.append(f"{summary_escaped} {links_html}")
    return "<br/><br/>".join(parts)


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


def rebuild_feed(existing_items: list, new_item: dict, channel_name: str, title_suffix: str) -> FeedGenerator:
    fg = FeedGenerator()
    fg.title(f"{title_suffix} — ежедневный дайджест")
    fg.link(href=f"https://t.me/{channel_name}", rel="alternate")
    fg.description(f"Автоматический ежедневный дайджест Telegram-канала {title_suffix}")
    fg.language("ru")

    all_items = list(existing_items)
    if new_item:
        all_items.append(new_item)

    seen = set()
    unique_items = []
    for it in all_items:
        if it["guid"] in seen:
            continue
        seen.add(it["guid"])
        unique_items.append(it)
    unique_items.sort(key=lambda x: x["pub_dt"], reverse=True)

    for it in unique_items:
        fe = fg.add_entry()
        fe.id(it["guid"])
        fe.guid(it["guid"], permalink=False)
        fe.title(it["title"])
        fe.link(href=it["link"])
        fe.description(it["description"])
        fe.pubDate(it["pub_dt"])

    return fg


def write_feed_atomically(fg: FeedGenerator, feed_path: Path) -> None:
    tmp_path = feed_path.with_suffix(".tmp")
    fg.rss_file(str(tmp_path), pretty=True)
    try:
        ET.parse(tmp_path)
    except ET.ParseError as e:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Сгенерированный {feed_path} не прошёл проверку XML: {e}") from e
    tmp_path.replace(feed_path)


def try_build_digest(archive: dict) -> None:
    now_msk = datetime.now(MSK)

    if not (DIGEST_WINDOW_START_HOUR <= now_msk.hour < DIGEST_WINDOW_END_HOUR):
        log("Сейчас не окно сборки дайджеста (00:00–05:59 MSK) — только сбор постов на этом запуске.")
        return

    target_date = (now_msk.date() - timedelta(days=1)).isoformat()
    cutoff_date = now_msk.date() - timedelta(days=RETENTION_DAYS)

    any_built = False

    for channel_name, channel_cfg in CHANNELS.items():
        feed_path = Path(channel_cfg["feed_file"])
        guid = f"digest-{channel_name}-{target_date}"

        if digest_exists_in_feed(feed_path, guid):
            log(f"[{channel_name}] Дайджест за {target_date} уже есть в {feed_path} — пропускаю.")
            continue

        posts_today = [
            v for v in archive.values()
            if v.get("date") == target_date and v.get("channel") == channel_name
        ]
        if not posts_today:
            log(f"[{channel_name}] Нет постов за {target_date} — {feed_path} не трогаю.")
            continue

        posts_today.sort(key=lambda p: (p["msgid"] if p["msgid"] is not None else 0, p["ts"]))
        log(f"[{channel_name}] Собираю дайджест за {target_date}: {len(posts_today)} посто(в).")

        try:
            result = summarize_batch(posts_today, channel_cfg)
        except Exception as e:
            log(f"[{channel_name}] Непредвиденная ошибка при вызове LLM: {e}. Использую алгоритмическое резюме.")
            result = None

        combine = channel_cfg.get("combine", False)
        if result is not None and combine:
            description = format_item_description_grouped(posts_today, result)
        elif result is not None:
            description = format_item_description(posts_today, result)
        else:
            summaries = [algorithmic_summary(p["text"]) for p in posts_today]
            description = format_item_description(posts_today, summaries)

        title = f"{channel_cfg['title_suffix']} — новости за {format_date_ru(target_date)}"

        new_item = {
            "guid": guid,
            "title": title,
            "link": f"https://t.me/{channel_name}",
            "description": description,
            "pub_dt": datetime.combine(date.fromisoformat(target_date), dt_time(23, 59, 0), tzinfo=MSK),
        }

        try:
            existing_items = load_existing_items(feed_path, cutoff_date, f"https://t.me/{channel_name}")
            fg = rebuild_feed(existing_items, new_item, channel_name, channel_cfg["title_suffix"])
            write_feed_atomically(fg, feed_path)
            log(f"[{channel_name}] {feed_path} обновлён: добавлена запись «{title}».")
            any_built = True
        except Exception as e:
            log(f"[{channel_name}] ОШИБКА при записи {feed_path}: {e}. Файл НЕ изменён.")
            continue

    if not any_built:
        log("На этом запуске ни один дайджест не был добавлен.")


# --------------------------------------------------------------------------
# ТОЧКА ВХОДА
# --------------------------------------------------------------------------

def main() -> int:
    log("Запуск: сбор постов…")
    archive = collect_posts()
    log("Проверка, не пора ли собрать дайджесты за прошедший день…")
    try_build_digest(archive)
    log("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
