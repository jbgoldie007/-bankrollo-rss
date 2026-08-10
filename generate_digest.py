#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bankrollo — ежедневный Telegram-дайджест в RSS.

Что делает скрипт при каждом запуске (workflow запускает его каждые 15 минут):

1. СБОР (всегда). Опрашивает два источника Telegram-фида канала, находит
   посты, которых ещё нет в локальном архиве posts_archive.json, и
   добавляет их туда. Это накопительное хранилище — именно оно решает
   проблему "источник отдаёт только последние N постов": даже если в
   какой-то момент источник урезан, мы уже подобрали более старые посты
   на предыдущих опросах.

2. СБОРКА ДАЙДЖЕСТА (только в окне 00:00–05:59 по Москве). Берёт из
   архива все посты за ПРЕДЫДУЩИЙ календарный день (по Москве), просит
   LLM (OpenRouter, бесплатно) выдать по одному предложению-резюме на пост
   ОДНИМ запросом на весь день (не по одному запросу на пост — это и
   было причиной 429). Если LLM недоступен — использует алгоритмическое
   резюме (первое предложение поста), пайплайн никогда не падает из-за
   внешнего API. Результат добавляется как один <item> в feed.xml.
   Если запись за эту дату уже есть — ничего не делает (защита от
   дублей при повторных запусках).

feed.xml никогда не перезаписывается "сырыми" данными — новая версия
сначала пишется во временный файл и проверяется на валидность XML,
и только после этого заменяет старый файл.
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
# НАСТРОЙКИ — при необходимости можно менять
# --------------------------------------------------------------------------

CHANNELS = {
    "bankrollo": {
        "sources": [
            "https://wtf.roflcopter.fr/rss-bridge/?action=display&bridge=Telegram&username=bankrollo&format=Atom",
            "http://tg.i-c-a.su/rss/bankrollo",
        ],
        "filter": None,  # все посты
        "title_suffix": "Bankrollo",
        "combine_posts": False,
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
                    "помогите", "сбор", "реабилитация", "финанс", "пожертвова",  # благотворительность
                    "монастыр", "церковь", "храм", "священ", "отец",  # религия
                    "крипто", "usdt", "bitcoin", "эфир", "блокчейн", "nft",  # крипто
                    "карта qore", "kyc", "комиссия", "платёж",  # финансовая реклама
                ]
            )
        ),
        "title_suffix": "Victor Stepanych",
        "combine_posts": True,  # объединяем смежные посты
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
            ]
        ),
        "title_suffix": "НаЭкономила",
        "combine_posts": False,
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
                    "негр", "жоп", "ебут", "ибица",  # оскорбительный флаф
                ]
            )
            and not re.match(r"^(Небо|Морпеха|Кстати|Всем)\s+", text, re.IGNORECASE)
        ),
        "title_suffix": "Condottieros",
        "combine_posts": False,
    },
}

# Если вы включите GitHub Pages или иначе разместите публичный feed.xml,
# можно указать сюда его адрес — это улучшит совместимость с некоторыми
# читалками (необязательно).
FEED_PUBLIC_URL = ""

ARCHIVE_PATH = Path("posts_archive.json")
FEED_PATH = Path("feed.xml")

RETENTION_DAYS = 90          # сколько дней хранить записи в публичном feed.xml
ARCHIVE_KEEP_DAYS = 100       # сколько дней хранить сырые посты в архиве (запас)

# Окно по московскому времени, в которое можно собирать дайджест за
# ВЧЕРА. Специально широкое (6 часов), чтобы задержка запуска GitHub
# Actions не привела к пропуску дня.
DIGEST_WINDOW_START_HOUR = 0
DIGEST_WINDOW_END_HOUR = 6

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Пробуем модели по очереди. Первая — "openrouter/free", это
# автомаршрутизатор самого OpenRouter: он сам выбирает, какая бесплатная
# модель сейчас доступна, что снижает риск 404 из-за снятой с продажи
# модели (список бесплатных моделей на OpenRouter меняется чаще, чем у
# большинства других провайдеров). Дальше — пара конкретных моделей на
# случай проблем с автомаршрутизатором.
OPENROUTER_MODELS = [
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-20b:free",
]

REQUEST_TIMEOUT = 25
MAX_RETRIES = 3
BACKOFF_BASE = 2  # секунд

MSK = ZoneInfo("Europe/Moscow")
UTC = ZoneInfo("UTC")


# --------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНОЕ
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S MSK")
    print(f"[{ts}] {msg}", flush=True)


