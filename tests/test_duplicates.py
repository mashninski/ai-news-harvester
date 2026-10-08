"""Этап 11 (ai-news-plan.md сайта): дубль по смыслу решает triage, повтор —
ссылка в «Крыніцы» карточки основного, в том числе уже опубликованной;
пересказ без фраз о том, чего нет в источнике. Сеть и API не нужны."""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

import collect
import lint
import pipeline
import prompts
import publish
from test_pipeline import fake_client, site  # noqa: F401 — фикстура
from test_publish import FakeSession, card, routes

WHEN = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)

# Пара 02.10.2026 с сайта (content/naviny/e8a9ff00…, 68ca0f79…): видео и подкаст
# TechCrunch про указ Трампа, вышли с разницей 8 минут, обе стали карточками.
# Склейка их не поймала: один источник, общих слов в заголовках почти нет
VIDEO = ("https://techcrunch.com/video/its-not-ai-anymore-its-super-intelligence-according-to-the-white-house/",
         "It’s not AI anymore, it’s ‘super intelligence’ (according to the White House)", "2026-10-02T17:48:16Z")
PODCAST = ("https://techcrunch.com/podcast/call-it-ai-call-it-super-intelligence-only-2-of-consumers-are-buying-it/",
           "Call it AI, call it Super Intelligence, only 2% of consumers are buying it", "2026-10-02T17:56:00Z")
BODY = "President Trump signed an executive order renaming artificial intelligence. " * 12


