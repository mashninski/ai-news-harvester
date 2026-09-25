#!/usr/bin/env python3
"""
Ручная проверка фидов-кандидатов перед добавлением в collect.SOURCES
(спека сайта, §2, «требуют ручной проверки»). Ходит в сеть тем же честным
User-Agent, что и коллектор: если источник режет ботов, это видно сразу.

    python probe_feeds.py              # все кандидаты ниже
    python probe_feeds.py URL [URL…]   # свои адреса

Для каждого адреса печатает HTTP-код, тип фида, число записей и три последних
заголовка с длиной текста. Годится, если: код 200, фид распознан, записи свежие
(дни, не месяцы) и про ИИ, а не чужой раздел сайта.
"""

import sys

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


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    groups = {"свои адреса": sys.argv[1:]} if sys.argv[1:] else CANDIDATES
    for name, urls in groups.items():
        print(name)
        for u in urls:
            probe(u)


if __name__ == "__main__":
    main()
