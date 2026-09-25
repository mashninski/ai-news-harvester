#!/usr/bin/env python3
"""
Публикация карточек в репозиторий сайта — этап 5 плана.

Берёт черновики пайплайна (output/cards/<hash>.json), приводит их к формату
сайта и открывает ОДИН pull request в mashninski-site: по файлу
content/naviny/<hash>.json на карточку. Ревью — диф этого PR, мерж —
публикация (журнал сайта, «ревью карточек — PR на GitHub, не админка»).
Заметки пайплайна (review_notes) в файл сайта не попадают: они ставятся
комментариями к строкам дифа, рядом с абзацем, к которому относятся.

    python publish.py                       # PR в main со всем, что ещё не предлагалось
    python publish.py --base naviny-test    # PR в другую ветку — проверка, не прод
    python publish.py --preview DIR         # без сети и токена: что ушло бы в PR — файлами в DIR
    python publish.py --cards DIR           # взять карточки не из output/cards

Токен — переменная окружения AI_NEWS_SITE_TOKEN или строка
AI_NEWS_SITE_TOKEN=… в .env.local рядом с этим файлом (.env.local в .gitignore).
Fine-grained token только на mashninski-site: Contents и Pull requests —
read and write. Deploy key не годится: он даёт git, а PR открывается только
через API.

Anthropic API здесь не вызывается.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

import collect
import lint
import tarask

ROOT = Path(__file__).resolve().parent
CARDS_DIR = ROOT / "output" / "cards"
ENV_FILE = ROOT / ".env.local"

TOKEN_ENV = "AI_NEWS_SITE_TOKEN"
REPO_ENV = "AI_NEWS_SITE_GITHUB"        # owner/repo — для проверки на другом репозитории
DEFAULT_REPO = "mashninski/mashninski-site"
API = "https://api.github.com"

CONTENT_DIR = "content/naviny"
# Бот создаёт ветки только с этим префиксом и никогда не двигает существующие:
# токен с Contents: write технически может писать и в main, а защиты веток
# у приватного репозитория на бесплатном плане нет (журнал сайта, этап 5)
BRANCH_PREFIX = "naviny/"

TEXT_FIELDS = ("be_title", "thesis", "summary", "retelling")
META_FIELDS = ("published_at", "vendor", "category", "importance", "sources")
ID_RE = re.compile(r"^[0-9a-f]{40}$")          # из id делается имя файла — только sha1
NOTE_FIELD_RE = re.compile(r"^(be_title|thesis|summary|retelling)( \(fix\))?: ")
QUOTED_RE = re.compile(r"«([^«»]+)»")
KIND_SUFFIX_RE = re.compile(r" \((?:імя|слова)\)$")
BODY_LIMIT = 60000                              # GitHub не принимает тело PR длиннее 65 536 знаков

log = logging.getLogger("publish")


# ---------- формат сайта ----------

def paragraphs(text: str) -> list[str]:
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def site_card(card: dict) -> dict:
    """Карточка в том виде, в каком она лежит на сайте. Тексты — наверху файла,
    чтобы диф начинался с того, что читают; пересказ — массив абзацев, по
    строке на абзац. Служебного (status, review_notes, lint, narkamauka) нет:
    файл в main и есть «опубликовано», а заметки нужны только на ревью."""
    return {
        "be_title": card["be_title"].strip(),
        "thesis": card["thesis"].strip(),
        "summary": " ".join(card["summary"].split()),
        "retelling": paragraphs(card["retelling"]),
        "published_at": card["published_at"],
        "vendor": card["vendor"],
        "vendor_name": card.get("vendor_name", ""),
        "category": card["category"],
        "importance": card["importance"],
        "sources": [{"source": s["source"], "url": s["url"], "title": s.get("title", ""),
                     "primary": bool(s.get("primary"))} for s in card["sources"]],
        "generated_at": card.get("generated_at", ""),
        "models": card.get("models", {}),
    }


def render(sc: dict) -> str:
    return json.dumps(sc, ensure_ascii=False, indent=2) + "\n"


def card_path(card_id: str) -> str:
    return f"{CONTENT_DIR}/{card_id}.json"


# ---------- проверка ----------

def check(card: dict) -> tuple[list[str], list[dict]]:
    """(почему карточку нельзя в PR, заметки для ревью). Первое — то, что правкой
    слова в дифе не исправить или что сломает сайт: оборванный текст, дата, id.
    Второе — то, что ревьюер поправит сам."""
    fatal, notes = [], []
    if not ID_RE.match(str(card.get("id", ""))):
        fatal.append(f"id не sha1: `{str(card.get('id'))[:50]}`")
    for f in TEXT_FIELDS:
        t = card.get(f)
        if not isinstance(t, str) or not t.strip():
            fatal.append(f"{f} пусты")
        elif f == "retelling" and lint.truncated(t):
            fatal.append(f"{f} абрываецца: «…{t.strip()[-40:]}»")
    for f in META_FIELDS:
        if card.get(f) in (None, "", []):
            fatal.append(f"няма {f}")
    if card.get("published_at") and collect.parse_iso(str(card["published_at"])) is None:
        fatal.append(f"дата не чытаецца: `{str(card['published_at'])[:60]}`")
    sources = card.get("sources") or []
    if not all(isinstance(s, dict) and str(s.get("url", "")).startswith("http") for s in sources):
        fatal.append("у крыніцы няма спасылкі")
    if fatal:
        return fatal, notes

    for f in TEXT_FIELDS:
        t = card[f]
        # Тезис и summary короткие: без точки в конце — или обрыв, или просто нет
        # точки, и то и другое ревьюер допишет сам. Оборванный пересказ — нет
        if f in ("thesis", "summary") and lint.truncated(t):
            notes.append({"kind": "публікацыя", "detail": f"{f}: не канчаецца канцом сказа — абарваны ці няма кропкі"})
        # Цитата в «» — чтобы заметка встала к своему абзацу пересказа (anchor)
        if m := re.search(r"<!--.*?-->|<!--", t):
            notes.append({"kind": "публікацыя", "detail": f"{f}: HTML-камэнтар «{m.group()}» — на сайце будзе бачны"})
        if m := re.search(r"\{\{name:[^}]*\}*", t):
            notes.append({"kind": "публікацыя", "detail": f"{f}: не зьнятая разьметка імя «{m.group()}»"})
        if t.count("{{") != t.count("}}"):
            notes.append({"kind": "публікацыя", "detail": f"{f}: разьметка {{{{term:…}}}} не закрытая"})
        if m := re.search(r'"[^"\n]{0,40}"?', t):
            notes.append({"kind": "публікацыя", "detail": f"{f}: простыя двукоссі «{m.group()}» — у JSON яны "
                                                          "з адваротнай касой рысай, лепш «ёлачкі»"})
    return fatal, notes


# ---------- заметки → комментарии к строкам дифа ----------

def field_lines(text: str) -> dict[str, int]:
    """Номер строки (с 1) каждого поля верхнего уровня в отрендеренной карточке."""
    out = {}
    for i, line in enumerate(text.split("\n"), 1):
        m = re.match(r'^  "(\w+)": ', line)
        if m:
            out[m.group(1)] = i
    return out


def anchor(note: dict, sc: dict, lines: dict[str, int]) -> int:
    """Строка, к которой относится заметка. Поле — из начала detail
    («retelling: …»); в пересказе — абзац, где стоит процитированное
    («было» → «стало» ищется по «стало»). Не нашлось — строка поля; заметка
    без поля (крыніца, тэрмін) — строка заголовка."""
    m = NOTE_FIELD_RE.match(note.get("detail", ""))
    if not m:
        return lines["be_title"]
    f = m.group(1)
    if f != "retelling":
        return lines[f]
    frags = [tarask.strip_names(q).rstrip("…").strip() for q in QUOTED_RE.findall(note["detail"][m.end():])]
    for frag in reversed(frags):
        if not frag:
            continue
        for i, p in enumerate(sc["retelling"]):
            if frag in p:
                return lines["retelling"] + 1 + i
    return lines["retelling"]


def note_key(note: dict) -> tuple[str, str]:
    """Одинаковая заметка из разных полей — одна: «Уілісан → Ўілісан» в тезисе,
    summary и пяти абзацах пересказа — это одно наблюдение, а не семь."""
    detail = NOTE_FIELD_RE.sub("", note.get("detail", ""))
    if note.get("kind") == "канвертар":
        detail = KIND_SUFFIX_RE.sub("", detail)
    return note.get("kind", ""), detail


def comments_for(path: str, sc: dict, text: str, notes: list[dict]) -> list[dict]:
    lines = field_lines(text)
    groups: dict[tuple, dict] = {}
    for n in notes:
        g = groups.setdefault(note_key(n), {"note": n, "count": 0, "line": anchor(n, sc, lines)})
        g["count"] += 1
    by_line: dict[int, list[str]] = {}
    for (kind, _), g in groups.items():
        detail = NOTE_FIELD_RE.sub("", g["note"]["detail"])
        times = f" ×{g['count']}" if g["count"] > 1 else ""
        by_line.setdefault(g["line"], []).append(f"- **{kind}** — {detail}{times}")
    return [{"path": path, "line": line, "side": "RIGHT", "body": "\n".join(items)}
            for line, items in sorted(by_line.items())]


# ---------- сборка PR ----------

@dataclass
class Entry:
    card: dict
    site: dict
    path: str
    text: str
    notes: list[dict]
    comments: list[dict] = field(default_factory=list)


@dataclass
class Plan:
    entries: list[Entry]
    skipped: list[tuple[str, str, list[str]]]      # (id, заголовок, причины)
    branch: str
    title: str
    message: str
    body: str


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def cards_word(n: int) -> str:
    return f"{n} {plural(n, 'картка', 'карткі', 'картак')}"


def load_cards(d: Path) -> tuple[list[dict], list[tuple[str, str, list[str]]]]:
    cards, broken = [], []
    for p in sorted(d.glob("*.json")):
        try:
            cards.append(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, UnicodeDecodeError) as e:
            broken.append((p.stem, "", [f"файл не чытаецца як JSON: {e}"]))
    return cards, broken


def md_cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def date_be(value: str) -> str:
    dt = collect.parse_iso(value)
    return dt.strftime("%d.%m.%Y") if dt else md_cell(value)


def make_body(entries: list[Entry], skipped, base: str, when: datetime) -> str:
    if base == "main":
        merge = "**Merge — гэта публікацыя:** што трапіла ў `main`, тое на сайце."
    else:
        merge = (f"**Merge у `{base}` — не прод:** карткі трапяць толькі ў прэв'ю Vercel "
                 f"гэтай галіны. У `main` гэты PR не зьліваць.")
    out = [
        f"Карткі ШІ-навін на праверку — {cards_word(len(entries))}. {merge}",
        "",
        "- **Чытаць** — укладка «Files changed»: адна картка — адзін файл "
        f"`{CONTENT_DIR}/<id>.json`. Зьверху файла загаловак, тэзіс, кароткі зьмест "
        "і пераказ, па радку на абзац. Заўвагі пайплайна — камэнтарамі каля свайго радка.",
        "- **Правіць** — у файле «⋯» → «Edit file», выправіць і закамітаць у гэтую ж "
        "галіну. Двукоссі ў тэксьце толькі «ёлачкі»: простае `\"` ламае JSON. "
        "У разьметцы `{{term:слаг|форма}}` мяняецца толькі форма пасьля `|`.",
        "- **Адхіліць картку** — у файле «⋯» → «Delete file». Бот яе больш не прапануе.",
        "",
        "| # | Файл | Загаловак | Крыніца | Дата | Заўвагі |",
        "|---|---|---|---|---|---|",
    ]
    for i, e in enumerate(entries, 1):
        src = next((s for s in e.site["sources"] if s["primary"]), e.site["sources"][0])
        n = sum(c["body"].count("\n") + 1 for c in e.comments)   # после склейки одинаковых
        out.append(f"| {i} | `{e.card['id'][:12]}` | {md_cell(e.site['be_title'])} | "
                   f"[{md_cell(src['source'])}]({src['url']}) | {date_be(e.site['published_at'])} | "
                   f"{n or '—'} |")
    if skipped:
        out += ["", f"### Не ўвайшлі — {len(skipped)}", "",
                "Гэтых картак у PR няма: такую памылку не выправіць праўкай слова ў дыфе.", ""]
        for cid, title, reasons in skipped:
            label = f" «{md_cell(title)}»" if title else ""
            out.append(f"- `{cid[:12]}`{label} — {'; '.join(reasons)}")
    models = sorted({m for e in entries for m in e.site["models"].values()})
    out += ["", f"<sub>ai-news-harvester · publish.py · {collect.iso(when)} · "
                f"мадэлі: {', '.join(models) or '—'}</sub>"]
    body = "\n".join(out) + "\n"
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "\n\n…апісаньне абрэзана: GitHub не бярэ даўжэй.\n"
    return body


def plan_pr(cards: list[dict], broken: list, done: set[str], base: str, when: datetime) -> Plan:
    """Всё, что уйдёт в PR, без сети: файлы, комментарии, тело. done — уже
    предложенные или уже лежащие на сайте id, они пропускаются молча."""
    entries, skipped = [], list(broken)
    for c in cards:
        if c.get("id") in done:
            continue
        fatal, extra = check(c)
        if fatal:
            skipped.append((str(c.get("id", "?")), str(c.get("be_title") or ""), fatal))
            continue
        sc = site_card(c)
        path = card_path(c["id"])
        text = render(sc)
        notes = list(c.get("review_notes") or []) + extra
        e = Entry(c, sc, path, text, notes)
        e.comments = comments_for(path, sc, text, notes)
        entries.append(e)
    entries.sort(key=lambda e: e.path)       # как GitHub показывает файлы в «Files changed»
    stamp = when.strftime("%Y-%m-%d-%H%M")
    n = len(entries)
    return Plan(
        entries=entries,
        skipped=skipped,
        branch=f"{BRANCH_PREFIX}{stamp}",
        title=f"ШІ-навіны: {cards_word(n)} на праверку, {when.strftime('%d.%m.%Y')}",
        message=f"навіны: {cards_word(n)} на праверку",
        body=make_body(entries, skipped, base, when),
    )


def notes_markdown(plan: Plan) -> str:
    """Запасной вид заметок — в тело PR, если комментарии к строкам не встали."""
    out = ["", "### Заўвагі пайплайна", ""]
    for e in plan.entries:
        if e.comments:
            out.append(f"**`{e.card['id'][:12]}`** {md_cell(e.site['be_title'])}")
            out += [c["body"] for c in e.comments]
            out.append("")
    return "\n".join(out)


def write_preview(plan: Plan, d: Path):
    for e in plan.entries:
        p = d / e.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(e.text, encoding="utf-8")
    (d / "pr.md").write_text(f"# {plan.title}\n\nГаліна: `{plan.branch}`\n\n{plan.body}", encoding="utf-8")
    (d / "comments.json").write_text(
        json.dumps([c for e in plan.entries for c in e.comments], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


# ---------- GitHub ----------

class GitHubError(RuntimeError):
    pass


class GitHub:
    """REST API GitHub: коммит через Git Data API, без локального клона —
    рабочая копия сайта у автора не трогается."""

    def __init__(self, repo: str, token: str, session: requests.Session | None = None):
        self.repo = repo
        self.s = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ai-news-harvester"}

    def call(self, method: str, path: str, body: dict | None = None, missing_ok: bool = False):
        r = self.s.request(method, f"{API}/repos/{self.repo}{path}", headers=self.headers,
                           json=body, timeout=30)
        if missing_ok and r.status_code == 404:
            return None
        if r.status_code >= 300:
            raise GitHubError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    def head(self, branch: str) -> str:
        ref = self.call("GET", f"/git/ref/heads/{branch}", missing_ok=True)
        if ref is None:
            raise GitHubError(f"ветки {branch} нет в {self.repo}")
        return ref["object"]["sha"]

    def published_ids(self, base: str) -> set[str]:
        """id карточек, которые уже лежат в content/naviny ветки base. Через
        деревья, а не Contents API: тот отдаёт не больше 1000 файлов папки."""
        tree = self.call("GET", f"/git/commits/{self.head(base)}")["tree"]["sha"]
        for part in CONTENT_DIR.split("/"):
            entries = self.call("GET", f"/git/trees/{tree}")["tree"]
            tree = next((t["sha"] for t in entries if t["path"] == part and t["type"] == "tree"), None)
            if tree is None:
                return set()
        return {t["path"][:-5] for t in self.call("GET", f"/git/trees/{tree}")["tree"]
                if t["path"].endswith(".json")}

    def open_pr(self, plan: Plan, base: str) -> tuple[int, str, str]:
        if not plan.branch.startswith(BRANCH_PREFIX):
            raise GitHubError(f"бот пишет только в ветки {BRANCH_PREFIX}*")
        parent = self.head(base)
        base_tree = self.call("GET", f"/git/commits/{parent}")["tree"]["sha"]
        tree = self.call("POST", "/git/trees", {
            "base_tree": base_tree,
            "tree": [{"path": e.path, "mode": "100644", "type": "blob", "content": e.text} for e in plan.entries],
        })["sha"]
        commit = self.call("POST", "/git/commits", {"message": plan.message, "tree": tree, "parents": [parent]})["sha"]
        # POST, не PATCH: создаётся новая ветка; существующую GitHub не даст перезаписать (422)
        self.call("POST", "/git/refs", {"ref": f"refs/heads/{plan.branch}", "sha": commit})
        pr = self.call("POST", "/pulls", {"title": plan.title, "head": plan.branch, "base": base,
                                          "body": plan.body})
        return pr["number"], pr["html_url"], commit

    def comment_lines(self, number: int, commit: str, comments: list[dict]):
        self.call("POST", f"/pulls/{number}/reviews", {
            "commit_id": commit, "event": "COMMENT",
            "body": "Заўвагі пайплайна — каля радкоў, да якіх яны адносяцца.",
            "comments": comments,
        })

    def append_body(self, number: int, body: str):
        self.call("PATCH", f"/pulls/{number}", {"body": body})


# ---------- состояние ----------

def done_ids(db: sqlite3.Connection) -> set[str]:
    return {h for (h,) in db.execute("SELECT hash FROM publication")}


def record(db: sqlite3.Connection, plan: Plan, number: int | None):
    now = collect.iso(datetime.now(timezone.utc))
    for e in plan.entries:
        db.execute("INSERT OR REPLACE INTO publication VALUES (?, 'proposed', ?, ?, NULL, ?)",
                   (e.card["id"], number, plan.branch, now))
    for cid, _, reasons in plan.skipped:
        if ID_RE.match(cid):
            db.execute("INSERT OR REPLACE INTO publication VALUES (?, 'skipped', ?, ?, ?, ?)",
                       (cid, number, plan.branch, "; ".join(reasons)[:500], now))
    db.commit()


# ---------- запуск ----------

def load_env_file(path: Path = ENV_FILE):
    """KEY=VALUE из .env.local; уже заданная переменная окружения главнее."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def publish(plan: Plan, gh: GitHub, db: sqlite3.Connection, base: str) -> tuple[int, str, bool]:
    """Открывает PR и ставит заметки. Возвращает (номер, ссылка, заметки встали)."""
    number, url, commit = gh.open_pr(plan, base)
    # Сразу в состояние: даже если дальше что-то упадёт, эти карточки второй раз не уйдут
    record(db, plan, number)
    comments = [c for e in plan.entries for c in e.comments]
    if not comments:
        return number, url, True
    try:
        gh.comment_lines(number, commit, comments)
        return number, url, True
    except GitHubError as e:
        log.warning("комментарии к строкам не встали (%s) — заметки дописаны в описание PR", e)
        gh.append_body(number, (plan.body + notes_markdown(plan))[:BODY_LIMIT])
        return number, url, False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cards", default=str(CARDS_DIR), help="папка с карточками пайплайна")
    ap.add_argument("--base", default="main", help="ветка сайта, в которую PR")
    ap.add_argument("--state", default=str(collect.STATE_PATH), help="путь к файлу состояния")
    ap.add_argument("--preview", metavar="DIR", help="без сети: записать в DIR файлы, тело PR и комментарии")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    when = datetime.now(timezone.utc)
    cards, broken = load_cards(Path(args.cards))
    log.info("Карточек в %s: %d", args.cards, len(cards) + len(broken))

    if args.preview:
        plan = plan_pr(cards, broken, set(), args.base, when)
        write_preview(plan, Path(args.preview))
        log.info("В PR ушло бы: %d, не вошло бы: %d. Записано в %s", len(plan.entries), len(plan.skipped),
                 args.preview)
        return

    load_env_file()
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        sys.exit(f"не задан {TOKEN_ENV} — токен GitHub на репозиторий сайта (переменная или .env.local)")
    gh = GitHub(os.environ.get(REPO_ENV) or DEFAULT_REPO, token)
    db = collect.open_state(Path(args.state))

    done = done_ids(db) | gh.published_ids(args.base)
    plan = plan_pr(cards, broken, done, args.base, when)
    for cid, title, reasons in plan.skipped:
        log.warning("НЕ ВОШЛА %s %s: %s", cid[:12], title[:60], "; ".join(reasons))
    if not plan.entries:
        log.info("Новых карточек нет — PR не открываю")
        return
    number, url, ok = publish(plan, gh, db, args.base)
    log.info("PR #%d: %s — %s", number, url, cards_word(len(plan.entries)))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
