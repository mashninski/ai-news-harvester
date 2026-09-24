#!/usr/bin/env python3
"""
Коллектор новостей об ИИ для раздела mashninski.com/naviny — этап 3 плана.

Что делает за один прогон:
  1. скачивает 7 RSS/Atom-фидов и sitemap Anthropic (у него нет RSS);
  2. приводит всё к виду {source, url, published_at, title, body, hash};
  3. отбрасывает уже виденное — по нормализованному URL, против data/state.sqlite;
  4. отбрасывает старше MAX_AGE_DAYS — в фидах OpenAI и Hugging Face лежит весь
     архив, без отсечки первый прогон выдал бы ~2500 «новых»;
  5. склеивает одну новость из разных источников по словам заголовка —
     и внутри прогона, и с отданным в прошлых прогонах за последние 72 часа;
  6. печатает список новых и записывает всё увиденное в состояние, а текст
     новых материалов — в таблицу article для пайплайна (pipeline.py).

Сломанный источник (сеть, таймаут, HTTP-ошибка, битый XML) пропускается
с предупреждением в лог, остальные собираются как обычно.

Спека — claude/ai-news-spec.md в репозитории сайта (§2 — источники,
§5 — архитектура), план — claude/ai-news-plan.md (§3, этап 3).
"""

import argparse
import calendar
import hashlib
import json
import logging
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "data" / "state.sqlite"
SCHEMA_PATH = ROOT / "schema.sql"

# Честное имя бота. Под браузер не маскируемся: если источник режет ботов
# (VentureBeat отдаёт 429 всем, кроме браузеров и известных читалок), он просто
# пропускается, как любой сломанный фид.
USER_AGENT = "ai-news-harvester/0.1 (+https://github.com/mashninski/ai-news-harvester)"
TIMEOUT = 20

