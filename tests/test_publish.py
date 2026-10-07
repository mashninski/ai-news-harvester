"""Публикация (этап 5) без сети: формат карточки для сайта, проверка,
заметки → комментарии к строкам дифа, тело PR и весь разговор с GitHub
на поддельной сессии. Карточки — выдуманные: настоящие черновики лежат
в приватном репозитории сайта и сюда не копируются. Исключение — тексты
пяти карточек первого PR бота (fixtures/naviny_pr1): на них подобран порог
повтора тезиса (этап 8б, журнал сайта, 03.10.2026); PR был открыт в main,
четыре из пяти опубликованы на сайте."""

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
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


# ---------- заметки о содержании ----------
# Пять карточек первого PR в main — коммит бота 306889a в ветке
# naviny/2026-10-03-0540 сайта, до ручной правки; только тексты. Разбор
# журнала сайта (03.10.2026): первый абзац пересказа повторяет тезис у трёх,
# summary — у одной, относительные даты — у d02470a, длина вне нормы — у трёх

PR1 = Path(__file__).resolve().parent / "fixtures" / "naviny_pr1"


def pr1_cards() -> dict[str, dict]:
    out = {}
    for p in sorted(PR1.glob("*.json")):
        c = json.loads(p.read_text(encoding="utf-8"))
        c["retelling"] = "\n\n".join(c["retelling"])      # в черновике пайплайна пересказ — строкой
        out[p.stem[:7]] = c
    return out


def notes_of(c: dict, start: str) -> list[str]:
    return [n["detail"] for n in publish.content_notes(c) if n["detail"].startswith(start)]


def test_repeat_threshold_on_first_pr():
    cards = pr1_cards()
    first = {k for k, c in cards.items() if notes_of(c, "retelling: першы абзац паўтарае тэзіс")}
    summary = {k for k, c in cards.items() if notes_of(c, "summary: паўтарае тэзіс")}
    assert first == {"b2497ef", "c798f2b", "ce0a680"}
    assert summary == {"c798f2b"}
    # Запас по обе стороны порога: повторы и не повторы не жмутся к нему
    shares = {k: publish.repeat_share(c["thesis"], publish.paragraphs(c["retelling"])[0]) for k, c in cards.items()}
    assert min(shares[k] for k in first) >= publish.REPEAT_SHARE + 0.04
    assert max(v for k, v in shares.items() if k not in first) <= publish.REPEAT_SHARE - 0.06


def test_dates_and_length_on_first_pr():
    cards = pr1_cards()
    dates = {k: notes_of(c, "") for k, c in cards.items()}
    dates = {k: [d for d in v if "адносная дата" in d] for k, v in dates.items()}
    assert {k for k, v in dates.items() if v} == {"d02470a"}
    found = " ".join(dates["d02470a"])
    for phrase in ("«у пятніцу»", "«апошнія выхадныя»", "«Два месяцы таму»", "«апошні тыдзень»", "«На выхадных»"):
        assert phrase in found
    long_short = {k: notes_of(c, "retelling: ") for k, c in cards.items()}
    long_short = {k: [d for d in v if "норма 120–220" in d] for k, v in long_short.items()}
    assert {k for k, v in long_short.items() if v} == {"c773f1b", "c798f2b", "d02470a"}
    assert long_short["c798f2b"] == ["retelling: 71 слова — норма 120–220"]


@pytest.mark.parametrize("text", [
    "у панядзелак", "ў аўторак", "у сераду", "у чацвер", "У пятніцу", "у суботу", "у нядзелю",
    "да пятніцы", "учора", "ўчора", "Сёння", "сёньня", "заўтра", "пазаўчора", "паслязаўтра", "учорашні",
    "на выхадных", "у выхадныя", "на гэтым тыдні", "на мінулым тыдні", "на наступным тыдні",
    "апошні тыдзень", "у мінулым месяцы", "у гэтым годзе", "тры дні таму", "два тыдні таму",
    "пяць месяцаў таму", "год таму", "сёлета", "летась", "днямі", "на днях",
])
def test_relative_date_found(text):
    assert publish.REL_DATE_RE.search(f"Нешта {text} здарылася."), text


@pytest.mark.parametrize("text", [
    "ліміт выхадных токенаў", "выхадныя дадзеныя", "наступная мадэль", "апошняя версія",
    "DevDay пройдзе 29 верасня", "раней", "тыдзень з лішнім", "сераднія вынікі", "аўтар артыкула",
    "у 2026 годзе", "з мая па ліпень",
])
def test_relative_date_not_found(text):
    assert not publish.REL_DATE_RE.search(f"Нешта {text} здарылася."), text


def test_length_norm_comes_from_prompt():
    lo, hi = publish.prompts.RETELLING_WORDS
    assert f"{lo}–{hi} слоў" in publish.prompts.GENERATE_TASK
    ok = "\n\n".join(["Слова " * 49 + "канец."] * 3)                                # 150 слоў
    assert notes_of(card(retelling=ok), "retelling: ") == []
    dashes = "Слова — " * (hi + 5)                                      # тире словам не лічыцца
    assert notes_of(card(retelling=dashes.strip()), "retelling: ") == [f"retelling: {hi + 5} слоў — норма {lo}–{hi}"]


