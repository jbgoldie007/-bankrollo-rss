import os
import re
import html
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests


# RSS Bridge — источник постов Telegram
SOURCE_RSS = (
    "https://wtf.roflcopter.fr/rss-bridge/"
    "?action=display&bridge=Telegram&username=bankrollo&format=Atom"
)

OUTPUT_FILE = "feed.xml"

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = "gemini-2.0-flash"

MOSCOW_TZ = timezone(timedelta(hours=3))


def get_rss():
    response = requests.get(
        SOURCE_RSS,
        timeout=60,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    response.raise_for_status()
    return response.text


def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_date(value):
    if not value:
        return None

    value = value.strip()

    # RFC 822 / RSS date
    try:
        return parsedate_to_datetime(value).astimezone(MOSCOW_TZ)
    except Exception:
        pass

    # ISO date
    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(MOSCOW_TZ)

    except Exception:
        return None


def parse_feed(xml_text):
    root = ET.fromstring(xml_text)

    posts = []

    # Определяем RSS или Atom
    is_atom = root.tag.endswith("feed")

    if is_atom:

        # Atom namespace
        namespace = ""

        if "}" in root.tag:
            namespace = root.tag.split("}")[0] + "}"

        entries = root.findall(f"{namespace}entry")

        for entry in entries:

            title_element = entry.find(
                f"{namespace}title"
            )

            content_element = entry.find(
                f"{namespace}content"
            )

            summary_element = entry.find(
                f"{namespace}summary"
            )

            published_element = entry.find(
                f"{namespace}published"
            )

            updated_element = entry.find(
                f"{namespace}updated"
            )

            title = (
                title_element.text
                if title_element is not None
                else ""
            )

            if content_element is not None:
                text = "".join(
                    content_element.itertext()
                )
            elif summary_element is not None:
                text = "".join(
                    summary_element.itertext()
                )
            else:
                text = ""

            date_text = ""

            if published_element is not None:
                date_text = published_element.text or ""

            elif updated_element is not None:
                date_text = updated_element.text or ""

            post_date = parse_date(date_text)

            # Получаем ссылку на Telegram
            link = ""

            for link_element in entry.findall(
                f"{namespace}link"
            ):
                href = link_element.attrib.get(
                    "href",
                    ""
                )

                if href:
                    link = href
                    break

            if link:
                posts.append(
                    {
                        "title": clean_text(title),
                        "text": clean_text(text),
                        "link": link,
                        "date": post_date,
                    }
                )

    else:

        # Обычный RSS
        for item in root.findall(".//item"):

            title = item.findtext(
                "title",
                ""
            )

            description = item.findtext(
                "description",
                ""
            )

            link = item.findtext(
                "link",
                ""
            )

            pub_date = item.findtext(
                "pubDate",
                ""
            )

            post_date = parse_date(
                pub_date
            )

            if link:
                posts.append(
                    {
                        "title": clean_text(title),
                        "text": clean_text(
                            description
                        ),
                        "link": link.strip(),
                        "date": post_date,
                    }
                )

    return posts


def call_gemini(
    posts,
    target_date
):

    posts_text = []

    for i, post in enumerate(
        posts,
        1
    ):

        posts_text.append(
            f"""
ПОСТ {i}

Заголовок:
{post["title"]}

Текст:
{post["text"]}

Ссылка:
{post["link"]}

"""
        )

    prompt = f"""
Ты редактор ежедневного дайджеста Telegram-канала
«Банки, деньги, два офшора» (Bankrollo).

Дата дайджеста:
{target_date.strftime("%d.%m.%Y")}

Тебе переданы ВСЕ посты канала за этот день.

Твоя задача — обработать КАЖДЫЙ пост.

СТРОГИЕ ПРАВИЛА:

1. НЕ ПРОПУСКАЙ ни одного поста.

2. Каждый пост преврати ровно в ОДНУ короткую строку.

3. Сохрани порядок постов.

4. Не объединяй разные посты.

5. Не удаляй посты.

6. Суть каждого поста передай максимально кратко.

7. Желательная длина — 10–30 слов.

8. Если пост является рекламой, начинай строку с:
[РЕКЛАМА]

9. Если пост содержит мнение автора,
не представляй его как установленный факт.

10. Не придумывай информацию,
которой нет в исходном посте.

11. Каждая строка должна начинаться
с подходящего эмодзи.

12. После краткого текста добавь
кликабельную ссылку на оригинальный пост:

<a href="ССЫЛКА">Ссылка</a>

13. Верни ТОЛЬКО HTML.

Формат:

<ul>
<li>🔥 Краткое содержание новости. <a href="https://t.me/...">Ссылка</a></li>
<li>💰 Краткое содержание новости. <a href="https://t.me/...">Ссылка</a></li>
</ul>

ВОТ ПОСТЫ:

{"".join(posts_text)}
"""

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{GEMINI_MODEL}:generateContent"
        f"?key={GEMINI_API_KEY}"
    )

    response = requests.post(
        url,
        json={
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 12000,
            },
        },
        timeout=180,
    )

    response.raise_for_status()

    data = response.json()

    return (
        data["candidates"][0]
        ["content"]["parts"][0]
        ["text"]
        .strip()
    )