SOURCES = [
    {"id": "openai",     "kind": "rss",     "url": "https://openai.com/news/rss.xml"},
    {"id": "deepmind",   "kind": "rss",     "url": "https://deepmind.google/blog/rss.xml"},
    {"id": "huggingface", "kind": "rss",    "url": "https://huggingface.co/blog/feed.xml"},
    {"id": "anthropic",  "kind": "sitemap", "url": "https://www.anthropic.com/sitemap.xml"},
    {"id": "techcrunch", "kind": "rss",     "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"id": "venturebeat", "kind": "rss",    "url": "https://venturebeat.com/category/ai/feed/"},
    {"id": "mittr",      "kind": "rss",     "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed"},
    {"id": "willison",   "kind": "rss",     "url": "https://simonwillison.net/atom/everything/"},
    # Кандидаты из спеки, §2 («требуют ручной проверки»): Ars Technica AI, Meta AI,
    # Mistral AI, Microsoft AI. 24.09.2026 проверить не удалось — из облачной сессии
    # сайты источников закрыты сетевой политикой. Добавлять сюда только после
    # проверки: python probe_feeds.py
]

# Блоги самих компаний. В кластере основным становится материал отсюда:
# это первоисточник, портал его пересказывает.
VENDORS = {"openai", "deepmind", "huggingface", "anthropic"}

MAX_AGE_DAYS = 7              # старше — пишем в состояние как виденное, дальше не отдаём

# Sitemap Anthropic
NEWS_PATH = re.compile(r"^/news/[^/]+$")   # /news/<slug>; сама лента /news сюда не входит
SITEMAP_MAX_DEPTH = 2         # sitemap index → вложенные sitemap, глубже не ходим
PAGE_FETCH_LIMIT = 15         # страниц за прогон; не скачанные подождут следующего
BODY_PARAGRAPHS = 12          # сколько абзацев статьи брать в body
BODY_MAX_WORDS = 700          # больше для пересказа не нужно (спека, §3, п. 3)

# Склейка заголовков — значения подобраны на реальной выдаче 24.09.2026,
# разбор в журнале сайта за эту дату
CLUSTER_WINDOW_HOURS = 72     # разница публикаций больше — это разные новости
CLUSTER_MIN_SHARED = 2        # общих значимых слов не меньше
CLUSTER_MIN_OVERLAP = 0.5     # доля общих слов от более короткого заголовка

log = logging.getLogger("harvester")


@dataclass
class Item:
    source: str
    url: str
    published_at: str | None  # ISO 8601 в UTC, "2026-09-24T16:20:39Z"
    title: str
    body: str
    hash: str = ""
    duplicates: list = field(default_factory=list)  # повторы из других источников

    def __post_init__(self):
        if not self.hash:
            self.hash = url_hash(self.url)


# ---------- URL ----------

# Метки рекламных кампаний и рефералок. Одна статья с разными метками —
# одна статья; без их удаления уже виденное возвращалось бы как новое.
TRACKING_PARAMS = {
    "ref", "ref_src", "ref_url", "referrer", "fbclid", "gclid", "dclid", "gbraid",
    "wbraid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid", "mkt_tok",
    "_hsenc", "_hsmi", "cmpid", "guccounter", "guce_referrer", "guce_referrer_sig",
    "__twitter_impression", "s_cid", "sr_share",
}


def _is_tracking(name: str) -> bool:
    name = name.lower()
    return name.startswith("utm_") or name in TRACKING_PARAMS


def clean_url(url: str) -> str:
    """Ссылка для показа: без меток, без #якоря, хост в нижнем регистре."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def url_key(url: str) -> str:
    """Ключ для сравнения: как clean_url, плюс без схемы, без www., без
    завершающего слэша и с отсортированными параметрами. Ходить по нему нельзя."""
    parts = urlsplit(clean_url(url))
    host = parts.netloc.removeprefix("www.")
    path = parts.path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return f"{host}{path}" + (f"?{query}" if query else "")


def url_hash(url: str) -> str:
    return hashlib.sha1(url_key(url).encode("utf-8")).hexdigest()


# ---------- сеть ----------

def fetch(url: str) -> bytes:
    """Единственная точка выхода в сеть — тесты подменяют её фикстурами."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


# ---------- даты и текст ----------

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def html_to_text(html: str) -> str:
    """HTML из фида в плоский текст: абзацы через пустую строку."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.find_all(["p", "li", "h2", "h3", "blockquote", "pre"])
    if blocks:
        parts = [b.get_text(" ", strip=True) for b in blocks if not b.find_parent(["li", "blockquote"])]
    else:
        parts = [soup.get_text(" ", strip=True)]
    return "\n\n".join(re.sub(r"\s+", " ", p) for p in parts if p.strip())


# ---------- RSS / Atom ----------

def fetch_rss(source: dict, is_seen, cutoff: datetime) -> list[Item]:
    feed = feedparser.parse(fetch(source["url"]))
    if feed.bozo and not feed.entries:
        raise ValueError(f"битый фид: {feed.bozo_exception}")
    if not feed.version and not feed.entries:
        # HTML-страница ошибки или пустой ответ: feedparser не считает это ошибкой
        # и молча отдаёт ноль записей — источник умер бы незаметно
        raise ValueError("ответ не похож на RSS/Atom")
    if feed.bozo:
        # Обрезанный или кривой XML: feedparser спас часть записей — берём их
        log.warning("%s: фид прочитан с ошибкой разбора: %s", source["id"], feed.bozo_exception)

    items = []
    for e in feed.entries:
        link, title = e.get("link"), (e.get("title") or "").strip()
        if not link or not title:
            continue
        parsed = e.get("published_parsed") or e.get("updated_parsed")
        published = iso(datetime.fromtimestamp(calendar.timegm(parsed), timezone.utc)) if parsed else None
        # content:encoded (или atom content), если есть, — он полнее summary
        content = e.get("content") or []
        body_html = content[0].get("value", "") if content else e.get("summary", "")
        items.append(Item(source["id"], clean_url(link), published, title, html_to_text(body_html)))
    return items


# ---------- sitemap (Anthropic) ----------

SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def sitemap_urls(url: str, depth: int = 0) -> list[tuple[str, str | None]]:
    """Все (loc, lastmod) из sitemap; sitemap index обходится рекурсивно."""
    root = ET.fromstring(fetch(url))
    if root.tag == f"{SM}sitemapindex":
        if depth >= SITEMAP_MAX_DEPTH:
            raise ValueError(f"sitemap index глубже {SITEMAP_MAX_DEPTH} уровней: {url}")
        out = []
        for sm in root.findall(f"{SM}sitemap"):
            out += sitemap_urls(sm.findtext(f"{SM}loc").strip(), depth + 1)
        return out
    if root.tag != f"{SM}urlset":
        raise ValueError(f"не sitemap: корневой тег {root.tag}")
    return [(u.findtext(f"{SM}loc").strip(), u.findtext(f"{SM}lastmod"))
            for u in root.findall(f"{SM}url") if u.findtext(f"{SM}loc")]


MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
# Без \b в начале: у Anthropic шапка склеена без пробела — «…one placeSep 22, 2026»
TEXT_DATE = re.compile(rf"({MONTHS})[a-z]* (\d{{1,2}}), (\d{{4}})\b")


def parse_article(html: bytes, max_paragraphs: int = BODY_PARAGRAPHS) -> tuple[str, str, str | None]:
    """Заголовок, первые абзацы и дата публикации со страницы статьи.
    Пайплайн (pipeline.py) зовёт её же, когда в фиде DeepMind или Hugging Face
    текста почти нет: одна и та же разборка страниц на оба случая."""
    soup = BeautifulSoup(html, "html.parser")

    og = soup.find("meta", property="og:title")
    title = og["content"].strip() if og and og.get("content") else ""
    if not title and soup.title:
        # <title> у Anthropic вида «Заголовок \ Anthropic»
        title = re.split(r"\s+[\\|]\s+", soup.title.get_text(strip=True))[0]

    # Дата: meta article:published_time, иначе дата текстом «Sep 23, 2026»
    # (у Anthropic в разметке только она), иначе вызывающий возьмёт lastmod
    published = None
    meta = soup.find("meta", property="article:published_time")
    if meta and meta.get("content"):
        dt = parse_iso(meta["content"])
        published = iso(dt) if dt else None
    if not published:
        m = TEXT_DATE.search(soup.get_text(" "))
        if m:
            dt = datetime.strptime(f"{m[1]} {m[2]} {m[3]}", "%b %d %Y").replace(tzinfo=timezone.utc)
            published = iso(dt)

    # Абзацы из <article>, иначе из <main>. Абзац-«шапка» с навигацией у Anthropic
    # тоже <p> внутри article — отсекаем короткие и те, где нет точки
    scope = soup.find("article") or soup.find("main") or soup
    paras = []
    for p in scope.find_all("p"):
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(text) >= 60 and "." in text:
            paras.append(text)
        if len(paras) >= max_paragraphs:
            break
    return title, "\n\n".join(paras), published


def fetch_sitemap(source: dict, is_seen, cutoff: datetime) -> list[Item]:
    """Новое — это URL, которого не было в прошлых прогонах, а не свежий lastmod:
    lastmod меняется и при правке старой статьи. lastmod здесь служит только
    дешёвым фильтром, чтобы не качать страницы заведомо старых статей."""
    base = urlsplit(source["url"])

    fresh, stale = [], []
    for loc, lastmod in sitemap_urls(source["url"]):
        parts = urlsplit(loc)
        if parts.netloc != base.netloc or not NEWS_PATH.match(parts.path.rstrip("/")):
            continue
        url = clean_url(loc)
        if is_seen(url_hash(url)):
            continue
        mod = parse_iso(lastmod)
        if mod and mod < cutoff:
            # Страницу не качаем. Запись без заголовка уйдёт в состояние
            # как «старое» и в выдачу не попадёт.
            stale.append(Item(source["id"], url, iso(mod), "", ""))
        else:
            fresh.append((url, mod))

    fresh.sort(key=lambda x: x[1] or datetime.max.replace(tzinfo=timezone.utc), reverse=True)
    if len(fresh) > PAGE_FETCH_LIMIT:
        log.warning("%s: новых страниц %d, качаю %d, остальные в следующий прогон",
                    source["id"], len(fresh), PAGE_FETCH_LIMIT)

    items = []
    for url, mod in fresh[:PAGE_FETCH_LIMIT]:
        try:
            title, body, published = parse_article(fetch(url))
        except Exception as ex:
            # Не записываем в виденное — попробуем в следующий прогон
            log.warning("%s: не скачалась страница %s: %s", source["id"], url, ex)
            continue
        if not title:
            log.warning("%s: нет заголовка на странице %s, пропускаю", source["id"], url)
            continue
        items.append(Item(source["id"], url, published or (iso(mod) if mod else None), title, body))
    return items + stale


ADAPTERS = {"rss": fetch_rss, "sitemap": fetch_sitemap}


# ---------- склейка заголовков ----------

# Служебные английские слова и слова, которые есть в каждом втором заголовке
# новостей об ИИ и ничего не говорят о том, какая это новость
STOPWORDS = set("""
a an the and or but of to in on at for from by with without into onto over under about
as is are was were be been being it its this that these those their there here
you your we our us they them he she his her i my me not no yes can will would could should
may might must do does did done has have had how why what when where who which whom
new now just more most than then also only all any some every up down out off after before
vs via per says said say saying announce announces announced announcing launch launches
launched launching introduce introduces introduced introducing unveil unveils unveiled
release releases released releasing today week weeks year years day days first
ai artificial intelligence model models company companies startup startups
""".split())

TOKEN = re.compile(r"[a-z0-9]+(?:[.\-'’][a-z0-9]+)*")


def title_tokens(title: str) -> set[str]:
    out = set()
    for t in TOKEN.findall(title.lower()):
        t = t.replace("’", "'").removesuffix("'s")
        if t in STOPWORDS or len(t) < 2:
            continue
        # Грубое множественное число: avatars → avatar, но не glass, opus, analysis
        if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
            t = t[:-1]
        out.add(t)
    return out


def series_finder(pool: list[Item]):
    """Слова серии: у одного источника они стоят в двух и больше заголовках
    в пределах окна склейки. У DeepMind в одну неделю «Gemini 3.8 Live»
    и «Gemini 3.8 text-to-speech» — «gemini» и «3.8» там название линейки,
    а не признак новости, и без этой поправки оба поста склеивались
    с «Gemini 3.8 TTS Playground» у Willison одинаково."""
    by_src = defaultdict(list)
    for it in pool:
        dt = parse_iso(it.published_at)
        if dt:
            by_src[it.source].append((dt, title_tokens(it.title)))
    window = timedelta(hours=CLUSTER_WINDOW_HOURS)
    cache = {}

    def series(source: str, dt: datetime | None) -> set[str]:
        if not dt:
            return set()
        if (source, dt) not in cache:
            cnt = Counter()
            for d, toks in by_src[source]:
                if abs(d - dt) <= window:
                    cnt.update(toks)
            cache[(source, dt)] = {t for t, n in cnt.items() if n >= 2}
        return cache[(source, dt)]

    return series


def same_story(a: Item, b: Item, series) -> bool:
    if a.source == b.source:
        # Одну новость дважды в одном источнике не публикуют, а серии склеились бы
        return False
    da, db = parse_iso(a.published_at), parse_iso(b.published_at)
    if da and db and abs(da - db) > timedelta(hours=CLUSTER_WINDOW_HOURS):
        return False
    ta = title_tokens(a.title) - series(a.source, da)
    tb = title_tokens(b.title) - series(b.source, db)
    shared = ta & tb
    if len(shared) < CLUSTER_MIN_SHARED:
        return False
    return len(shared) / min(len(ta), len(tb)) >= CLUSTER_MIN_OVERLAP


def cluster(fresh: list[Item], known: list[Item] = ()) -> tuple[list[Item], list[tuple[Item, Item]]]:
    """Склеивает одну новость из разных источников.

    fresh — новое в этом прогоне, known — отданное в прошлых прогонах за окно
    склейки (с их прежними повторами в .duplicates). При cron раз в 2 часа
    пост вендора и статья портала о нём обычно приходят в разные прогоны,
    поэтому сравнивать только внутри прогона мало.

    Возвращает (новые основные материалы с повторами в .duplicates,
    [(поздний повтор, уже отданный основной)]).

    Каждый материал сравнивается только с основным материалом кластера, не со
    всеми его участниками: иначе A≈B и B≈C склеили бы разные A и C цепочкой.
    Основным становится уже отданный, иначе — пост вендора, иначе — более ранний."""
    series = series_finder([*fresh, *known])
    fresh_hashes = {it.hash for it in fresh}

    def order(it: Item):
        return (it.source not in VENDORS, it.published_at or "9999")

    primaries: list[Item] = list(known)
    for it in sorted(fresh, key=order):
        for p in primaries:
            if same_story(p, it, series) and all(d.source != it.source for d in p.duplicates):
                p.duplicates.append(it)
                break
        else:
            primaries.append(it)

    new = [p for p in primaries if p.hash in fresh_hashes]
    late = [(d, p) for p in known for d in p.duplicates if d.hash in fresh_hashes]
    return new, late


# ---------- состояние ----------

def open_state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return db


def truncate_words(text: str, limit: int = BODY_MAX_WORDS) -> str:
    """Первые limit слов с сохранением абзацев."""
    out, n = [], 0
    for para in text.split("\n\n"):
        words = para.split()
        if n + len(words) > limit:
            if limit - n > 0:
                out.append(" ".join(words[: limit - n]) + " …")
            break
        out.append(para)
        n += len(words)
    return "\n\n".join(out)


def save(db: sqlite3.Connection, rows: list[tuple], bodies: list[tuple] = ()):
    db.executemany(
        "INSERT OR IGNORE INTO seen (hash, url, source, title, published_at, first_seen_at, status, primary_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    db.executemany("INSERT OR IGNORE INTO article (hash, body) VALUES (?, ?)", bodies)
    db.commit()


def load_known(db: sqlite3.Connection, since: datetime) -> list[Item]:
    """Отданные раньше материалы, опубликованные после since, с их повторами."""
    rows = db.execute(
        "SELECT hash, url, source, title, published_at, status, primary_hash FROM seen"
        " WHERE status IN ('new', 'duplicate') AND published_at >= ?", (iso(since),)).fetchall()
    known = {h: Item(src, url, pub, title or "", "", h)
             for h, url, src, title, pub, status, _ in rows if status == "new"}
    for h, url, src, title, pub, status, primary in rows:
        if status == "duplicate" and primary in known:
            known[primary].duplicates.append(Item(src, url, pub, title or "", "", h))
    return list(known.values())


# ---------- прогон ----------

@dataclass
class Result:
    new: list[Item]               # основные материалы с повторами внутри
    late: list[tuple[Item, Item]]  # (повтор, отданный в прошлых прогонах основной)
    duplicates: int               # повторов всего, включая поздние
    stale: int
    failed: dict[str, str]        # источник → причина


def collect(db: sqlite3.Connection, sources=SOURCES, now: datetime | None = None,
            dry_run: bool = False) -> Result:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)

    def is_seen(h: str) -> bool:
        return db.execute("SELECT 1 FROM seen WHERE hash = ?", (h,)).fetchone() is not None

    raw: list[Item] = []
    failed = {}
    for src in sources:
        try:
            got = ADAPTERS[src["kind"]](src, is_seen, cutoff)
        except Exception as ex:
            # Любая ошибка источника — сеть, таймаут, HTTP, битый XML — роняет
            # только этот источник
            failed[src["id"]] = f"{type(ex).__name__}: {ex}"[:300]
            log.warning("ПРОПУЩЕН источник %s — %s", src["id"], failed[src["id"]])
            continue
        log.info("%s: %d записей", src["id"], len(got))
        raw += got

    fresh, stale, taken = [], [], set()
    for it in raw:
        if it.hash in taken or is_seen(it.hash):
            continue
        taken.add(it.hash)
        dt = parse_iso(it.published_at)
        (stale if dt and dt < cutoff else fresh).append(it)

    # Самый старый свежий материал может склеиться с отданным ещё на окно раньше
    known = load_known(db, cutoff - timedelta(hours=CLUSTER_WINDOW_HOURS))
    primaries, late = cluster(fresh, known)

    stamp = iso(now)
    rows = []
    for p in primaries:
        rows.append((p.hash, p.url, p.source, p.title, p.published_at, stamp, "new", None))
        rows += [(d.hash, d.url, d.source, d.title, d.published_at, stamp, "duplicate", p.hash)
                 for d in p.duplicates]
    rows += [(d.hash, d.url, d.source, d.title, d.published_at, stamp, "duplicate", p.hash)
             for d, p in late]
    rows += [(s.hash, s.url, s.source, s.title or None, s.published_at, stamp, "stale", None)
             for s in stale]
    # Текст — только основным материалам: пайплайн берёт из состояния status = 'new'
    bodies = [(p.hash, truncate_words(p.body)) for p in primaries]
    if not dry_run:
        save(db, rows, bodies)

    primaries.sort(key=lambda it: it.published_at or "", reverse=True)
    dups = sum(len(p.duplicates) for p in primaries) + len(late)
    return Result(primaries, late, dups, len(stale), failed)


def fmt_date(it: Item) -> str:
    return (it.published_at or "дата ?")[:16].replace("T", " ")


def print_result(res: Result):
    print(f"Новых материалов: {len(res.new)}"
          f" (повторов склеено: {res.duplicates}; старше {MAX_AGE_DAYS} дн., отложено: {res.stale})")
    if res.failed:
        print(f"Источники с ошибкой: {', '.join(res.failed)}")
    for it in res.new:
        print(f"\n[{it.source}] {fmt_date(it)}  {it.title}\n    {it.url}")
        for d in it.duplicates:
            print(f"    = [{d.source}] {d.title}\n      {d.url}")
    if res.late:
        print(f"\nПовторы материалов, отданных в прошлых прогонах: {len(res.late)}")
        for d, p in res.late:
            print(f"\n[{d.source}] {fmt_date(d)}  {d.title}\n    {d.url}\n    = повтор [{p.source}] {p.title}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="ничего не записывать в состояние")
    ap.add_argument("--json", metavar="FILE", help="записать новые материалы в JSON")
    ap.add_argument("--state", default=str(STATE_PATH), help="путь к файлу состояния")
    args = ap.parse_args()

    # Консоль Windows по умолчанию не в UTF-8 — без этого кириллица и кавычки бьются
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    db = open_state(Path(args.state))
    res = collect(db, dry_run=args.dry_run)
    print_result(res)
    if args.json:
        out = {
            "new": [asdict(it) for it in res.new],
            "late_duplicates": [{**asdict(d), "primary_hash": p.hash} for d, p in res.late],
        }
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
