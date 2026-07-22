import os
import re
import html
import hashlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests


SOURCE_RSS = "https://rsshub.app/telegram/channel/bankrollo"
OUTPUT_FILE = "feed.xml"

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = "gemini-2.5-flash"

MOSCOW_TZ = timezone(timedelta(hours=3))


def get_rss():
    response = requests.get(
        SOURCE_RSS,
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    return response.text


def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_rss(xml_text):
    root = ET.fromstring(xml_text)

    items = []

    for item in root.findall(".//item"):
        title = item.findtext("title", "")
        description = item.findtext("description", "")
        link = item.findtext("link", "")
        pub_date = item.findtext("pubDate", "")

        if not link:
            continue

        items.append({
            "title": clean_text(title),
            "text": clean_text(description),
            "link": link.strip(),
            "pub_date": pub_date.strip(),
        })

    return items


def post_id_from_link(link):
    match = re.search(r"/(\d+)(?:\?.*)?$", link)
    return match.group(1) if match else hashlib.md5(link.encode()).hexdigest()


def get_post_date(item):
    """
    Пытаемся получить дату из RSS.
    Если RSSHub не отдаёт pubDate, дата берётся из GitHub Actions
    через переменную TARGET_DATE.
    """

    if item["pub_date"]:
        try:
            dt = parsedate_to_datetime(item["pub_date"])
            return dt.astimezone(MOSCOW_TZ).date()
        except Exception:
            pass

    return None


def call_gemini(posts, target_date):
    posts_text = []

    for i, post in enumerate(posts, 1):
        posts_text.append(
            f"""
POST {i}
TITLE: {post['title']}
TEXT: {post['text']}
LINK: {post['link']}
"""
        )

    prompt = f"""
Ты редактор ежедневного дайджеста Telegram-канала «Банки, деньги, два офшора».

Дата дайджеста: {target_date.strftime('%d.%m.%Y')}

Нужно обработать ВСЕ переданные посты.

Правила:
1. НЕ пропускай посты.
2. Каждый пост преврати ровно в ОДНУ короткую строку на русском языке.
3. Суть поста передай максимально кратко, желательно 10–25 слов.
4. Если это реклама — начинай строку с [РЕКЛАМА].
5. Если это не новость, а мнение или комментарий автора — передай это как мнение, не выдавай за факт.
6. Не придумывай факты.
7. Не объединяй разные посты.
8. Не удаляй посты.
9. Сохрани порядок постов.
10. Каждая строка должна начинаться с подходящего эмодзи.
11. После текста добавь ссылку на оригинал в формате:
   <a href="ССЫЛКА">Оригинал</a>

Верни ТОЛЬКО HTML-список:
<ul>
<li>...</li>
<li>...</li>
</ul>

Вот посты:
{''.join(posts_text)}
"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    response = requests.post(
        url,
        json={
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8000,
            },
        },
        timeout=120,
    )

    response.raise_for_status()

    data = response.json()

    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def create_rss(digest_date, content):
    if os.path.exists(OUTPUT_FILE):
        try:
            root = ET.parse(OUTPUT_FILE).getroot()
            channel = root.find("channel")

            existing_items = []

            if channel is not None:
                for item in channel.findall("item"):
                    existing_items.append(item)

            if channel is None:
                channel = ET.SubElement(root, "channel")

        except Exception:
            root = ET.Element("rss", {"version": "2.0"})
            channel = ET.SubElement(root, "channel")
            existing_items = []
    else:
        root = ET.Element("rss", {"version": "2.0"})
        channel = ET.SubElement(root, "channel")
        existing_items = []

    channel.find("title") is None and ET.SubElement(
        channel, "title"
    ).__setattr__("text", "Bankrollo — ежедневный дайджест")

    channel.find("link") is None and ET.SubElement(
        channel, "link"
    ).__setattr__("text", "https://t.me/bankrollo")

    channel.find("description") is None and ET.SubElement(
        channel, "description"
    ).__setattr__(
        "text",
        "Краткая ежедневная выжимка постов Telegram-канала Bankrollo"
    )

    date_string = digest_date.strftime("%Y-%m-%d")
    item_link = f"https://t.me/bankrollo/digest-{date_string}"

    # Не создаём дубль, если этот день уже есть
    for item in existing_items:
        guid = item.findtext("guid", "")
        if date_string in guid:
            return

    item = ET.Element("item")

    title = ET.SubElement(item, "title")
    title.text = f"Bankrollo — новости за {digest_date.strftime('%d.%m.%Y')}"

    link = ET.SubElement(item, "link")
    link.text = item_link

    guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid.text = f"bankrollo-{date_string}"

    description = ET.SubElement(item, "description")
    description.text = f"<![CDATA[{content}]]>"

    pub_date = ET.SubElement(item, "pubDate")
    pub_date.text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    channel.insert(0, item)

    # Храним максимум 90 дней
    all_items = channel.findall("item")

    for old_item in all_items[90:]:
        channel.remove(old_item)

    ET.ElementTree(root).write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():
    target_date_string = os.environ.get("TARGET_DATE")

    if target_date_string:
        target_date = datetime.strptime(
            target_date_string,
            "%Y-%m-%d"
        ).date()
    else:
        target_date = (
            datetime.now(MOSCOW_TZ).date()
            - timedelta(days=1)
        )

    print(f"Создаём дайджест за {target_date}")

    rss = get_rss()
    posts = parse_rss(rss)

    print(f"Получено постов из RSS: {len(posts)}")

    # Если RSS отдаёт даты — фильтруем по дате.
    dated_posts = [
        post
        for post in posts
        if get_post_date(post) == target_date
    ]

    # Если RSSHub не отдаёт даты, используем все полученные посты.
    # Это временный fallback, чтобы автоматизация не падала.
    if dated_posts:
        posts_for_digest = dated_posts
        print(f"Постов за дату {target_date}: {len(posts_for_digest)}")
    else:
        posts_for_digest = posts
        print(
            "В RSS нет доступных дат публикации. "
            "Используем все полученные посты."
        )

    if not posts_for_digest:
        print("Нет постов. Завершаем работу.")
        return

    digest = call_gemini(
        posts_for_digest,
        target_date,
    )

    create_rss(
        target_date,
        digest,
    )

    print("RSS успешно обновлён.")


if __name__ == "__main__":
    main()
