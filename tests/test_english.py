"""Английский текст карточки для /en/ai-naviny сайта (ai-news-en-spec.md сайта):
запрос e-… в батче генерации, разбор, черновик, site_card, check() и снятие
сломанного английского при публикации. Без сети и API."""

import json
from datetime import datetime, timezone

import collect
import pipeline
import prompts
import publish
import tarask
from test_pipeline import answers, fake_client, seed, site  # noqa: F401 — site — фикстура
from test_publish import card

WHEN = datetime(2026, 10, 8, 5, 17, tzinfo=timezone.utc)


def no_converter(*a, **k):
    raise AssertionError("при наркамаўке конвертер не вызывается")


def run_to_drafts(tmp_path, site, monkeypatch, answer):
    monkeypatch.setattr(pipeline, "CARDS_DIR", tmp_path / "cards")
    monkeypatch.setattr(tarask, "run_converter", no_converter)
    monkeypatch.delenv(pipeline.BUDGET_ENV, raising=False)
    db = collect.open_state(tmp_path / "state.sqlite")
    hashes = seed(db)
    res = pipeline.Resources(site, "narkamauka")
    client = fake_client(answer)
    for _ in range(5):
        pipeline.run_once(db, client, res, limit=10)
    return db, client, hashes


def draft(tmp_path, h):
    return json.loads((tmp_path / "cards" / f"{h}.json").read_text(encoding="utf-8"))


def test_english_goes_in_the_generation_batch_and_into_the_draft(tmp_path, site, monkeypatch, capsys):
    db, client, (h0, h1, h2) = run_to_drafts(tmp_path, site, monkeypatch, answers)
    b = client.messages.batches
    gen = [b.store[i]["requests"] for i in b.created if b.store[i]["requests"][0]["custom_id"].startswith("g-")]
    ids = [r["custom_id"] for r in gen[0]]
    # На каждый материал — беларуский и английский запрос в одном батче
    assert sorted(ids) == sorted([f"g-{h1}", f"e-{h1}", f"g-{h2}", f"e-{h2}"])
    en = next(r for r in gen[0] if r["custom_id"] == f"e-{h1}")["params"]
    assert en["system"] == prompts.EN_TASK                                   # без справочного блока и кэша
    assert en["output_config"]["effort"] == "low"
    assert en["output_config"]["format"]["schema"] == prompts.EN_SCHEMA
    assert "Company: OpenAI" in en["messages"][0]["content"]                 # vendor_name от triage
    assert "<source>" in en["messages"][0]["content"]
    # В requests батча — материалы, а не запросы: потолок генераций про беларуские
    assert db.execute("SELECT requests FROM batch WHERE stage = 'generate'").fetchone()[0] == 2
    usage = json.loads(db.execute("SELECT usage FROM batch WHERE stage = 'generate'").fetchone()[0])
    assert 0 < usage["en_usd"] < usage["usd"]
    assert "из них английский текст: 2 результатов" in capsys.readouterr().out
    # Обе карточки — и чистая, и прошедшая fix — несут английский текст
    for h in (h1, h2):
        c = draft(tmp_path, h)
        assert c["en_title"] == "The company releases a model"            # точка в конце снята кодом
        assert c["en_thesis"] == "Fine-tuning took a week."
        assert c["en_retelling"].endswith("Second paragraph.")
        assert not any(n["kind"] == "en" for n in c["review_notes"])
    sc = publish.site_card(draft(tmp_path, h2))
    assert list(sc)[:7] == ["be_title", "thesis", "summary", "retelling", "en_title", "en_thesis", "en_retelling"]
    assert sc["en_retelling"] == ["The model is the largest, Rohin Shah said.", "Second paragraph."]


def test_english_failure_does_not_hold_the_belarusian_card(tmp_path, site, monkeypatch):
    def broken_english(stage, params):
        if stage == "en":
            text = params["messages"][0]["content"]
            if "Title 1" in text:
                return None                                                    # запрос упал
            return {"en_title": "Title", "en_thesis": "Thesis.", "en_retelling": "Cut in the mid"}
        return answers(stage, params)

    db, _, (h0, h1, h2) = run_to_drafts(tmp_path, site, monkeypatch, broken_english)
    for h, why in ((h1, "батч: errored"), (h2, "en_retelling абрываецца")):
        c = draft(tmp_path, h)
        assert c["status"] == "draft" and c["be_title"]                     # беларуская карточка есть
        assert not any(k.startswith("en_") for k in c)
        note = next(n for n in c["review_notes"] if n["kind"] == "en")
        assert note["detail"].startswith("англійскага тэксту няма") and why in note["detail"]
        assert "en_title" not in publish.site_card(c)


def test_publication_drops_broken_english_with_a_note_and_keeps_the_card():
    good = card(1, en_title="A title", en_thesis="A thesis.", en_retelling="One.\n\nTwo.")
    half = card(2, en_title="Only a title")
    plan = publish.plan_pr([good, half], [], set(), "main", WHEN)
    assert [e.card["id"] for e in plan.entries] == [good["id"], half["id"]]
    assert plan.entries[0].site["en_retelling"] == ["One.", "Two."]
    assert "en_title" not in plan.entries[1].site
    note = next(n for n in plan.entries[1].notes if n["kind"] == "en")
    assert "трэба ўсе тры палі" in note["detail"]
    assert not plan.skipped


def test_english_notes_anchor_to_their_lines():
    c = card(1, en_title="A title.", en_thesis="No period", en_retelling="One.\n\nTwo.")
    fatal, notes = publish.check(c)
    assert fatal == []
    assert [n["detail"] for n in notes][-2:] == [
        "en_title: кропка ў канцы загалоўка",
        "en_thesis: не канчаецца канцом сказа — абарваны ці няма кропкі",
    ]
    sc = publish.site_card(c)
    text = publish.render(sc)
    lines = publish.field_lines(text)
    comments = publish.comments_for("p", sc, text, notes[-2:])
    assert [x["line"] for x in comments] == [lines["en_title"], lines["en_thesis"]]


def test_english_prompt_keeps_the_rules_of_the_spec():
    t = prompts.EN_TASK
    for rule in ("own words", "relative dates", "does not repeat the title", "self-contained",
                 f"{prompts.RETELLING_WORDS[0]}–{prompts.RETELLING_WORDS[1]} words", "no period at the end"):
        assert rule in t, rule
    assert "{{" not in t                                                    # разметки в английском нет
    user = prompts.en_user({"source": "openai", "title": "T", "url": "u", "body": "B", "published_at": "2026-10-08",
                            "also": [("techcrunch", "T2", "Body two"), ("verge", "T3", "")]}, "OpenAI")
    assert "<source2>\nBody two\n</source2>" in user and "- verge: T3" in user
