"""Публикация (этап 5) без сети: формат карточки для сайта, проверка,
заметки → комментарии к строкам дифа, тело PR и весь разговор с GitHub
на поддельной сессии. Карточки — выдуманные: настоящие черновики лежат
в приватном репозитории сайта и сюда не копируются."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

import collect
import publish

WHEN = datetime(2026, 9, 25, 8, 30, tzinfo=timezone.utc)


def card(n: int = 1, **over) -> dict:
    c = {
        "id": f"{n:040x}",
        "status": "draft",
        "published_at": "2026-09-22T12:00:00Z",
        "vendor": "openai",
        "vendor_name": "OpenAI",
        "category": "release",
        "importance": 2,
        "sources": [{"source": "openai", "url": f"https://openai.com/news/{n}", "title": "Title", "primary": True}],
        "be_title": f"Загаловак {n}",
        "thesis": "Тэзіс аднім сказам.",
        "summary": "Кароткі зьмест.\nУ два радкі.",
        "retelling": "Першы абзац пра {{term:token|токены}}.\n\nДругі абзац. Саймон Ўілісан лічыць, што гэта так.\n\nТрэці.",
        "review_notes": [],
        "lint": [{"field": "retelling"}],
        "narkamauka": {"be_title": "…"},
        "generated_at": "2026-09-25T08:04:06Z",
        "models": {"triage": "claude-haiku-4-5", "generate": "claude-sonnet-5"},
    }
    c.update(over)
    return c


# ---------- формат сайта ----------

def test_site_card_texts_first_paragraphs_as_lines_no_service_fields():
    sc = publish.site_card(card())
    assert list(sc)[:4] == ["be_title", "thesis", "summary", "retelling"]
    assert sc["retelling"] == ["Першы абзац пра {{term:token|токены}}.",
                               "Другі абзац. Саймон Ўілісан лічыць, што гэта так.", "Трэці."]
    assert sc["summary"] == "Кароткі зьмест. У два радкі."
    for gone in ("id", "status", "review_notes", "lint", "narkamauka"):
        assert gone not in sc
    text = publish.render(sc)
    assert "\\u" not in text and "Загаловак 1" in text     # кириллица как есть, диф читается
    assert text.split("\n")[5].strip().startswith('"Першы абзац')   # абзац — своя строка


# ---------- проверка ----------

@pytest.mark.parametrize("over, reason", [
    ({"retelling": "Абзац. Gemini Robotics ER 2 працуе як"}, "retelling абрываецца"),
    ({"published_at": "2026-09T12:00:00Z"}, "дата не чытаецца"),
    ({"id": "../../src/app/page"}, "id не sha1"),
    ({"summary": ""}, "summary пусты"),
    ({"sources": [{"source": "x", "url": "javascript:alert(1)"}]}, "няма спасылкі"),
])
def test_check_fatal(over, reason):
    fatal, _ = publish.check(card(**over))
    assert any(reason in f for f in fatal), fatal


def test_check_notes_for_what_reviewer_fixes_himself():
    c = card(thesis="Тэзіс без кропкі",
             retelling='Абзац.\n\nПра рэжым "max" і <!-- guard -->SVG.\n\n{{name:Саймон Ўілісан}} сказаў.')
    fatal, notes = publish.check(c)
    assert fatal == []
    details = [n["detail"] for n in notes]
    assert any(d.startswith("thesis: не канчаецца") for d in details)
    assert any("«<!-- guard -->»" in d for d in details)
    assert any('«"max"»' in d for d in details)
    assert any("«{{name:Саймон Ўілісан}}»" in d for d in details)


# ---------- заметки → комментарии ----------

def test_notes_anchor_to_their_paragraph_and_duplicates_merge():
    notes = [
        {"kind": "лінтар", "detail": "retelling: засталося лічыцца — «{{name:Саймон Ўілісан}} лічыць, што гэ»"},
        {"kind": "канвертар", "detail": "summary: «Саймон Уілісан» → «Саймон Ўілісан» (імя)"},
        {"kind": "канвертар", "detail": "retelling: «Саймон Уілісан» → «Саймон Ўілісан» (імя)"},
        {"kind": "канвертар", "detail": "retelling (fix): «Саймон Уілісан» → «Саймон Ўілісан»"},
        {"kind": "крыніца", "detail": "тэкст дакачаны са старонкі: у фідзе было мала"},
    ]
    sc = publish.site_card(card())
    text = publish.render(sc)
    lines = text.split("\n")
    comments = publish.comments_for("content/naviny/x.json", sc, text, notes)
    by_line = {c["line"]: c["body"] for c in comments}
    # лінтар — у абзаца, где стоит цитата (маркер имени в цитате снят)
    lint_line = next(ln for ln, b in by_line.items() if "лінтар" in b)
    assert "Саймон Ўілісан лічыць" in lines[lint_line - 1]
    # одно наблюдение конвертера из трёх полей — один комментарий, ×3
    conv = [b for b in by_line.values() if "канвертар" in b]
    assert len(conv) == 1 and "×3" in conv[0]
    # заметка без поля — к заголовку
    title_line = next(ln for ln, b in by_line.items() if "крыніца" in b)
    assert lines[title_line - 1].startswith('  "be_title"')
    assert all(c["side"] == "RIGHT" and c["path"] == "content/naviny/x.json" for c in comments)


# ---------- план PR ----------

def test_plan_skips_done_and_broken_lists_skipped_in_body():
    cards = [card(3), card(1), card(2, retelling="Абарваны як"), card(4)]
    plan = publish.plan_pr(cards, [("zzz", "", ["файл не чытаецца як JSON"])], {f"{4:040x}"}, "main", WHEN)
    assert [e.card["id"] for e in plan.entries] == [f"{1:040x}", f"{3:040x}"]   # по пути, как в «Files changed»
    assert plan.branch == "naviny/2026-09-25-0830"
    assert plan.title == "ШІ-навіны: 2 карткі на праверку, 25.09.2026"
    assert plan.message == "навіны: 2 карткі на праверку"
    assert "Не ўвайшлі — 2" in plan.body and "retelling абрываецца" in plan.body and "`zzz`" in plan.body
    assert "Merge — гэта публікацыя" in plan.body


def test_body_for_test_branch_warns_not_prod_and_escapes_table():
    plan = publish.plan_pr([card(1, be_title="A | B")], [], set(), "naviny-test", WHEN)
    assert "не прод" in plan.body and "`naviny-test`" in plan.body
    assert "A \\| B" in plan.body
    # список, а не таблица: на телефоне таблица шире экрана
    assert "1. **A \\| B** · без заўваг · [openai](https://openai.com/news/1) · 22.09.2026 · `" in plan.body
    assert "| # |" not in plan.body


def test_plural():
    assert [publish.cards_word(n) for n in (1, 3, 5, 11, 21, 24)] == \
        ["1 картка", "3 карткі", "5 картак", "11 картак", "21 картка", "24 карткі"]


def test_preview_writes_files_body_and_comments(tmp_path):
    notes = [{"kind": "лінтар", "detail": "thesis: засталося x — «Тэзіс»"}]
    plan = publish.plan_pr([card(1, review_notes=notes)], [], set(), "main", WHEN)
    publish.write_preview(plan, tmp_path)
    f = tmp_path / "content" / "naviny" / f"{1:040x}.json"
    assert json.loads(f.read_text(encoding="utf-8"))["be_title"] == "Загаловак 1"
    assert "Загаловак 1" in (tmp_path / "pr.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "comments.json").read_text(encoding="utf-8"))[0]["line"] == 3


def test_load_cards_reports_unreadable_file(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(card(1)), encoding="utf-8")
    (tmp_path / "b.json").write_text("{битый", encoding="utf-8")
    cards, broken = publish.load_cards(tmp_path)
    assert len(cards) == 1 and broken[0][0] == "b"


def test_env_file_does_not_override_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env.local"
    env.write_text('# каментар\nAI_NEWS_SITE_TOKEN="from-file"\nOTHER_X=1\n', encoding="utf-8")
    monkeypatch.delenv("OTHER_X", raising=False)
    monkeypatch.setenv("AI_NEWS_SITE_TOKEN", "from-env")
    publish.load_env_file(env)
    import os
    assert os.environ["AI_NEWS_SITE_TOKEN"] == "from-env" and os.environ["OTHER_X"] == "1"


# ---------- GitHub на поддельной сессии ----------

class FakeSession:
    """Отвечает по таблице (метод, конец пути) → (код, json). Всё записывает."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, headers=None, json=None, timeout=None):
        path = url.split("/repos/o/r", 1)[1]
        self.calls.append((method, path, json))
        assert headers["Authorization"] == "Bearer t"
        for (m, suffix), (code, body) in self.routes.items():
            if m == method and path.endswith(suffix):
                return NS(status_code=code, content=b"x" if body is not None else b"",
                          json=lambda body=body: body, text=str(body))
        return NS(status_code=404, content=b"x", json=lambda: {"message": "Not Found"}, text="Not Found")