def create_rss(
    digest_date,
    content
):

    # Если RSS уже существует —
    # загружаем его
    if os.path.exists(
        OUTPUT_FILE
    ):

        try:

            tree = ET.parse(
                OUTPUT_FILE
            )

            root = tree.getroot()

            channel = root.find(
                "channel"
            )

            if channel is None:

                channel = ET.SubElement(
                    root,
                    "channel"
                )

        except Exception:

            root = ET.Element(
                "rss",
                {
                    "version": "2.0"
                }
            )

            channel = ET.SubElement(
                root,
                "channel"
            )

            tree = ET.ElementTree(
                root
            )

    else:

        root = ET.Element(
            "rss",
            {
                "version": "2.0"
            }
        )

        channel = ET.SubElement(
            root,
            "channel"
        )

        tree = ET.ElementTree(
            root
        )

    # Заголовок RSS
    if channel.find(
        "title"
    ) is None:

        element = ET.SubElement(
            channel,
            "title"
        )

        element.text = (
            "Bankrollo — ежедневный дайджест"
        )

    # Ссылка на Telegram
    if channel.find(
        "link"
    ) is None:

        element = ET.SubElement(
            channel,
            "link"
        )

        element.text = (
            "https://t.me/bankrollo"
        )

    # Описание
    if channel.find(
        "description"
    ) is None:

        element = ET.SubElement(
            channel,
            "description"
        )

        element.text = (
            "Краткий ежедневный дайджест "
            "постов Telegram-канала Bankrollo"
        )

    date_string = digest_date.strftime(
        "%Y-%m-%d"
    )

    guid_value = (
        f"bankrollo-digest-{date_string}"
    )

    # Проверяем, нет ли уже дайджеста
    for item in channel.findall(
        "item"
    ):

        guid = item.findtext(
            "guid",
            ""
        )

        if guid == guid_value:

            print(
                "Дайджест за эту дату "
                "уже существует."
            )

            return

    # Создаём новую запись
    item = ET.Element(
        "item"
    )

    title = ET.SubElement(
        item,
        "title"
    )

    title.text = (
        "Bankrollo — новости за "
        f"{digest_date.strftime('%d.%m.%Y')}"
    )

    link = ET.SubElement(
        item,
        "link"
    )

    link.text = (
        "https://t.me/bankrollo"
    )

    guid = ET.SubElement(
        item,
        "guid",
        {
            "isPermaLink": "false"
        }
    )

    guid.text = guid_value

    description = ET.SubElement(
        item,
        "description"
    )

    # CDATA вручную
    description.text = (
        "<![CDATA["
        + content
        + "]]>"
    )

    pub_date = ET.SubElement(
        item,
        "pubDate"
    )

    now = datetime.now(
        timezone.utc
    )

    pub_date.text = now.strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    # Новые записи сверху
    channel.insert(
        0,
        item
    )

    # Храним последние 90 дней
    items = channel.findall(
        "item"
    )

    for old_item in items[90:]:

        channel.remove(
            old_item
        )

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )


def main():

    # По умолчанию создаём дайджест
    # за предыдущий календарный день
    target_date = (
        datetime.now(
            MOSCOW_TZ
        ).date()
        - timedelta(
            days=1
        )
    )

    print(
        f"Создаём дайджест за "
        f"{target_date}"
    )

    # Загружаем Atom/RSS
    xml_text = get_rss()

    # Разбираем ленту
    posts = parse_feed(
        xml_text
    )

    print(
        f"Получено постов из источника: "
        f"{len(posts)}"
    )

    # Отбираем посты строго за нужный день
    posts_for_digest = [
        post
        for post in posts
        if post["date"] is not None
        and post["date"].date()
        == target_date
    ]

    print(
        f"Постов за "
        f"{target_date}: "
        f"{len(posts_for_digest)}"
    )

    if not posts_for_digest:

        print(
            "Постов за нужную дату "
            "не найдено."
        )

        return

    # Сортировка от старых к новым
    posts_for_digest.sort(
        key=lambda post: post["date"]
    )

    # Генерируем дайджест
    digest = call_gemini(
        posts_for_digest,
        target_date
    )

    # Сохраняем RSS
    create_rss(
        target_date,
        digest
    )

    print(
        "RSS успешно обновлён."
    )


if __name__ == "__main__":

    main()
