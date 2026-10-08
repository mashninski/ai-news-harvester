"""Источник только для повторов (collect.SECONDARY_ONLY, рашэнне аўтара
08.10.2026): TechCrunch своей карточки не получает, только ссылкой рядом
с той же новостью другого источника. Без сети и API."""

import json

import collect
import pipeline
from collect import Item
from test_pipeline import answers, fake_client, site  # noqa: F401 — site — фикстура


def item(source, title, hours, h):
    return Item(source, f"https://{source}.example/{h}", f"2026-10-07T{hours:02d}:00:00Z", title, "Body. " * 50, h)


TC = "OpenAI launches GPT-6 Astra model for long research tasks"
VENDOR = "OpenAI launches GPT-6 Astra for long-running research tasks"


def test_techcrunch_alone_is_not_a_card():
    new, late, orphans = collect.cluster([item("techcrunch", TC, 10, "t1")])
    assert new == [] and late == []
    assert [o.hash for o in orphans] == ["t1"]


def test_techcrunch_yields_to_any_other_source_in_the_same_run():
    # TechCrunch раньше по времени, но основным становится другой источник
    tc, ars = item("techcrunch", TC, 8, "t1"), item("arstechnica", VENDOR, 10, "a1")
    new, _, orphans = collect.cluster([tc, ars])
    assert [p.hash for p in new] == ["a1"]
    assert [d.hash for d in new[0].duplicates] == ["t1"]
    assert orphans == []


def test_lone_techcrunch_does_not_swallow_a_later_vendor_post(tmp_path):
    db = collect.open_state(tmp_path / "state.sqlite")
    tc = item("techcrunch", TC, 8, "t" * 40)
    vendor = item("openai", VENDOR, 9, "o" * 40)
    now = collect.parse_iso("2026-10-07T12:00:00Z")
    # Прогон 1: только TechCrunch — повтор без основного, в генерацию не идёт
    primaries, late, orphans = collect.cluster([tc], collect.load_known(db, now.replace(day=1)))
    collect.save(db, [(o.hash, o.url, o.source, o.title, o.published_at, "x", "duplicate", None) for o in orphans])
    assert db.execute("SELECT status, primary_hash FROM seen WHERE hash = ?", (tc.hash,)).fetchone() == ("duplicate", None)
    # Прогон 2: пост компании — свой основной, а не повтор анонса
    new, late, _ = collect.cluster([vendor], collect.load_known(db, now.replace(day=1)))
    assert [p.hash for p in new] == [vendor.hash] and late == []


def test_queued_techcrunch_material_is_rejected_without_generation(tmp_path, site, monkeypatch):
    monkeypatch.delenv(pipeline.BUDGET_ENV, raising=False)
    db = collect.open_state(tmp_path / "state.sqlite")
    stamp = pipeline.now_iso()
    h = "c" * 40
    db.execute("INSERT INTO seen (hash, url, source, title, published_at, first_seen_at, status)"
               " VALUES (?, 'https://techcrunch.com/x', 'techcrunch', 'T', ?, ?, 'new')", (h, stamp, stamp))
    db.execute("INSERT INTO article (hash, body) VALUES (?, ?)", (h, "Teaser sentence. " * 20))
    db.execute("INSERT INTO item (hash, stage, category, vendor, importance, updated_at)"
               " VALUES (?, 'triaged', 'product', 'openai', 3, ?)", (h, stamp))
    db.commit()
    client = fake_client(answers)
    pipeline.run_once(db, client, pipeline.Resources(site, "narkamauka"))
    assert client.messages.batches.created == []                       # генерации нет
    assert db.execute("SELECT stage FROM item WHERE hash = ?", (h,)).fetchone()[0] == "rejected"
    assert db.execute("SELECT body FROM article WHERE hash = ?", (h,)).fetchone()[0] == ""
    assert json.dumps(sorted(collect.SECONDARY_ONLY)) == '["techcrunch"]'