def test_content_notes_go_to_pr_comments_not_into_check():
    c = pr1_cards()["d02470a"]
    full = card(5, **c)
    fatal, notes = publish.check(full)
    assert fatal == [] and not any("адносная дата" in n["detail"] for n in notes)   # check() — как на сайце
    plan = publish.plan_pr([full], [], set(), "main", WHEN)
    e = plan.entries[0]
    bodies = "\n".join(x["body"] for x in e.comments)
    assert "адносная дата «у пятніцу»" in bodies and "351 слова" in bodies
    # Заметка о дате в пересказе — у своего абзаца
    lines = e.text.split("\n")
    two_months = next(x for x in e.comments if "Два месяцы таму" in x["body"])
    assert "Два месяцы таму" in lines[two_months["line"] - 1]
    # Повтор тезиса — у первого абзаца пересказа
    repeat = publish.plan_pr([card(6, **pr1_cards()["b2497ef"])], [], set(), "main", WHEN).entries[0]
    line = next(x["line"] for x in repeat.comments if "першы абзац паўтарае тэзіс" in x["body"])
    assert repeat.text.split("\n")[line - 1].strip().startswith('"Anthropic Frontier Red Team праверыла')


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
    normal = "\n\n".join(["Слова " * 49 + "канец."] * 3)      # пераказ у норме даўжыні — без заўваг
    plan = publish.plan_pr([card(1, be_title="A | B", retelling=normal)], [], set(), "naviny-test", WHEN)
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


def test_drafts_of_a_day_go_into_one_pr_and_nothing_is_lost(tmp_path):
    # Один PR за цикл: черновики прогонов копятся в output/cards (между
    # прогонами — кэш Actions) и все уходят в PR прогона 05:17 или 17:17
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    db = collect.open_state(tmp_path / "state.sqlite")
    for run in range(3):                                   # три прогона без публикации
        for n in (2 * run + 1, 2 * run + 2):
            (cards_dir / f"{n:040x}.json").write_text(json.dumps(card(n), ensure_ascii=False), encoding="utf-8")
    cards, broken = publish.load_cards(cards_dir)
    s = FakeSession(routes())
    plan = publish.plan_pr(cards, broken, publish.done_ids(db), "main", WHEN)
    publish.publish(plan, publish.GitHub("o/r", "t", s), db, "main")
    assert len(plan.entries) == 6 and len([c for c in s.calls if c[1] == "/pulls"]) == 1
    # Назавтра: в PR только новое, предложенное вчера второй раз не идёт, файлы не удалялись
    (cards_dir / f"{7:040x}.json").write_text(json.dumps(card(7), ensure_ascii=False), encoding="utf-8")
    cards, broken = publish.load_cards(cards_dir)
    assert len(cards) == 7
    plan = publish.plan_pr(cards, broken, publish.done_ids(db), "main", WHEN)
    assert [e.card["id"] for e in plan.entries] == [f"{7:040x}"]


def test_workflow_publishes_twice_a_day_or_by_hand():
    wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "harvest.yml").read_text(encoding="utf-8")
    # Прогон публикации узнаётся по строке расписания: она должна совпадать
    # в списке cron и в case шага «Вид прогона», других прогонов в 05 и 17 нет.
    # Полная сверка видов и часов — test_workflow_two_cycles_a_day в test_pipeline.py
    assert '- cron: "47 5,17 * * *"' in wf and '"47 5,17 * * *")  kind=publish' in wf
    assert "options: [collect, generate, fix, publish]" in wf and "env.PUBLISH == 'true'" in wf


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


# ---------- та же проверка в дашборде сайта ----------
# check() и paragraphs() повторены на TypeScript в репозитории сайта
# (src/lib/naviny.ts: publishCheck, paragraphs) — ими дашборд проверяет
# правку уже опубликованной карточки перед коммитом в main. Тест гоняет
# одни и те же карточки через обе копии и требует одинаковый ответ,
# до буквы: две копии правил не разойдутся молча.

SITE_NAVINY_TS = (Path(os.environ.get("AI_NEWS_SITE_REPO") or publish.ROOT.parent / "mashninski-site")
                  / "src" / "lib" / "naviny.ts")


def _node_runs_ts() -> bool:
    if shutil.which("node") is None or not SITE_NAVINY_TS.exists():
        return False
    probe = subprocess.run(["node", "--experimental-strip-types", "--no-warnings",
                            "--input-type=module-typescript", "-e", "const x: number = 1"], capture_output=True)
    return probe.returncode == 0


needs_site_ts = pytest.mark.skipif(not _node_runs_ts(),
                                   reason="нет репозитория сайта рядом или node без поддержки TypeScript (нужен 22.6+)")

