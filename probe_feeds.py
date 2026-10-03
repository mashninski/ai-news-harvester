#!/usr/bin/env python3
"""
Ручная проверка фидов-кандидатов перед добавлением в collect.SOURCES
(спека сайта, §2, «требуют ручной проверки»). Ходит в сеть тем же честным
User-Agent, что и коллектор: если источник режет ботов, это видно сразу.

    python probe_feeds.py              # все кандидаты ниже
    python probe_feeds.py URL [URL…]   # свои адреса

Для каждого адреса печатает HTTP-код, тип фида, число записей и три последних
заголовка с длиной текста, плюс что robots.txt сайта говорит нашему User-Agent.
Годится, если: код 200, фид распознан, записи свежие (дни, не месяцы) и про ИИ,
а не чужой раздел сайта, robots.txt адрес не закрывает и в записях есть текст
для пересказа (сотни знаков, а не анонс в одно предложение).

robots.txt с кодом 4xx честно печатается как «не прочитан»: так было
у Ars Technica 03.10.2026 (боту 403, в браузере — запрет ИИ-ботов поимённо).
Это не «разрешено», а повод посмотреть файл руками и спросить автора.
"""

import sys
import urllib.robotparser
from urllib.parse import urlsplit

import feedparser
import requests

import collect

CANDIDATES = {
    "Ars Technica AI": ["https://arstechnica.com/ai/feed/"],
    "Meta AI blog": ["https://ai.meta.com/blog/rss/", "https://ai.meta.com/blog/feed/"],
    "Mistral AI": ["https://mistral.ai/news/rss.xml", "https://mistral.ai/rss.xml"],
    "Microsoft AI": ["https://blogs.microsoft.com/ai/feed/", "https://microsoft.ai/feed/"],
}


def probe(url: str):
    try:
        r = requests.get(url, headers={"User-Agent": collect.USER_AGENT}, timeout=collect.TIMEOUT)
    except Exception as ex:
        print(f"  ОШИБКА {type(ex).__name__}: {str(ex)[:160]}\n    {url}")
        return
    f = feedparser.parse(r.content)
    print(f"  HTTP {r.status_code}, фид: {f.version or 'не распознан'}, записей: {len(f.entries)}\n    {url}")
    for e in f.entries[:3]:
        content = e.get("content") or []
        body = content[0].get("value", "") if content else e.get("summary", "")
        print(f"    {e.get('published', e.get('updated', '?'))[:16]}  {e.get('title', '')[:70]}"
              f"  (текст: {len(collect.html_to_text(body))} зн.)")
    print(f"    {robots(url)}")


def robots(url: str) -> str:
    """Что robots.txt сайта говорит про этот адрес нашему User-Agent."""
    parts = urlsplit(url)
    try:
        r = requests.get(f"{parts.scheme}://{parts.netloc}/robots.txt",
                         headers={"User-Agent": collect.USER_AGENT}, timeout=collect.TIMEOUT)
    except Exception as ex:
        return f"robots.txt: ОШИБКА {type(ex).__name__}"
    if r.status_code != 200:
        return f"robots.txt: HTTP {r.status_code} — не прочитан, посмотреть руками"
    p = urllib.robotparser.RobotFileParser()
    p.parse(r.text.splitlines())
    return "robots.txt: адрес разрешён" if p.can_fetch(collect.USER_AGENT, url) else "robots.txt: адрес ЗАКРЫТ"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    groups = {"свои адреса": sys.argv[1:]} if sys.argv[1:] else CANDIDATES
    for name, urls in groups.items():
        print(name)
        for u in urls:
            probe(u)


if __name__ == "__main__":
    main()