def routes(review=(200, {"id": 1})):
    return {
        ("GET", "/git/ref/heads/main"): (200, {"object": {"sha": "base"}}),
        ("GET", "/git/commits/base"): (200, {"tree": {"sha": "root"}}),
        ("POST", "/git/trees"): (201, {"sha": "tree2"}),
        ("POST", "/git/commits"): (201, {"sha": "c2"}),
        ("POST", "/git/refs"): (201, {"ref": "x"}),
        ("POST", "/pulls"): (201, {"number": 7, "html_url": "https://github.com/o/r/pull/7"}),
        ("POST", "/pulls/7/reviews"): review,
        ("PATCH", "/pulls/7"): (200, {"number": 7}),
    }


def plan_with_note():
    notes = [{"kind": "лінтар", "detail": "retelling: засталося x — «Трэці.»"}]
    return publish.plan_pr([card(1, review_notes=notes), card(2)], [("bad" * 13 + "a", "", ["x"])],
                           set(), "main", WHEN)


def test_open_pr_new_branch_one_commit_then_line_comments(tmp_path):
    s = FakeSession(routes())
    gh = publish.GitHub("o/r", "t", s)
    db = collect.open_state(tmp_path / "state.sqlite")
    plan = plan_with_note()
    number, url, ok = publish.publish(plan, gh, db, "main")
    assert (number, ok) == (7, True)
    methods = [(m, p) for m, p, _ in s.calls]
    assert methods == [("GET", "/git/ref/heads/main"), ("GET", "/git/commits/base"), ("POST", "/git/trees"),
                       ("POST", "/git/commits"), ("POST", "/git/refs"), ("POST", "/pulls"),
                       ("POST", "/pulls/7/reviews")]
    body = {p: b for m, p, b in s.calls}
    assert body["/git/trees"]["base_tree"] == "root"
    assert [t["path"] for t in body["/git/trees"]["tree"]] == [f"content/naviny/{1:040x}.json",
                                                              f"content/naviny/{2:040x}.json"]
    assert body["/git/commits"]["parents"] == ["base"]
    assert body["/git/refs"] == {"ref": "refs/heads/naviny/2026-09-25-0830", "sha": "c2"}
    assert body["/pulls"]["base"] == "main" and body["/pulls"]["head"] == "naviny/2026-09-25-0830"
    review = body["/pulls/7/reviews"]
    assert review["commit_id"] == "c2" and review["event"] == "COMMENT"
    assert review["comments"][0]["path"] == f"content/naviny/{1:040x}.json"
    # предложенные и отсеянные — в состоянии, второй раз не пойдут
    assert publish.done_ids(db) == {f"{1:040x}", f"{2:040x}", "bad" * 13 + "a"}
    again = publish.plan_pr([card(1), card(2)], [], publish.done_ids(db), "main", WHEN)
    assert again.entries == []