PARITY_CASES = [
    card(),
    card(retelling="Абзац. Gemini Robotics ER 2 працуе як"),
    card(published_at="2026-09T12:00:00Z"),
    card(published_at="2026-09"),
    card(published_at="2026-09-22 12:00"),
    card(id="../../src/app/page"),
    card(summary=""),
    card(be_title="   "),
    card(sources=[]),
    card(sources=[{"source": "x", "url": "javascript:alert(1)"}]),
    card(importance=0),
    card(vendor=None),
    card(thesis="Тэзіс без кропкі",
         retelling='Абзац.\n\nПра рэжым "max" і <!-- guard -->SVG.\n\n{{name:Саймон Ўілісан}} сказаў.'),
    card(thesis="Тэзіс пра {{term:token|токены} без дужкі.", summary="Цытата: «так».)"),
    card(summary="Канец з цытатай.»", retelling="Абзац з <!-- незакрытым камэнтаром."),
    # Английский текст для /en/ai-naviny (ai-news-en-spec.md сайта, §3)
    card(en_title="A title", en_thesis="A thesis.", en_retelling="One.\n\nTwo."),
    card(en_title="Only a title"),
    card(en_title="A", en_thesis="B.", en_retelling="One.\n\nCut in the mid"),
    card(en_title="A title.", en_thesis="No period", en_retelling="With <!-- x --> comment.\n\nTwo."),
    card(en_title="A", en_thesis="A {{term:x|y}} thesis.", en_retelling="One."),
    card(en_title="A", en_thesis="   ", en_retelling="One."),
]


@needs_site_ts
def test_check_same_as_site_dashboard(tmp_path):
    script = tmp_path / "parity.mts"
    script.write_text(
        f'import {{ publishCheck, paragraphs }} from {json.dumps(SITE_NAVINY_TS.as_uri())};\n'
        'import fs from "node:fs";\n'
        'const cases = JSON.parse(fs.readFileSync(0, "utf8"));\n'
        'console.log(JSON.stringify(cases.map((c) => ({ ...publishCheck(c), paragraphs: paragraphs(c.retelling) }))));\n',
        encoding="utf-8")
    out = subprocess.run(["node", "--experimental-strip-types", "--no-warnings", str(script)],
                         input=json.dumps(PARITY_CASES, ensure_ascii=False), capture_output=True,
                         text=True, encoding="utf-8", check=True)
    site = json.loads(out.stdout)
    for c, got in zip(PARITY_CASES, site):
        fatal, notes = publish.check(c)
        assert got == {"fatal": fatal, "notes": [n["detail"] for n in notes],
                       "paragraphs": publish.paragraphs(c["retelling"])}, c


def test_merge_squashes_with_head_sha_and_failure_keeps_state(tmp_path):
    r = routes(); r[("PUT", "/pulls/7/merge")] = (200, {"merged": True})
    s = FakeSession(r)
    db = collect.open_state(tmp_path / "state.sqlite")
    number, _, ok = publish.publish(plan_with_note(), publish.GitHub("o/r", "t", s), db, "main", merge=True)
    assert (number, ok) == (7, True)
    put = [(p, b) for m, p, b in s.calls if m == "PUT"]
    assert put[0][0] == "/pulls/7/merge"
    assert put[0][1]["merge_method"] == "squash" and put[0][1]["sha"] == "c2"
    # без merge — PUT не уходит
    s2 = FakeSession(routes())
    publish.publish(plan_with_note(), publish.GitHub("o/r", "t", s2), collect.open_state(tmp_path / "s2.sqlite"), "main")
    assert not [c for c in s2.calls if c[0] == "PUT"]
    # мерж упал (409) — исключение, но карточки уже в состоянии
    r3 = routes(); r3[("PUT", "/pulls/7/merge")] = (409, {"message": "Head branch was modified"})
    db3 = collect.open_state(tmp_path / "s3.sqlite")
    try:
        publish.publish(plan_with_note(), publish.GitHub("o/r", "t", FakeSession(r3)), db3, "main", merge=True)
        assert False
    except publish.GitHubError:
        pass
    assert f"{1:040x}" in publish.done_ids(db3)


def test_skipped_and_failed_comments_become_annotations_in_actions(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    publish.report_skipped(plan_with_note())
    assert "::warning::" not in capsys.readouterr().out          # вне Actions — только лог
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    publish.report_skipped(plan_with_note())
    assert capsys.readouterr().out.splitlines() == [f"::warning::НЕ ВОШЛА {'bad' * 4} : x"]   # заголовка у битой нет
    s = FakeSession(routes(review=(422, {"message": "line must be part of the diff"})))
    publish.publish(plan_with_note(), publish.GitHub("o/r", "t", s), collect.open_state(tmp_path / "s.sqlite"), "main")
    out = capsys.readouterr().out
    assert out.startswith("::warning::комментарии к строкам не встали") and "описание PR" in out
