"""Пайплайн этапа 4 без сети и без API: конвертер с guard-маркерами, линтер,
и полный круг батчей на поддельном клиенте Anthropic — отправка в одном
«запуске», забор в следующем."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import collect
import lint
import pipeline
import prompts
import tarask

needs_node = pytest.mark.skipif(shutil.which("node") is None or not (tarask.TOOL.parent / "node_modules").exists(),
                                reason="нет node или tools/taraskevize не установлен (npm ci)")

GUARDS = tarask.Guards([
    {"match": "Шах", "form": "word"},
    {"match": "Джэймс", "form": "word"},
    {"match": "трэніроў", "form": "stem"},
    {"match": "трэнажор", "form": "stem"},
])


# ---------- guard-маркеры ----------

def test_protect_wraps_latin_urls_code_and_markers():
    t = "Каманда CERT-UA і GGUF-мадэлі, https://x.com/a-b. `x-1` {{term:fine-tuning|файн-цюнінгу}}"
    out = tarask.protect(t)
    assert "<.CERT-UA>" in out and "<.GGUF>-мадэлі" in out
    assert "<.https://x.com/a-b>." in out            # точка после URL — не часть URL
    assert "<.`x-1`>" in out
    assert "<.{{term:fine-tuning|>файн-цюнінгу}}" in out   # форма слова внутри конвертируется


def test_protect_guard_dictionary_word_and_stem():
    out = tarask.protect("Рохін Шах, шахматы, трэніроўкамі і трэнажорнай", GUARDS)
    assert "<.Шах>," in out and "<.шахматы>" not in out
    assert "<.трэніроўкамі>" in out and "<.трэнажорнай>" in out


def test_protect_hides_converter_syntax():
    # Угловые скобки в тексте — синтаксис конвертера: прячутся и возвращаются
    assert tarask.restore(tarask.protect("а < б > в")) == "а < б > в"


@needs_node
def test_convert_known_breakages_are_guarded():
    src = "Рохін Шах і Джэймс Манйіка, трэніроўкі ў трэнажорнай зале, каманда CERT-UA, навучанне"
    raw = tarask.run_converter([src])[0]
    # Без guard конвертер ломает все четыре случая из корпуса и дефис CERT-UA
    assert "Шаг" in raw and "Джэймз" in raw and "сымулятар" in raw and "CERT—UA" in raw
    out = tarask.convert([src], GUARDS)[0]
    assert "Шах" in out and "Джэймс" in out and "трэніроўкі" in out and "трэнажорнай" in out
    assert "CERT-UA" in out and "навучаньне" in out   # остальное — обычная тарашкевіца


@needs_node
def test_converter_takes_base_form_not_variations():
    # variations: "first" дал бы «у вадным», «зьвестак», «Горадня»
    out = tarask.run_converter(["Праца ў адным офісе, аналіз дадзеных, Гродна."])[0]
    assert "вадным" not in out and "дадзеных" in out and "Гродна" in out


def test_suspicious_changes_flags_names_and_substitutions():
    before = "{{name:Альберт Гу}} пра трэніроўкі і сістэму."
    after = "{{name:Альбэрт Гу}} пра трэнаваньня і сыстэму."
    notes = tarask.suspicious_changes(before, after)
    assert {"kind": "імя", "before": "Альберт Гу", "after": "Альбэрт Гу"} in notes
    assert {"kind": "слова", "before": "трэніроўкі", "after": "трэнаваньня"} in notes
    assert not any(n["before"] == "сістэму" for n in notes)   # обычная орфография — не шум
    assert tarask.suspicious_changes("з ім не было", "зь ім ня было") == []


# ---------- линтер ----------

@pytest.fixture(scope="module")
def linter():
    entries = [
        {"avoid": "з'яўляецца", "prefer": ["ёсць"]},
        {"avoid": "прыняць удзел", "prefer": ["удзельнічаць"]},
        {"avoid": "дадзены", "match": r"(?<!\w)дадзен(?:ы|ага|аму|ым|ая|ай|ую|ае)(?!\w)", "prefer": ["гэты"]},
        {"avoid": "лічыцца", "prefer": ["уважаецца"]},
        {"avoid": "на працягу", "prefer": ["цягам"]},
    ]
    conv = {"з'яўляецца": "зьяўляецца"}
    rules = []
    for e in entries:
        rx = e.get("match") or "|".join({lint._pattern(e["avoid"]), lint._pattern(conv.get(e["avoid"], e["avoid"]))})
        rules.append(lint.Rule(e["avoid"], e["prefer"], "", "", __import__("re").compile(rx, 2)))
    return lint.Linter(rules)


def found(linter, text):
    return [(h.found, h.rule.avoid) for h in linter.check([text])]


def test_lint_catches_inflected_and_converted_forms(linter):
    assert found(linter, "Кампанія прыняла ўдзел у праекце.") == [("прыняла удзел", "прыняць удзел")]
    assert found(linter, "Мадэль зьяўляецца найбольшай.")[0][1] == "з'яўляецца"
    assert found(linter, "На працягу года.")[0][1] == "на працягу"


def test_lint_does_not_catch_neighbour_words(linter):
    assert found(linter, "Аналіз дадзеных паказаў вынік.") == []      # data, а не «данный»
    assert found(linter, "Яго лічаць лідарам, лічбы растуць.") == []
    assert found(linter, "На працяглыя тэрміны.") == []
    assert found(linter, "Гэта зьяўленьне новых мадэляў.") == []


def test_lint_endings_with_u_short(linter):
    # Текст ищется после _fold (ў → у), окончания «-аў», «-ўся» — тоже:
    # до 25.09.2026 они не совпадали ни с чем
    rx = __import__("re").compile(lint._pattern("рэзультаты"))
    assert rx.search(lint._fold("Рэзультатаў пакуль няма."))
    rx = __import__("re").compile(lint._pattern("аказацца"))
    assert rx.search(lint._fold("Ён аказаўся правым."))


def test_lint_verb_noun_phrase_in_any_order(linter):
    assert found(linter, "Кампанія прыняла актыўны ўдзел.")[0][1] == "прыняць удзел"
    assert found(linter, "Раунд вялі яны, а ўдзел у ім таксама прынялі іншыя.")[0][1] == "прыняць удзел"
    assert found(linter, "Удзел — гэта не прыняць рашэньне.") == []   # через тире не тянется


def test_lint_builtin_alphabet_rules(linter):
    assert [a for _, a in found(linter, "зваротнай транскриптазы")] == ["и / щ / ъ"]
    assert [a for _, a in found(linter, "перавышала лімit")] == ["лацінка ўнутры кірылічнага слова"]
    # Слаг термина латиницей рядом с кириллицей — не смесь
    assert found(linter, "прайшла {{term:fine-tuning|файн-цюнінг}} на GPT-5.") == []


def test_normalize_fixes_mechanics_but_not_versions():
    text, changes = lint.normalize("Ліміт — лімiт, 42.92% і 39.0 %, 2$ і 0.10 $, GPT-5.6, Opus 5.5, Gemini 3.8 TTS.")
    assert text == "Ліміт — ліміт, 42,92% і 39,0 %, 2 $ і 0,10 $, GPT-5.6, Opus 5.5, Gemini 3.8 TTS."
    assert len(changes) == 3


@needs_node
def test_normalize_quotes_and_comments():
    text, changes = lint.normalize('Рэжым "max" — пры генерацыі <!-- guard -->SVG-малюнка, 5" экран.')
    assert text == "Рэжым «max» — пры генерацыі SVG-малюнка, 5\" экран."
    assert len(changes) == 2


def test_truncated_text_is_detected():
    assert lint.truncated("Gemini Robotics ER 2 працуе як")
    assert lint.truncated("адна кіруе целам, другая выконвае ролю")
    assert not lint.truncated("Мадэль выйшла. Цяпер «так».")
    assert not lint.truncated("Хто гэта?\n\nНевядома…")


@needs_node
def test_truncated_generation_goes_back(tmp_path, site, monkeypatch):
    monkeypatch.setattr(pipeline, "CARDS_DIR", tmp_path / "cards")
    db = collect.open_state(tmp_path / "state.sqlite")
    h0, h1, h2 = seed(db)

    def cut(stage, params):
        a = answers(stage, params)
        if stage == "generate" and "Title 1" in params["messages"][0]["content"]:
            a["retelling"] = "Мадэль найбольшая. Яна працуе як"
        return a
    client = fake_client(cut)
    for _ in range(3):
        pipeline.run_once(db, client, pipeline.Resources(site), limit=10)
    stage, error = db.execute("SELECT stage, error FROM item WHERE hash = ?", (h1,)).fetchone()
    assert stage in ("triaged", "generate_sent") and "абарваны: retelling" in error
    assert not (tmp_path / "cards" / f"{h1}.json").exists()


def test_u_after_marker_follows_previous_word():
    # «}}» закрывает конвертеру предыдущее слово — «ў» ставим сами, тем же правилом
    out = tarask.convert(["найбольшы {{term:funding-round|раунд фінансавання}} у гісторыі",
                          "{{name:Дэміс Хасабіс}} ў сваім эсэ", "пра {{term:x|мадэлі}} ураду"])
    assert out[0].endswith("фінансаваньня}} ў гісторыі")
    assert out[1] == "{{name:Дэміс Хасабіс}} у сваім эсэ"      # после согласной — «у»
    assert out[2] == "пра {{term:x|мадэлі}} ураду"              # начало слова не трогаем


def test_split_keeps_versions_quotes_and_paragraphs():
    t = "Выйшла Opus 5.5. Яна «лепшая.» Далей.\n\nДругі абзац."
    paras = lint.split_paragraphs(t)
    assert paras == [["Выйшла Opus 5.5.", "Яна «лепшая.»", "Далей."], ["Другі абзац."]]
    assert lint.join_paragraphs(paras) == t


# ---------- круг батчей ----------

def msg(obj, model="claude-sonnet-5"):
    return NS(content=[NS(type="text", text=json.dumps(obj, ensure_ascii=False))], stop_reason="end_turn",
              model=model, usage=NS(input_tokens=100, output_tokens=50,
                                    cache_creation_input_tokens=0, cache_read_input_tokens=1000))


class FakeBatches:
    """Батч готов через delay проверок после отправки. Отправляется он в одном
    запуске, а проверяется уже в следующем — как в жизни."""

    def __init__(self, answer, delay=0):
        self.answer = answer    # (stage, params) → dict ответа или None (ошибка)
        self.delay = delay
        self.store = {}
        self.created = []

    def create(self, requests):
        bid = f"msgbatch_{len(self.store)}"
        self.store[bid] = {"requests": requests, "polls": 0}
        self.created.append(bid)
        return NS(id=bid)

    def retrieve(self, bid):
        b = self.store[bid]
        b["polls"] += 1
        status = "ended" if b["polls"] > self.delay else "in_progress"
        return NS(processing_status=status, request_counts=NS(succeeded=0, errored=0, processing=1))

    def results(self, bid):
        out = []
        for r in self.store[bid]["requests"]:
            stage = {"t": "triage", "g": "generate", "f": "fix"}[r["custom_id"][0]]
            ans = self.answer(stage, r["params"])
            if ans is None:
                out.append(NS(custom_id=r["custom_id"], result=NS(type="errored", error="boom")))
            else:
                out.append(NS(custom_id=r["custom_id"],
                              result=NS(type="succeeded", message=msg(ans, r["params"]["model"]))))
        return out


def fake_client(answer, delay=0):
    return NS(messages=NS(batches=FakeBatches(answer, delay)))


@pytest.fixture
def site(tmp_path):
    c = tmp_path / "site" / "claude"
    c.mkdir(parents=True)
    (c / "ai-news-styleguide.md").write_text("# стайлгайд\n", encoding="utf-8")
    (c / "ai-news-glossary.json").write_text(json.dumps({"entries": [
        {"term": "fine-tuning", "be": "файн-цюнінг", "alt": [], "avoid": [], "note": ""}]}), encoding="utf-8")
    (c / "ai-news-anti-calques.json").write_text(json.dumps({"entries": [
        {"avoid": "з'яўляецца", "prefer": ["ёсць"], "category": "звязка", "note": ""}]}), encoding="utf-8")
    (c / "ai-news-converter-guards.json").write_text(json.dumps({"entries": [
        {"match": "Шах", "form": "word"}]}), encoding="utf-8")
    (c / "ai-news-names.json").write_text(json.dumps({"entries": [
        {"en": "Simon Willison", "write": "Саймон Уілісан", "be": "Саймон Ўілісан", "who": "блогер"},
        {"en": "Demis Hassabis", "write": "Дэміс Хасабіс", "be": "Дэміс Хасабіс", "who": ""}]}), encoding="utf-8")
    return tmp_path / "site"


def seed(db, n=3):
    stamp = pipeline.now_iso()
    hashes = []
    for i in range(n):
        url = f"https://example.com/news/{i}"
        h = collect.url_hash(url)
        hashes.append(h)
        db.execute("INSERT INTO seen (hash, url, source, title, published_at, first_seen_at, status)"
                   " VALUES (?, ?, 'openai', ?, ?, ?, 'new')", (h, url, f"Title {i}", stamp, stamp))
        db.execute("INSERT INTO article (hash, body) VALUES (?, ?)", (h, "Some body text. " * 40))
    db.commit()
    return hashes


def answers(stage, params):
    if stage == "triage":
        text = params["messages"][0]["content"]
        imp = 1 if "Title 0" in text else 3          # первый — мусор, отсекается
        return {"is_ai": True, "category": "product", "vendor": "openai", "vendor_name": "OpenAI",
                "importance": imp, "reason": "тэст"}
    if stage == "generate":
        bad = "Title 2" in params["messages"][0]["content"]   # у третьего — калька
        return {"be_title": "Кампанія выпусціла мадэль",
                "thesis": "Мадэль прайшла {{term:fine-tuning|файн-цюнінг}}.",
                "summary": "Навучанне заняло тыдзень. {{name:Рохін Шах}} пракаментаваў.",
                "retelling": ("Мадэль з'яўляецца найбольшай. " if bad else "Мадэль найбольшая. ")
                             + "Пра гэта сказаў {{name:Рохін Шах}}.\n\nДругі абзац пра {{term:bogus|нешта}}."}
    if stage == "fix":
        return {"fixes": [{"n": 1, "sentence": "Мадэль — найбольшая.", "changed": True}]}


@needs_node
def test_full_cycle_across_runs(tmp_path, site, monkeypatch):
    monkeypatch.setattr(pipeline, "CARDS_DIR", tmp_path / "cards")
    db = collect.open_state(tmp_path / "state.sqlite")
    h0, h1, h2 = seed(db)
    res = pipeline.Resources(site)
    client = fake_client(answers)

    # Запуск 1: triage отправлен, ничего не готово
    pipeline.run_once(db, client, res, limit=10)
    assert {s for (s,) in db.execute("SELECT stage FROM item")} == {"triage_sent"}
    # Запуск 2: triage забран, первый отсеян, двое ушли на генерацию
    pipeline.run_once(db, client, res, limit=10)
    stages = dict(db.execute("SELECT hash, stage FROM item"))
    assert stages[h0] == "rejected" and stages[h1] == stages[h2] == "generate_sent"
    assert db.execute("SELECT body FROM article WHERE hash = ?", (h0,)).fetchone()[0] == ""
    # Запуск 3: генерация забрана; чистый — сразу draft, с калькой — на fix
    pipeline.run_once(db, client, res, limit=10)
    stages = dict(db.execute("SELECT hash, stage FROM item"))
    assert stages[h1] == "draft" and stages[h2] == "fix_sent"
    # Запуск 4: fix забран, карточка готова
    pipeline.run_once(db, client, res, limit=10)
    assert dict(db.execute("SELECT hash, stage FROM item"))[h2] == "draft"
    assert pipeline.run_once(db, client, res, limit=10) == 0      # больше ничего не ждёт

    card = json.loads((tmp_path / "cards" / f"{h2}.json").read_text(encoding="utf-8"))
    assert card["status"] == "draft" and card["importance"] == 3 and card["vendor"] == "openai"
    assert card["retelling"].startswith("Мадэль — найбольшая.")          # fix встал на место
    assert card["lint"][0]["changed"] and card["lint"][0]["hits"] == ["з'яўляецца"]
    assert "Рохін Шах" in card["retelling"] and "{{name:" not in card["retelling"]  # guard + маркер снят
    assert "{{term:fine-tuning|файн-цюнінг}}" in card["thesis"]          # разметка терминов хранится
    assert "Навучаньне" in card["summary"]                               # конвертер отработал
    assert "{{term:bogus" not in card["retelling"]                       # неизвестный слаг снят
    assert any(n["kind"] == "тэрмін" for n in card["review_notes"])
    # Все пять батчей отправлены по одному разу и забраны
    assert db.execute("SELECT COUNT(*) FROM batch WHERE collected_at IS NULL").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 3   # triage, generate, fix


@needs_node
def test_failed_results_go_back_and_then_fail(tmp_path, site, monkeypatch):
    monkeypatch.setattr(pipeline, "CARDS_DIR", tmp_path / "cards")
    db = collect.open_state(tmp_path / "state.sqlite")
    (h,) = seed(db, 1)
    res = pipeline.Resources(site)
    client = fake_client(lambda stage, params: None)   # каждый ответ — ошибка
    for _ in range(2 * pipeline.MAX_ATTEMPTS + 1):
        pipeline.run_once(db, client, res, limit=10)
    stage, attempts, error = db.execute("SELECT stage, attempts, error FROM item").fetchone()
    assert stage == "failed" and attempts == pipeline.MAX_ATTEMPTS and "errored" in error


@needs_node
def test_unfinished_batch_waits_without_resubmitting(tmp_path, site, monkeypatch):
    monkeypatch.setattr(pipeline, "CARDS_DIR", tmp_path / "cards")
    db = collect.open_state(tmp_path / "state.sqlite")
    seed(db, 2)
    res = pipeline.Resources(site)
    client = fake_client(answers, delay=2)
    for _ in range(3):
        assert pipeline.run_once(db, client, res, limit=10) == 1   # всё ещё ждём
    assert len(client.messages.batches.created) == 1               # второй раз не отправлен
    pipeline.run_once(db, client, res, limit=10)
    assert {s for (s,) in db.execute("SELECT stage FROM item")} == {"rejected", "generate_sent"}


@needs_node
def test_unfixable_lint_hits_are_kept_for_review(tmp_path, site, monkeypatch):
    monkeypatch.setattr(pipeline, "CARDS_DIR", tmp_path / "cards")
    db = collect.open_state(tmp_path / "state.sqlite")
    (h,) = seed(db, 1)
    res = pipeline.Resources(site)
    payload = {"nk": {f: "Адзін сказ." for f in pipeline.FIELDS},
               "tk": {f: "Адзін сказ." for f in pipeline.FIELDS}, "notes": []}
    payload["nk"]["retelling"] = "Мадэль з'яўляецца найбольшай. Другі сказ."   # два сказа
    payload["tk"]["retelling"] = "Мадэль зьяўляецца найбольшай, другі сказ."   # один — не сопоставить
    payload["flagged"] = pipeline.lint_card(res, payload["tk"])
    db.execute("INSERT INTO item (hash, stage, category, vendor, importance, payload, updated_at)"
               " VALUES (?, 'linted', 'product', 'openai', 2, ?, 'x')", (h, json.dumps(payload)))
    assert pipeline.submit_fix(db, fake_client(answers), res) is None   # fix не отправлялся
    card = json.loads((tmp_path / "cards" / f"{h}.json").read_text(encoding="utf-8"))
    assert any(n["kind"] == "лінтар" for n in card["review_notes"])


def test_old_material_is_not_enrolled(tmp_path):
    db = collect.open_state(tmp_path / "state.sqlite")
    db.execute("INSERT INTO seen (hash, url, source, title, published_at, first_seen_at, status)"
               " VALUES ('x', 'u', 'openai', 't', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z', 'new')")
    pipeline.enroll_new(db, collect.parse_iso("2026-09-24T00:00:00Z"))
    assert db.execute("SELECT stage FROM item").fetchone()[0] == "too_old"


def test_prompt_reference_block_is_stable(site):
    # Кэш — совпадение префикса байт в байт: два построения должны совпасть
    prompts.reference_block.cache_clear()
    a = prompts.cached_system(site, prompts.GENERATE_TASK)[0]["text"]
    prompts.reference_block.cache_clear()
    b = prompts.cached_system(site, prompts.FIX_TASK)[0]["text"]
    assert a == b and "fine-tuning | fine-tuning → файн-цюнінг" in a
    # Имена: как пишет модель; форма на сайте — только где конвертер её меняет
    assert "Simon Willison → Саймон Уілісан (на сайце: Саймон Ўілісан) — блогер" in a
    assert "Demis Hassabis → Дэміс Хасабіс\n" in a


def test_generate_prompt_keeps_publication_date_out():
    item = {"source": "techcrunch", "title": "t", "url": "u", "published_at": "2026-09-17T10:00:00Z", "body": "b"}
    assert "у тэкст не пішы" in prompts.generate_user(item)
    assert "дату публікацыі ў тэкст не пішы" in prompts.GENERATE_TASK
    assert "хто, калі" not in prompts.GENERATE_TASK


@needs_node
def test_names_convert_to_be():
    # write → конвертер (с guard-словарём, как в пайплайне) → be. Разошлось —
    # список имён врёт о том, что увидит читатель
    try:
        site = pipeline.site_repo()
    except SystemExit:
        pytest.skip("нет репозитория сайта")
    c = site / "claude"
    entries = json.loads((c / "ai-news-names.json").read_text(encoding="utf-8"))["entries"]
    guards = tarask.Guards.load(c / "ai-news-converter-guards.json")
    got = [tarask.restore(t) for t in tarask.run_converter([tarask.protect(e["write"], guards) for e in entries])]
    assert [(e["en"], g) for e, g in zip(entries, got)] == [(e["en"], e["be"]) for e in entries]