def extract_msgid(link: str):
    """Достаёт числовой ID сообщения из ссылки на пост Telegram, если возможно."""
    if not link:
        return None
    cleaned = link.strip().rstrip("/")
    m = re.search(r"/(\d+)(?:[?#].*)?$", cleaned)
    return int(m.group(1)) if m else None


def clean_text(raw_html: str) -> str:
    text = BeautifulSoup(raw_html or "", "html.parser").get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def format_date_ru(iso_date_str: str) -> str:
    d = date.fromisoformat(iso_date_str)
    return d.strftime("%d.%m.%Y")


def algorithmic_summary(text: str) -> str:
    """Резервный вариант без LLM: первое предложение поста, аккуратно обрезанное."""
    text = (text or "").strip()
    if not text:
        return "Новый пост в канале."
    parts = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    first = parts[0] if parts else text
    if len(first) > 220:
        first = first[:217].rstrip() + "..."
    return first


# --------------------------------------------------------------------------
# ЭТАП 1: СБОР ПОСТОВ
# --------------------------------------------------------------------------

def fetch_source(url: str):
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BankrolloDigestBot/1.0)"},
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
                if not text:
                    text = "(пост без текста, см. оригинал)"

                # Применяем фильтр канала
                filter_fn = channel_cfg.get("filter")
                if filter_fn and not filter_fn(text):
                    log(f"Пост {key} отфильтрован по правилам канала {channel_name}.")
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
    log(f"Собрано новых постов за этот запуск: {new_count}. Всего в архиве: {len(archive)}.")
    return archive


# --------------------------------------------------------------------------
# ЭТАП 2: РЕЗЮМЕ ЧЕРЕЗ OPENROUTER (ОДИН ЗАПРОС НА ВЕСЬ ДЕНЬ) С ФОЛБЭКОМ
# --------------------------------------------------------------------------

def _call_openrouter_once(model: str, prompt: str):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # Рекомендованные OpenRouter заголовки (не обязательны, но помогают
        # с диагностикой и статистикой на стороне OpenRouter).
        "HTTP-Referer": "https://github.com/",
        "X-Title": "Bankrollo Digest Bot",
    }
    resp = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    return resp


def summarize_batch_openrouter(posts: list):
    """Пытается получить резюме для всех постов дня ОДНИМ запросом.
    Возвращает список строк той же длины, что posts, либо None при провале."""
    if not OPENROUTER_API_KEY:
        log("OPENROUTER_API_KEY не задан — пропускаю LLM, будет использовано алгоритмическое резюме.")
        return None

    numbered = "\n".join(f"{i + 1}. {p['text'][:600]}" for i, p in enumerate(posts))
    prompt = (
        "Ты помогаешь составить новостной дайджест Telegram-канала. "
        f"Ниже приведены {len(posts)} постов, пронумерованных по порядку. "
        "Для КАЖДОГО поста напиши ровно одно короткое предложение на русском языке, "
        "передающее суть новости, и добавь в начало предложения один подходящий по "
        "смыслу эмодзи. Верни ТОЛЬКО валидный JSON-массив строк, без каких-либо "
        f"пояснений и markdown-разметки, ровно {len(posts)} элементов в том же порядке, "
        "в котором идут посты.\n\nПосты:\n" + numbered
    )

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
                break  # к следующей модели, без повторов на этой

            if resp.status_code == 429:
                wait = BACKOFF_BASE ** attempt
                log(f"OpenRouter: лимит запросов (429) для модели {model}, "
                    f"повтор через {wait}s (попытка {attempt}/{MAX_RETRIES}).")
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
                summaries = json.loads(content)
                if isinstance(summaries, list) and len(summaries) == len(posts):
                    log(f"OpenRouter: резюме успешно получены (модель {model}).")
                    return [str(s).strip() for s in summaries]
                log(f"OpenRouter: неожиданный формат ответа (модель {model}), пробую ещё раз.")
            except Exception as e:
                log(f"OpenRouter: не удалось разобрать ответ (модель {model}): {e}")
            time.sleep(BACKOFF_BASE ** attempt)

    log("OpenRouter: все модели и попытки исчерпаны — использую алгоритмическое резюме.")
    return None


# --------------------------------------------------------------------------
# ЭТАП 3: СБОРКА ДАЙДЖЕСТА И feed.xml
# --------------------------------------------------------------------------

def digest_exists(target_date: str) -> bool:
    if not FEED_PATH.exists():
        return False
    parsed = feedparser.parse(str(FEED_PATH))
    target_guid = f"digest-{target_date}"
    return any(
        e.get("id") == target_guid or e.get("guid") == target_guid
        for e in parsed.entries
    )