def test_failed_line_comments_fall_back_to_body_and_state_is_kept(tmp_path):
    s = FakeSession(routes(review=(422, {"message": "line must be part of the diff"})))
    gh = publish.GitHub("o/r", "t", s)
    db = collect.open_state(tmp_path / "state.sqlite")
    number, _, ok = publish.publish(plan_with_note(), gh, db, "main")
    assert (number, ok) == (7, False)
    patch = [b for m, p, b in s.calls if m == "PATCH"][0]
    assert "Заўвагі пайплайна" in patch["body"] and "Трэці." in patch["body"]
    assert f"{1:040x}" in publish.done_ids(db)


def test_bot_refuses_branch_outside_prefix():
    plan = plan_with_note()
    plan.branch = "main"
    with pytest.raises(publish.GitHubError):
        publish.GitHub("o/r", "t", FakeSession(routes())).open_pr(plan, "main")


def test_published_ids_from_tree_and_missing_folder():
    r = {
        ("GET", "/git/ref/heads/main"): (200, {"object": {"sha": "base"}}),
        ("GET", "/git/commits/base"): (200, {"tree": {"sha": "root"}}),
        ("GET", "/git/trees/root"): (200, {"tree": [{"path": "content", "type": "tree", "sha": "c"}]}),
        ("GET", "/git/trees/c"): (200, {"tree": [{"path": "naviny", "type": "tree", "sha": "n"},
                                                 {"path": "posts.json", "type": "blob", "sha": "p"}]}),
        ("GET", "/git/trees/n"): (200, {"tree": [{"path": f"{5:040x}.json", "type": "blob", "sha": "a"}]}),
    }
    assert publish.GitHub("o/r", "t", FakeSession(r)).published_ids("main") == {f"{5:040x}"}
    r[("GET", "/git/trees/c")] = (200, {"tree": [{"path": "posts.json", "type": "blob", "sha": "p"}]})
    assert publish.GitHub("o/r", "t", FakeSession(r)).published_ids("main") == set()


def test_missing_base_branch_is_an_error():
    with pytest.raises(publish.GitHubError, match="ветки nope нет"):
        publish.GitHub("o/r", "t", FakeSession({})).head("nope")