def add(db, url, title, published, source="techcrunch", stage=None, status="new", primary=None, body=BODY):
    h = collect.url_hash(url)
    db.execute("INSERT INTO seen (hash, url, source, title, published_at, first_seen_at, status, primary_hash)"
               " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (h, url, source, title, published, published, status, primary))
    if body:
        db.execute("INSERT INTO article (hash, body) VALUES (?, ?)", (h, body))
    if stage:
        db.execute("INSERT INTO item (hash, stage, importance, updated_at) VALUES (?, ?, 2, ?)",
                   (h, stage, published))
    db.commit()
    return h


def listed_titles(text: str) -> list[str]:
    return [line.split(": ", 1)[1] for line in text.split("Ужо ёсць у стужцы:", 1)[1].strip().splitlines()
            if ". " in line and ": " in line]


def model(stage, params):
    """Как Haiku: тот же указ в списке «что уже есть» — повтор, без новых фактов."""
    if stage != "triage":
        return None
    text = params["messages"][0]["content"]
    titles = listed_titles(text)
    same = next((i for i, t in enumerate(titles, 1) if "super intelligence" in t.lower()), 0)
    return {"is_ai": True, "category": "policy", "vendor": "other", "vendor_name": "", "importance": 2,
            "reason": "тэст", "same_as": same, "adds_new": False}


@pytest.fixture
def db(tmp_path, monkeypatch):
    # Обе карточки пары — из TechCrunch; правило «только повторы» с 08.10.2026
    # их бы до triage не пустило, а здесь проверяется сам triage
    monkeypatch.setattr(collect, "SECONDARY_ONLY", set())
    return collect.open_state(tmp_path / "state.sqlite")


# ---------- triage ----------

def test_trump_pair_second_becomes_duplicate_of_first(db, site):
    video, podcast = add(db, *VIDEO), add(db, *PODCAST)
    pipeline.enroll_new(db, WHEN)
    client = fake_client(model)
    pipeline.run_once(db, client, pipeline.Resources(site, "narkamauka"), now=WHEN, generate=False)
    sent = {r["custom_id"][2:]: r["params"]["messages"][0]["content"]
            for r in client.messages.batches.store["msgbatch_0"]["requests"]}
    # Только раньше вышедшие: видео не видит подкаст, подкаст видит видео
    assert listed_titles(sent[video]) == [] and "(пуста)" in sent[video]
    assert listed_titles(sent[podcast]) == [VIDEO[1]]
    pipeline.run_once(db, client, pipeline.Resources(site, "narkamauka"), now=WHEN, generate=False)
    stages = dict(db.execute("SELECT hash, stage FROM item"))
    assert stages == {video: "triaged", podcast: "rejected"}
    assert db.execute("SELECT status, primary_hash FROM seen WHERE hash = ?", (podcast,)).fetchone() \
        == ("duplicate", video)
    # Основной ещё ждёт генерации — текст повтора остаётся: он второй источник генерации
    m = pipeline.material(db, video)
    assert [s["url"] for s in m["sources"]] == [VIDEO[0], PODCAST[0]]
    assert "<source2>" in prompts.generate_user(m)


def test_same_event_with_new_facts_keeps_own_card(db, site):
    video, podcast = add(db, *VIDEO), add(db, *PODCAST)
    pipeline.enroll_new(db, WHEN)

    def adds_new(stage, params):
        return {**model(stage, params), "adds_new": True}
    client = fake_client(adds_new)
    for _ in range(2):
        pipeline.run_once(db, client, pipeline.Resources(site, "narkamauka"), now=WHEN, generate=False)
    assert dict(db.execute("SELECT hash, stage FROM item")) == {video: "triaged", podcast: "triaged"}


def test_duplicate_of_rejected_material_is_not_a_duplicate(db, site):
    video, podcast = add(db, *VIDEO), add(db, *PODCAST)
    pipeline.enroll_new(db, WHEN)

    def video_is_junk(stage, params):
        a = model(stage, params)
        return {**a, "importance": 1} if "Загаловак: " + VIDEO[1] in params["messages"][0]["content"] else a
    client = fake_client(video_is_junk)
    for _ in range(2):
        pipeline.run_once(db, client, pipeline.Resources(site, "narkamauka"), now=WHEN, generate=False)
    assert dict(db.execute("SELECT hash, stage FROM item")) == {video: "rejected", podcast: "triaged"}
    assert db.execute("SELECT status FROM seen WHERE hash = ?", (podcast,)).fetchone()[0] == "new"


def test_duplicate_of_published_card_and_its_cluster_move_to_primary(db, site):
    # Видео уже на сайте (draft), текст стёрт. Подкаст пришёл с повтором склейки
    video = add(db, *VIDEO, stage="draft", body="")
    podcast = add(db, *PODCAST)
    echo = add(db, "https://arstechnica.com/ai/2026/10/super/", "Echo", "2026-10-02T18:30:00Z",
               source="arstechnica", status="duplicate", primary=podcast)
    pipeline.enroll_new(db, WHEN)
    client = fake_client(model)
    for _ in range(2):
        pipeline.run_once(db, client, pipeline.Resources(site, "narkamauka"), now=WHEN, generate=False)
    assert db.execute("SELECT stage, error FROM item WHERE hash = ?", (podcast,)).fetchone() \
        == ("rejected", f"паўтор {video}")
    assert dict(db.execute("SELECT hash, primary_hash FROM seen WHERE status = 'duplicate'")) \
        == {podcast: video, echo: video}
    # Основной уже сгенерирован — текст повторов больше не нужен
    assert {b for (b,) in db.execute("SELECT body FROM article WHERE hash IN (?, ?)", (podcast, echo))} == {""}


def test_known_list_window_stages_and_duplicate_chain(db):
    near = add(db, "https://openai.com/news/a", "Generated", "2026-10-01T10:00:00Z", source="openai", stage="linted")
    add(db, "https://openai.com/news/b", "Too old", "2026-09-25T10:00:00Z", source="openai", stage="draft")
    add(db, "https://openai.com/news/c", "Rejected", "2026-10-01T11:00:00Z", source="openai", stage="rejected")
    add(db, "https://techcrunch.com/x", "Only secondary", "2026-10-01T12:00:00Z", stage="triaged")
    dup = add(db, "https://deepmind.google/d", "Dup", "2026-10-01T13:00:00Z", source="deepmind",
              status="duplicate", primary=near)
    new = add(db, *VIDEO, stage="new")
    collect.SECONDARY_ONLY.add("techcrunch")
    try:
        assert pipeline.triage_known(db, [new]) == {new: [near]}
    finally:
        collect.SECONDARY_ONLY.discard("techcrunch")
    # Ссылка на повтор ведёт к его основному
    assert pipeline.primary_of(db, dup, {}) == near
    assert pipeline.same_event(db, {"same_as": 2, "adds_new": False}, [new, dup], {}) == near
    assert pipeline.same_event(db, {"same_as": 7, "adds_new": False}, [near], {}) is None


# ---------- публикация ----------

def site_file(c: dict) -> str:
    return publish.render(publish.site_card(c))


def gh_routes(files: dict[str, str]) -> dict:
    r = routes()
    for path, text in files.items():
        r[("GET", f"/contents/{path}?ref=main")] = (200, {"content": base64.b64encode(text.encode()).decode()})
    return r


def dup_of(db, primary_card: dict, url: str, source="arstechnica") -> str:
    """Повтор в состоянии: seen duplicate с primary_hash карточки."""
    h = collect.url_hash(url)
    db.execute("INSERT INTO seen (hash, url, source, title, published_at, first_seen_at, status, primary_hash)"
               " VALUES (?, ?, ?, 'Same story', ?, ?, 'duplicate', ?)",
               (h, url, source, collect.iso(WHEN), collect.iso(WHEN), primary_card["id"]))
    db.commit()
    return h


def test_duplicate_goes_into_sources_of_card_not_yet_published(tmp_path):
    db = collect.open_state(tmp_path / "state.sqlite")
    c = card(1)
    dup = dup_of(db, c, "https://arstechnica.com/ai/2026/10/one/")
    # Тот же адрес, что уже в карточке, второй раз не добавляется
    same = dup_of(db, c, "https://www.openai.com/news/1/?utm_source=x", source="openai2")
    plan = publish.plan_pr([c], [], set(), "main", WHEN, publish.duplicate_sources(db, WHEN))
    assert [s["url"] for s in plan.entries[0].site["sources"]] == [
        "https://openai.com/news/1", "https://arstechnica.com/ai/2026/10/one/"]
    publish.publish(plan, publish.GitHub("o/r", "t", FakeSession(routes())), db, "main")
    assert {h for (h,) in db.execute("SELECT hash FROM linked")} == {dup, same}
    assert publish.duplicate_sources(db, WHEN) == {}


def test_duplicate_of_published_card_edits_its_file_in_the_same_pr(tmp_path):
    db = collect.open_state(tmp_path / "state.sqlite")
    old = card(1)
    path = publish.card_path(old["id"])
    dup = dup_of(db, old, "https://arstechnica.com/ai/2026/10/one/")
    s = FakeSession(gh_routes({path: site_file(old)}))
    gh = publish.GitHub("o/r", "t", s)
    dups = publish.duplicate_sources(db, WHEN)
    edits, settled = publish.link_edits(gh, "main", dups, {old["id"]}, tmp_path / "cards")
    plan = publish.plan_pr([old, card(2)], [], {old["id"]}, "main", WHEN, dups, edits, settled)
    assert [e.card["id"] for e in plan.entries] == [card(2)["id"]]
    assert plan.title == "ШІ-навіны: 1 картка на праверку, крыніцы да 1 апублікаванай карткі, 02.10.2026"
    assert "Крыніцы да апублікаваных — 1" in plan.body and "arstechnica" in plan.body
    # В файле опубликованной меняются только «Крыніцы»: одна запись дописана
    before, after = json.loads(site_file(old)), json.loads(plan.edits[0].text)
    assert after["sources"][:1] == before["sources"] and after["sources"][1]["url"] == "https://arstechnica.com/ai/2026/10/one/"
    assert {k: v for k, v in after.items() if k != "sources"} == {k: v for k, v in before.items() if k != "sources"}
    publish.publish(plan, gh, db, "main")
    tree = next(b for m, p, b in s.calls if p == "/git/trees")["tree"]
    assert sorted(t["path"] for t in tree) == sorted([path, publish.card_path(card(2)["id"])])
    assert db.execute("SELECT primary_hash, pr FROM linked WHERE hash = ?", (dup,)).fetchone() == (old["id"], 7)
    # Следующий запуск этот повтор не трогает
    assert publish.duplicate_sources(db, WHEN) == {}


def test_only_edits_still_open_a_pr(tmp_path):
    db = collect.open_state(tmp_path / "state.sqlite")
    old = card(1)
    dup_of(db, old, "https://arstechnica.com/ai/2026/10/one/")
    gh = publish.GitHub("o/r", "t", FakeSession(gh_routes({publish.card_path(old["id"]): site_file(old)})))
    dups = publish.duplicate_sources(db, WHEN)
    edits, settled = publish.link_edits(gh, "main", dups, {old["id"]}, tmp_path / "cards")
    plan = publish.plan_pr([old], [], {old["id"]}, "main", WHEN, dups, edits, settled)
    assert plan.entries == [] and len(plan.edits) == 1
    assert plan.title == "ШІ-навіны: крыніцы да 1 апублікаванай карткі, 02.10.2026"
    assert plan.body.startswith("Новых картак няма")


def test_duplicate_already_in_draft_is_settled_without_fetching(tmp_path):
    # Повтор склейки вошёл в карточку ещё при генерации — на сайт за файлом не ходим
    db = collect.open_state(tmp_path / "state.sqlite")
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    url = "https://arstechnica.com/ai/2026/10/one/"
    c = card(1, sources=card(1)["sources"] + [{"source": "arstechnica", "url": url, "title": "x", "primary": False}])
    (cards_dir / f"{c['id']}.json").write_text(json.dumps(c), encoding="utf-8")
    dup = dup_of(db, c, url)
    s = FakeSession(routes())
    edits, settled = publish.link_edits(publish.GitHub("o/r", "t", s), "main",
                                        publish.duplicate_sources(db, WHEN), {c["id"]}, cards_dir)
    assert edits == [] and settled == [(dup, c["id"])] and s.calls == []
    plan = publish.plan_pr([c], [], {c["id"]}, "main", WHEN, {}, edits, settled)
    publish.record_settled(db, plan)
    assert db.execute("SELECT pr FROM linked WHERE hash = ?", (dup,)).fetchone() == (None,)


def test_duplicate_waits_while_card_is_not_on_site(tmp_path):
    db = collect.open_state(tmp_path / "state.sqlite")
    c = card(1)
    dup_of(db, c, "https://arstechnica.com/ai/2026/10/one/")
    s = FakeSession(routes())
    edits, settled = publish.link_edits(publish.GitHub("o/r", "t", s), "main",
                                        publish.duplicate_sources(db, WHEN), set(), tmp_path / "cards")
    assert (edits, settled, s.calls) == ([], [], [])
    # Старше LINK_LOOKBACK_DAYS — не смотрим вовсе
    later = WHEN + timedelta(days=publish.LINK_LOOKBACK_DAYS + 1)
    assert publish.duplicate_sources(db, later) == {}


# ---------- «чего нет в источнике» ----------

SILENCE = [
    "Крыніца не ўдакладняе, хто інвеставаў у гэты раунд.",
    "У матэрыяле не паведамляецца, як OpenAI адказала на пазоў.",
    "Падрабязнасці самога загаду ў крыніцы не прыводзяцца.",
    "Невядома таксама, хто заснаваў кампанію.",
    "Таксама невядома, калі функцыя стане даступная.",
    "Арыгінальны тэкст крыніцы абарваны на гэтым месцы.",
    "Падрабязнасці ў даступным урыўку крыніцы не пазначаны.",
    "У паведамленні Apple не ўдакладняецца, якімі будуць абмежаванні.",
    "Крыніца абмяжоўваецца агульнай сумай падтрымкі.",
]
NOT_SILENCE = [
    "Кампанія папярэджвае, што мадэль можа памыляцца.",
    "OpenAI не назвала цану новай мадэлі.",
    "Крыніцы, знаёмыя з перамовамі, расказалі выданню The Information пра здзелку.",
    "Мадэль можа браць даныя з многіх крыніц і сама вырашаць, што ў іх важна, а што не.",
    "Невядома, ці паўторацца такія паказчыкі пры большай колькасці спроб.",
    "Пакуль кампанія прапанавала толькі запіс на апавяшчэнне аб тым, калі адкрыецца рэгістрацыя.",
]


@pytest.mark.parametrize("sentence", SILENCE)
def test_linter_flags_what_the_source_does_not_say(sentence):
    hits = lint.Linter([]).check([sentence])
    assert [h.rule.avoid for h in hits] == ["спасылка на крыніцу"]


@pytest.mark.parametrize("sentence", NOT_SILENCE)
def test_linter_keeps_caveats_of_the_source_itself(sentence):
    assert lint.Linter([]).check([sentence]) == []


def test_generate_prompt_has_no_unknowns_paragraph_and_keeps_source_caveats():
    for task in (prompts.GENERATE_TASK, prompts.GENERATE_TASK_TARASK):
        assert "што пакуль невядома" not in task
        assert prompts.SOURCE_SILENCE in task and "кампанія папярэджвае" in task
    assert "what is still unknown" not in prompts.EN_TASK and "does not specify" in prompts.EN_TASK
    assert 'sentence = ""' in prompts.FIX_TASK


def test_fix_deletes_flagged_sentence_and_empty_paragraph_but_not_whole_field(site):
    res = pipeline.Resources(site, "narkamauka")
    retelling = "Першы абзац. Тут факт.\n\nКрыніца не называе цану. Невядома таксама, калі."
    tk = {"be_title": "Загаловак", "thesis": "У матэрыяле не паведамляецца пра цану.",
          "summary": "Кароткі змест.", "retelling": retelling}
    p = {"nk": dict(tk), "tk": dict(tk), "notes": [], "flagged": pipeline.lint_card(res, tk)}
    assert [(f["field"], f["p"], f["s"]) for f in p["flagged"]] == [("thesis", 0, 0), ("retelling", 1, 0),
                                                                     ("retelling", 1, 1)]
    for i, fl in enumerate(p["flagged"], 1):
        fl["n"] = i
    out = pipeline.apply_fixes(res, p, [{"n": n, "sentence": "", "changed": True} for n in (1, 2, 3)])
    assert out["tk"]["retelling"] == "Першы абзац. Тут факт."
    # Пустой тезис не пустил бы карточку в PR — он остаётся, остаток виден на ревью
    assert out["tk"]["thesis"] == tk["thesis"]
    assert [f["field"] for f in out["lint_left"]] == ["thesis"]