def format_item_description(posts: list, summaries: list) -> str:
    parts = []
    for post, summary in zip(posts, summaries):
        link_escaped = html.escape(post["link"], quote=True)
        summary_escaped = html.escape(summary)
        parts.append(f'{summary_escaped} <a href="{link_escaped}">Ссылка</a>')
    return "<br/><br/>".join(parts)


def load_existing_items(cutoff_date: date) -> list:
    items = []
    if not FEED_PATH.exists():
        return items
    parsed = feedparser.parse(str(FEED_PATH))
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
            "link": e.get("link", "https://t.me/bankrollo"),
            "description": e.get("description", e.get("summary", "")),
            "pub_dt": pub_dt,
        })
    return items


def rebuild_feed(all_items: list, new_item: dict = None) -> FeedGenerator:
    fg = FeedGenerator()
    fg.title("Дайджесты telegram-каналов")
    fg.link(href="https://t.me/bankrollo", rel="alternate")
    if FEED_PUBLIC_URL:
        fg.link(href=FEED_PUBLIC_URL, rel="self")
    fg.description("Автоматические ежедневные дайджесты из telegram-каналов: Bankrollo, Victor Stepanych, НаЭкономила, Condottieros")
    fg.language("ru")

    if new_item:
        all_items = list(all_items) + [new_item]
    else:
        all_items = list(all_items)

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


def write_feed_atomically(fg: FeedGenerator) -> None:
    tmp_path = FEED_PATH.with_suffix(".tmp")
    fg.rss_file(str(tmp_path), pretty=True)
    try:
        ET.parse(tmp_path)
    except ET.ParseError as e:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Сгенерированный feed.xml не прошёл проверку XML: {e}") from e
    tmp_path.replace(FEED_PATH)


def try_build_digest(archive: dict) -> None:
    now_msk = datetime.now(MSK)

    if not (DIGEST_WINDOW_START_HOUR <= now_msk.hour < DIGEST_WINDOW_END_HOUR):
        log("Сейчас не окно сборки дайджеста (00:00–05:59 MSK) — только сбор постов на этом запуске.")
        return

    target_date = (now_msk.date() - timedelta(days=1)).isoformat()

    # Группируем посты по каналам
    posts_by_channel = {}
    for channel_name in CHANNELS.keys():
        posts_by_channel[channel_name] = [
            v for v in archive.values()
            if v.get("date") == target_date and v.get("channel") == channel_name
        ]

    if not any(posts_by_channel.values()):
        log(f"В архиве нет постов за {target_date} — дайджест не создаётся, feed.xml не трогаю.")
        return

    new_items = []
    for channel_name, posts_today in posts_by_channel.items():
        if not posts_today:
            continue

        posts_today.sort(key=lambda p: (p["msgid"] if p["msgid"] is not None else 0, p["ts"]))
        channel_cfg = CHANNELS[channel_name]
        log(f"Собираю дайджест за {target_date} канала {channel_name}: {len(posts_today)} посто(в).")

        summaries = summarize_batch_openrouter(posts_today)
        if not summaries:
            summaries = [algorithmic_summary(p["text"]) for p in posts_today]

        description = format_item_description(posts_today, summaries)
        title = f"{channel_cfg['title_suffix']} — новости за {format_date_ru(target_date)}"

        new_items.append({
            "guid": f"digest-{channel_name}-{target_date}",
            "title": title,
            "link": f"https://t.me/{channel_name}",
            "description": description,
            "pub_dt": datetime.combine(date.fromisoformat(target_date), dt_time(23, 59, 0), tzinfo=MSK),
        })

    cutoff_date = now_msk.date() - timedelta(days=RETENTION_DAYS)
    existing_items = load_existing_items(cutoff_date)
    
    # Проверяем, какие item'ы уже есть (защита от дублей)
    existing_guids = {item["guid"] for item in existing_items}
    items_to_add = [item for item in new_items if item["guid"] not in existing_guids]

    if not items_to_add:
        log(f"Дайджесты за {target_date} уже есть в feed.xml — пропускаю (защита от дублей).")
        return

    fg = rebuild_feed(existing_items, None)
    for item in items_to_add:
        fg = rebuild_feed(existing_items + items_to_add, None)

    try:
        write_feed_atomically(fg)
        log(f"feed.xml обновлён: добавлено {len(items_to_add)} дайджестов.")
    except Exception as e:
        log(f"ОШИБКА при записи feed.xml: {e}. Существующий feed.xml НЕ изменён.")


# --------------------------------------------------------------------------
# ТОЧКА ВХОДА
# --------------------------------------------------------------------------

def main() -> int:
    log("Запуск: сбор постов…")
    archive = collect_posts()
    log("Проверка, не пора ли собрать дайджест за прошедший день…")
    try_build_digest(archive)
    log("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
