"""Тесты коллектора. Сеть не нужна: collect.fetch подменяется фикстурами из tests/fixtures.

Запуск из корня репозитория: python -m pytest
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collect  # noqa: E402
from collect import Item  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)

OPENAI = {"id": "openai", "kind": "rss", "url": "https://feeds.test/openai.xml"}
TECHCRUNCH = {"id": "techcrunch", "kind": "rss", "url": "https://feeds.test/techcrunch.xml"}
WILLISON = {"id": "willison", "kind": "rss", "url": "https://feeds.test/willison.xml"}
ANTHROPIC = {"id": "anthropic", "kind": "sitemap", "url": "https://www.anthropic.com/sitemap.xml"}
BROKEN = {"id": "broken", "kind": "rss", "url": "https://feeds.test/broken.xml"}

ROUTES = {
    OPENAI["url"]: "feed_openai.xml",
    TECHCRUNCH["url"]: "feed_techcrunch.xml",
    WILLISON["url"]: "feed_willison.xml",
    BROKEN["url"]: "feed_broken.xml",
    "https://www.anthropic.com/sitemap.xml": "sitemap_index.xml",
    "https://www.anthropic.com/sitemap-pages.xml": "sitemap_pages.xml",
    "https://www.anthropic.com/sitemap-news.xml": "sitemap_news.xml",
    "https://www.anthropic.com/news/claude-chat-and-cowork": "article.html",
}


class FakeNet:
    """Подмена сети: отдаёт фикстуры, запоминает запросы, умеет падать по заказу."""

    def __init__(self, errors=None, patch=None):
        self.calls = []
        self.errors = errors or {}   # url → исключение
        self.patch = patch or {}     # url → функция правки содержимого

    def __call__(self, url):
        self.calls.append(url)
        if url in self.errors:
            raise self.errors[url]
        if url not in ROUTES:
            raise requests.HTTPError(f"404 Not Found: {url}")
        data = (FIX / ROUTES[url]).read_bytes()
        return self.patch[url](data) if url in self.patch else data


@pytest.fixture
def db(tmp_path):
    return collect.open_state(tmp_path / "state.sqlite")


def run(db, monkeypatch, sources, net=None):
    net = net or FakeNet()
    monkeypatch.setattr(collect, "fetch", net)
    return collect.collect(db, sources=sources, now=NOW), net


def titles(res):
    return sorted(it.title for it in res.new)


# ---------- критерий готовности этапа 3 ----------

def test_first_run_new_without_duplicates_between_sources(db, monkeypatch):
    res, _ = run(db, monkeypatch, [OPENAI, TECHCRUNCH, WILLISON, ANTHROPIC])

    assert titles(res) == [
        "Better prompt caching for GPT-6",
        "Claude chat and Cowork, now in one place",
        "Introducing GPT-6 Sol and Luna",
        "PrismML brings its tiny LLMs to Qualcomm-powered smart glasses",
        "Shadow roots, explained with live examples",
    ]
    by_title = {it.title: it for it in res.new}
    # Основной — пост вендора, пересказ портала висит на нём
    assert [d.source for d in by_title["Introducing GPT-6 Sol and Luna"].duplicates] == ["techcrunch"]
    assert [d.source for d in by_title["Claude chat and Cowork, now in one place"].duplicates] == ["willison"]
    assert res.duplicates == 2
    # Старое: статья TechCrunch от 1 сентября и новость Anthropic с lastmod 2025 года
    assert res.stale == 2
    assert res.failed == {}


def test_second_run_finds_nothing(db, monkeypatch):
    run(db, monkeypatch, [OPENAI, TECHCRUNCH, WILLISON, ANTHROPIC])
    res, net = run(db, monkeypatch, [OPENAI, TECHCRUNCH, WILLISON, ANTHROPIC])

    assert res.new == [] and res.late == []
    assert res.duplicates == 0 and res.stale == 0
    # Страницу статьи Anthropic второй раз не качаем
    assert not any("/news/" in u for u in net.calls)


def test_broken_sources_are_skipped_not_fatal(db, monkeypatch, caplog):
    net = FakeNet(errors={
        "https://feeds.test/down.xml": requests.ConnectionError("connection refused"),
        "https://feeds.test/slow.xml": requests.Timeout("read timed out"),
        "https://feeds.test/500.xml": requests.HTTPError("500 Server Error"),
    })
    ROUTES["https://feeds.test/html.xml"] = "article.html"   # HTML вместо фида
    try:
        sources = [
            BROKEN,
            {"id": "down", "kind": "rss", "url": "https://feeds.test/down.xml"},
            {"id": "slow", "kind": "rss", "url": "https://feeds.test/slow.xml"},
            {"id": "http500", "kind": "rss", "url": "https://feeds.test/500.xml"},
            {"id": "html", "kind": "rss", "url": "https://feeds.test/html.xml"},
            TECHCRUNCH,
        ]
        with caplog.at_level(logging.WARNING, logger="harvester"):
            res, _ = run(db, monkeypatch, sources, net)
    finally:
        del ROUTES["https://feeds.test/html.xml"]

    assert set(res.failed) == {"broken", "down", "slow", "http500", "html"}
    assert "битый фид" in res.failed["broken"]
    assert "не похож на RSS/Atom" in res.failed["html"]
    # Рабочий источник собран полностью
    assert titles(res) == [
        "OpenAI launches GPT-6 Sol and Luna, boasting lower cost and fewer mistakes",
        "PrismML brings its tiny LLMs to Qualcomm-powered smart glasses",
    ]
    assert sum("ПРОПУЩЕН источник" in r.message for r in caplog.records) == 5
    collect.print_result(res)   # и печать отчёта не падает


def test_broken_sitemap_is_skipped(db, monkeypatch):
    net = FakeNet(patch={"https://www.anthropic.com/sitemap-news.xml": lambda d: d[:120]})
    res, _ = run(db, monkeypatch, [ANTHROPIC, OPENAI], net)
    assert "anthropic" in res.failed
    assert len(res.new) == 2


# ---------- URL ----------

@pytest.mark.parametrize("variant", [
    "https://techcrunch.com/2026/09/22/story/",
    "https://techcrunch.com/2026/09/22/story",
    "https://techcrunch.com/2026/09/22/story/?utm_source=rss&utm_medium=rss",
    "https://techcrunch.com/2026/09/22/story/?utm_campaign=x&fbclid=abc&ref=twitter",
    "https://TechCrunch.com/2026/09/22/story/#comments",
    "http://www.techcrunch.com/2026/09/22/story/",
])
def test_tracking_and_slash_do_not_change_hash(variant):
    assert collect.url_hash(variant) == collect.url_hash("https://techcrunch.com/2026/09/22/story")


def test_meaningful_params_are_kept():
    a = collect.url_hash("https://example.com/post?id=1&page=2")
    assert a == collect.url_hash("https://example.com/post?page=2&id=1&utm_source=x")
    assert a != collect.url_hash("https://example.com/post?id=2&page=2")
    assert collect.clean_url("https://ex.com/a/?utm_source=x&id=5#top") == "https://ex.com/a/?id=5"


def test_changed_tracking_params_are_still_seen(db, monkeypatch):
    run(db, monkeypatch, [TECHCRUNCH])
    net = FakeNet(patch={TECHCRUNCH["url"]: lambda d: d.replace(b"utm_source=rss", b"utm_source=newsletter")})
    res, _ = run(db, monkeypatch, [TECHCRUNCH], net)
    assert res.new == []


# ---------- sitemap ----------

def test_sitemap_index_mask_and_single_page_fetch(db, monkeypatch):
    res, net = run(db, monkeypatch, [ANTHROPIC])

    # Обошли index, скачали только одну страницу — свежую новость.
    # /news (лента), /careers, /research/... и старая новость не качаются
    pages = [u for u in net.calls if not u.endswith(".xml")]
    assert pages == ["https://www.anthropic.com/news/claude-chat-and-cowork"]

    [it] = res.new
    assert it.title == "Claude chat and Cowork, now in one place"
    assert it.published_at == "2026-09-22T00:00:00Z"   # дата со страницы, а не lastmod
    assert it.body.startswith("Starting today")
    assert "ResearchPolicy" not in it.body and "Short line" not in it.body
    assert res.stale == 1


def test_sitemap_page_failure_is_retried_next_run(db, monkeypatch):
    page = "https://www.anthropic.com/news/claude-chat-and-cowork"
    res, _ = run(db, monkeypatch, [ANTHROPIC], FakeNet(errors={page: requests.Timeout("slow")}))
    assert res.new == [] and res.failed == {}

    res, _ = run(db, monkeypatch, [ANTHROPIC])
    assert titles(res) == ["Claude chat and Cowork, now in one place"]


def test_sitemap_new_url_detected_even_with_old_lastmod_seen_before(db, monkeypatch):
    """Правка старой статьи двигает lastmod, но URL уже виден — не новое."""
    run(db, monkeypatch, [ANTHROPIC])
    bump = FakeNet(patch={"https://www.anthropic.com/sitemap-news.xml":
                          lambda d: d.replace(b"2025-03-01T12:00:00.000Z", b"2026-09-24T12:00:00.000Z")})
    res, net = run(db, monkeypatch, [ANTHROPIC], bump)
    assert res.new == []
    assert not any("/news/" in u for u in net.calls)


# ---------- склейка ----------

def item(source, title, when, url=None):
    return Item(source, url or f"https://{source}.test/{abs(hash(title))}", when, title, "")


def test_cluster_same_story_from_vendor_and_portal():
    a = item("openai", "Introducing GPT-6 Sol and Luna", "2026-09-22T15:00:00Z")
    b = item("techcrunch", "OpenAI launches GPT-6 Sol and Luna, boasting lower cost", "2026-09-22T21:00:00Z")
    new, late = collect.cluster([b, a])
    assert [p.source for p in new] == ["openai"]
    assert new[0].duplicates == [b]


def test_cluster_does_not_glue_by_brand_and_verb():
    """«OpenAI анонсировала X» и «OpenAI анонсировала Y» — разные новости."""
    a = item("openai", "OpenAI announces partnership with Airbnb", "2026-09-23T10:00:00Z")
    b = item("techcrunch", "OpenAI announces new data center in Texas", "2026-09-23T12:00:00Z")
    new, _ = collect.cluster([a, b])
    assert len(new) == 2


def test_cluster_ignores_series_words():
    """Два поста DeepMind про линейку Gemini 3.8: «gemini» и «3.8» — слова серии,
    и заголовок Willison не должен приклеиться к посту про Live Avatar."""
    live = item("deepmind", "Introducing Gemini 3.8 Live with Live Avatar", "2026-09-24T16:20:00Z")
    tts = item("deepmind", "Gemini 3.8 text-to-speech says hello", "2026-09-23T15:25:00Z")
    sw = item("willison", "Gemini 3.8 TTS Playground", "2026-09-23T17:12:00Z")
    new, _ = collect.cluster([live, tts, sw])
    assert len(new) == 3


def test_cluster_respects_time_window():
    a = item("openai", "Introducing GPT-6 Sol and Luna", "2026-09-10T15:00:00Z")
    b = item("techcrunch", "OpenAI launches GPT-6 Sol and Luna", "2026-09-22T21:00:00Z")
    new, _ = collect.cluster([a, b])
    assert len(new) == 2


def test_cluster_one_duplicate_per_source():
    """К отданному в прошлом прогоне материалу уже приклеен повтор TechCrunch —
    второй материал TechCrunch к нему не клеится, а идёт отдельным."""
    a = item("openai", "Introducing GPT-6 Sol and Luna", "2026-09-22T15:00:00Z")
    a.duplicates.append(item("techcrunch", "OpenAI launches GPT-6 Sol and Luna", "2026-09-22T21:00:00Z"))
    c = item("techcrunch", "GPT-6 Sol and Luna hands-on", "2026-09-23T09:00:00Z")
    new, late = collect.cluster([c], known=[a])
    assert new == [c] and late == []


def test_cluster_same_source_follow_up_is_separate():
    """Второй материал того же портала о той же новости (обзор после анонса):
    его слова стали словами серии, он идёт отдельным материалом."""
    a = item("openai", "Introducing GPT-6 Sol and Luna", "2026-09-22T15:00:00Z")
    b = item("techcrunch", "OpenAI launches GPT-6 Sol and Luna", "2026-09-22T21:00:00Z")
    c = item("techcrunch", "GPT-6 Sol and Luna hands-on", "2026-09-23T09:00:00Z")
    new, _ = collect.cluster([a, b, c])
    assert len(new) == 3


def test_late_duplicate_attaches_to_already_emitted(db, monkeypatch):
    """Пост вендора пришёл в одном прогоне, статья портала о нём — в следующем."""
    run(db, monkeypatch, [OPENAI])
    res, _ = run(db, monkeypatch, [TECHCRUNCH])

    assert titles(res) == ["PrismML brings its tiny LLMs to Qualcomm-powered smart glasses"]
    [(dup, primary)] = res.late
    assert dup.source == "techcrunch" and primary.title == "Introducing GPT-6 Sol and Luna"
    row = db.execute("SELECT status, primary_hash FROM seen WHERE hash = ?", (dup.hash,)).fetchone()
    assert row == ("duplicate", primary.hash)


def test_dry_run_writes_nothing(db, monkeypatch):
    monkeypatch.setattr(collect, "fetch", FakeNet())
    collect.collect(db, sources=[OPENAI], now=NOW, dry_run=True)
    assert db.execute("SELECT COUNT(*) FROM seen").fetchone() == (0,)
