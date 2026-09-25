#!/usr/bin/env python3
"""
Проверка пайплайна на эталонном корпусе — критерий этапа 2 плана: «промпт +
конвертер + линтер дают результат, который правишь меньше чем в 20% предложений».

Корпус — claude/ai-news-corpus/ в репозитории сайта: 20 новостей, пересказанных
и доведённых руками. Пайплайн (генерация → конвертер → guard-маркеры → линтер →
fix) прогоняется на ИСХОДНЫХ материалах этих новостей и сравнивается с эталоном.

Исходный материал берётся так (по порядку):
  1. data/eval/originals/NN.txt — текст оригинальной статьи. Скачивается
     командой `python eval_corpus.py fetch` по крыніца_url из корпуса. Нужна сеть
     до сайтов источников; VentureBeat (тексты 04, 11) режет ботов и не скачается.
  2. data/eval/facts/NN.txt — если оригинала нет: список фактов по-английски,
     извлечённый моделью из эталонного текста. Замена оригиналу, а не он сам:
     в нём ровно те факты, что выбрал эталон, и нет беларуских формулировок
     эталона (генерации не у чего списать). Какой вход был у каждого текста —
     пишется в отчёт.

Сравнение с эталоном — два числа:
  - буквальное: доля предложений пересказа, у которых в эталоне нет
    предложения, совпадающего с точностью до пробелов, регистра и пунктуации
    (похожесть ≥ LITERAL_MATCH). Пересказ независимый, поэтому это число
    почти всегда близко к 100% и само по себе мало что говорит;
  - по существу: модель-судья (JUDGE_MODEL) для каждого предложения пересказа
    решает, стал бы его править редактор, у которого есть эталон и стайлгайд:
    язык, термин, факт, тон. Это замена вычитке носителем, а не она сама
    (спека, §9, п. 2).

Всё через Batch API, состояние — в data/eval/, повторный запуск продолжает
с того места, где остановился.

    python eval_corpus.py fetch     # скачать оригиналы (нужна сеть до источников)
    python eval_corpus.py           # факты → генерация → судья → отчёт
"""

import argparse
import json
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import collect
import lint
import llm
import pipeline
import prompts

EVAL_DIR = collect.ROOT / "data" / "eval"
JUDGE_MODEL = "claude-opus-5"
LITERAL_MATCH = 0.9

# крыніца из шапки корпуса → id источника коллектора
SOURCE_IDS = {
    "TechCrunch": "techcrunch", "VentureBeat": "venturebeat", "Simon Willison's Weblog": "willison",
    "MIT Technology Review": "mittr", "Anthropic": "anthropic", "OpenAI": "openai",
    "Google DeepMind": "deepmind", "Hugging Face": "huggingface",
}


# ---------- корпус ----------

def load_corpus(site: Path) -> list[dict]:
    out = []
    for f in sorted((site / "claude" / "ai-news-corpus").glob("[0-9][0-9]-*.md")):
        _, head, rest = f.read_text(encoding="utf-8").split("---", 2)
        meta = dict(line.split(": ", 1) for line in head.strip().splitlines() if ": " in line)
        body = rest.split("\n---\n")[0].strip()   # ниже черты — заметки о правках, не текст
        src = next((v for k, v in SOURCE_IDS.items() if meta.get("крыніца", "").startswith(k)), "other")
        out.append({"n": f.name[:2], "file": f.name, "title": meta["загаловак"], "url": meta["крыніца_url"],
                     "source": src, "date": meta.get("дата_навіны", ""), "body": body})
    return out


# ---------- батчи ----------

def batches() -> dict:
    p = EVAL_DIR / "batches.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_batches(b: dict):
    (EVAL_DIR / "batches.json").write_text(json.dumps(b, indent=1))


def run_batch(client, name: str, requests: list[dict], poll: int) -> dict:
    """Отправить (если ещё не отправлен) и дождаться. Возвращает custom_id → результат."""
    b = batches()
    if name not in b:
        batch = client.messages.batches.create(requests=requests)
        b[name] = batch.id
        save_batches(b)
        print(f"{name}: отправлен батч {batch.id}, запросов {len(requests)}")
    while True:
        info = client.messages.batches.retrieve(b[name])
        if info.processing_status == "ended":
            break
        c = info.request_counts
        print(f"{name}: ждём, готово {c.succeeded + c.errored} из {c.succeeded + c.errored + c.processing}")
        time.sleep(poll)
    return {r.custom_id: r for r in client.messages.batches.results(b[name])}


def text_of(r) -> str | None:
    if r.result.type != "succeeded":
        return None
    return next((x.text for x in r.result.message.content if x.type == "text"), None)


# ---------- вход ----------

FACTS_PROMPT = """Below is a short Belarusian news retelling. Reconstruct the source \
material it was based on. First line: "Headline: " and a plain English news headline. \
Then terse English notes: every fact, number, name, date, \
quote and caveat from the text, one per line, starting with "- ". Write in plain \
English, as a reporter's notes on the original English article. Do not keep the \
Belarusian sentence structure or order word-for-word, do not add anything that is \
not in the text, do not comment.

Headline: {title}

Text:
{body}"""


def fetch_originals(corpus: list[dict]):
    d = EVAL_DIR / "originals"
    d.mkdir(parents=True, exist_ok=True)
    for c in corpus:
        try:
            title, body, _ = collect.parse_article(collect.fetch(c["url"]), max_paragraphs=pipeline.PAGE_PARAGRAPHS)
        except Exception as ex:
            print(f"{c['n']}: не скачалось — {type(ex).__name__}: {str(ex)[:120]}")
            continue
        (d / f"{c['n']}.txt").write_text(f"{title}\n\n{collect.truncate_words(body)}", encoding="utf-8")
        print(f"{c['n']}: {len(body)} знаков")


def split_title(text: str) -> tuple[str, str]:
    """Первая строка файла входа — английский заголовок, дальше текст."""
    first, _, rest = text.strip().partition("\n")
    return first.removeprefix("Headline:").strip(), rest.strip()


def ensure_inputs(client, corpus: list[dict], poll: int) -> dict[str, tuple[str, str, str]]:
    """n → (вид входа, английский заголовок, текст). Беларуский заголовок эталона
    генерации не показывается: иначе ей было бы с чего списать."""
    out, need = {}, []
    for c in corpus:
        orig = EVAL_DIR / "originals" / f"{c['n']}.txt"
        facts = EVAL_DIR / "facts" / f"{c['n']}.txt"
        if orig.exists():
            out[c["n"]] = ("original", *split_title(orig.read_text(encoding="utf-8")))
        elif facts.exists():
            out[c["n"]] = ("facts", *split_title(facts.read_text(encoding="utf-8")))
        else:
            need.append(c)
    if need:
        reqs = [{"custom_id": f"facts-{c['n']}", "params": {
            "model": llm.GENERATE_MODEL, "max_tokens": 2000,
            "messages": [{"role": "user", "content": FACTS_PROMPT.format(title=c["title"], body=c["body"])}],
        }} for c in need]
        res = run_batch(client, "facts", reqs, poll)
        (EVAL_DIR / "facts").mkdir(parents=True, exist_ok=True)
        for c in need:
            t = text_of(res[f"facts-{c['n']}"])
            if not t:
                raise SystemExit(f"{c['n']}: факты не извлеклись")
            (EVAL_DIR / "facts" / f"{c['n']}.txt").write_text(t.strip(), encoding="utf-8")
            out[c["n"]] = ("facts", *split_title(t))
    return out


# ---------- пайплайн ----------

def run_pipeline(client, corpus, inputs, poll: int) -> dict[str, dict]:
    """Прогон через тот же код, что и прод, на отдельном состоянии data/eval/state.sqlite.
    Triage пропускается: материалы корпуса уже отобраны. Возвращает n → карточка."""
    db = collect.open_state(EVAL_DIR / "state.sqlite")
    pipeline.CARDS_DIR = EVAL_DIR / "cards"
    res = pipeline.Resources(pipeline.site_repo())
    stamp = pipeline.now_iso()
    by_hash = {}
    for c in corpus:
        h = collect.url_hash(c["url"])
        by_hash[h] = c["n"]
        _, title, text = inputs[c["n"]]
        db.execute("INSERT OR IGNORE INTO seen (hash, url, source, title, published_at, first_seen_at, status)"
                   " VALUES (?, ?, ?, ?, ?, ?, 'new')",
                   (h, c["url"], c["source"], title, c["date"] + "T12:00:00Z", stamp))
        db.execute("INSERT OR IGNORE INTO article (hash, body) VALUES (?, ?)", (h, text))
        db.execute("INSERT OR IGNORE INTO item (hash, stage, category, vendor, importance, updated_at)"
                   " VALUES (?, 'triaged', 'other', 'other', 2, ?)", (h, stamp))
    db.commit()
    while True:
        pending = pipeline.collect_batches(db, client, res)
        for sub in (pipeline.submit_generate(db, client, res), pipeline.submit_fix(db, client, res)):
            pending += sub is not None
        if not pending:
            break
        time.sleep(poll)
    stages = dict(db.execute("SELECT hash, stage FROM item").fetchall())
    cards = {}
    for h, n in by_hash.items():
        p = pipeline.CARDS_DIR / f"{h}.json"
        if stages.get(h) == "draft" and p.exists():
            cards[n] = json.loads(p.read_text(encoding="utf-8"))
        else:
            err = db.execute("SELECT error FROM item WHERE hash = ?", (h,)).fetchone()[0]
            print(f"{n}: карточки нет, шаг {stages.get(h)}: {err}")
    return cards, db


# ---------- сравнение ----------

def plain(text: str) -> str:
    """Текст карточки для сравнения: маркеры терминов → слово, как увидит читатель."""
    return pipeline.TERM_RE.sub(lambda m: m.group(2), text)


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", s.lower().replace("ў", "у")).split())


def literal_diff(gen: list[str], ref: list[str]) -> int:
    refn = [norm(r) for r in ref]
    return sum(1 for g in gen
               if max((SequenceMatcher(a=norm(g), b=r).ratio() for r in refn), default=0) < LITERAL_MATCH)


JUDGE_PROMPT = """Ты — рэдактар беларускай стужкі навін пра ШІ, якая выходзіць \
тарашкевіцай. Перад табой эталонны тэкст навіны (вычытаны ўручную) і новы \
пераказ той жа навіны, зроблены аўтаматычна. Для КОЖНАГА пранумараванага сказа \
новага пераказу рашы: ці правіў бы ты яго перад публікацыяй, калі эталон і \
стайлгайд — твая норма.

edit = true, калі ў сказе ёсць хоць адно з:
- language — памылка правапісу тарашкевіцы, граматыкі, склону, русізм ці калька, \
няўдалае слова;
- term — тэрмін не так, як у эталоне ці глосарыі, або назва напісана не так;
- fact — факт, лічба ці імя супярэчаць эталону, або сцверджанне, якога ў \
эталоне няма і якое выглядае дадуманым;
- tone — рэкламны ці захоплены тон, канцылярыт, сенсацыйнасць, ацэнка без атрыбуцыі.

edit = false, калі сказ можна публікаваць як ёсць, хоць ён і сказаны іначай, \
чым у эталоне. Іншы парадак слоў, іншы выбар фактаў, іншае, але правільнае \
слова — не нагода для праўкі. Не прыдзірайся да стылю, калі ён не парушае \
правілаў вышэй.

Для кожнага сказа, дзе edit = true, дай category (адна галоўная) і кароткае \
тлумачэнне: што менавіта не так і як было б правільна.

<reference>
{reference}
</reference>

<new>
{numbered}
</new>"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"sentences": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "edit": {"type": "boolean"},
                       "category": {"type": "string", "enum": ["none", "language", "term", "fact", "tone"]},
                       "why": {"type": "string"}},
        "required": ["n", "edit", "category", "why"], "additionalProperties": False}}},
    "required": ["sentences"], "additionalProperties": False,
}


def judge(client, corpus, cards, poll: int) -> dict:
    reqs = []
    for c in corpus:
        card = cards.get(c["n"])
        if not card:
            continue
        sents = [plain(card["be_title"])] + lint.split_sentences(plain(card["retelling"]))
        numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sents))
        ref = f"{c['title']}\n\n{c['body']}"
        reqs.append({"custom_id": f"judge-{c['n']}", "params": {
            "model": JUDGE_MODEL, "max_tokens": 32000,
            "system": [{"type": "text", "text": prompts.reference_block(pipeline.site_repo()),
                        "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            "messages": [{"role": "user", "content": JUDGE_PROMPT.format(reference=ref, numbered=numbered)
                          + "\n\nСказ 0 — загаловак."}],
            "output_config": {"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        }})
    return run_batch(client, "judge", reqs, poll)


def report(corpus, inputs, cards, verdicts, db) -> dict:
    rows, tot = [], {"sent": 0, "literal": 0, "edit": 0, "title_edit": 0, "cats": {}}
    for c in corpus:
        card = cards.get(c["n"])
        if not card:
            rows.append({"n": c["n"], "missing": True})
            continue
        gen = lint.split_sentences(plain(card["retelling"]))
        ref = lint.split_sentences(c["body"])
        lit = literal_diff(gen, ref)
        r = verdicts.get(f"judge-{c['n']}")
        data = json.loads(text_of(r)) if r and text_of(r) else {"sentences": []}
        body_v = [v for v in data["sentences"] if v["n"] >= 1]
        title_v = [v for v in data["sentences"] if v["n"] == 0]
        edits = [v for v in body_v if v["edit"]]
        for v in edits:
            tot["cats"][v["category"]] = tot["cats"].get(v["category"], 0) + 1
        tot["sent"] += len(gen)
        tot["literal"] += lit
        tot["edit"] += len(edits)
        tot["title_edit"] += sum(v["edit"] for v in title_v)
        rows.append({"n": c["n"], "input": inputs[c["n"]][0], "sentences": len(gen), "literal_diff": lit,
                     "judged": len(body_v), "edit": len(edits),
                     "title_edit": any(v["edit"] for v in title_v),
                     "edits": [{**v, "sentence": gen[v["n"] - 1] if v["n"] - 1 < len(gen) else "?"}
                               for v in edits],
                     "title_verdict": title_v[0] if title_v else None,
                     "review_notes": card["review_notes"]})
    spent = sum(json.loads(u)["usd"] for (u,) in db.execute("SELECT usage FROM batch WHERE usage IS NOT NULL"))
    tot["pipeline_usd"] = round(spent, 4)
    return {"total": tot, "texts": rows}


def write_report(rep: dict, corpus, cards):
    t = rep["total"]
    lines = ["# Прогон пайплайна на эталонном корпусе\n",
             f"Предложений пересказа: {t['sent']}",
             f"Буквально отличаются от эталона: {t['literal']} ({100 * t['literal'] / max(t['sent'], 1):.1f}%)",
             f"Судья: правил бы {t['edit']} ({100 * t['edit'] / max(t['sent'], 1):.1f}%), по видам: {t['cats']}",
             f"Заголовков правил бы: {t['title_edit']} из {len(cards)}",
             f"Пайплайн (генерация + fix) стоил ≈ ${t['pipeline_usd']}\n"]
    for row in rep["texts"]:
        c = next(x for x in corpus if x["n"] == row["n"])
        lines.append(f"\n## {row['n']} — {c['title']}\n")
        if row.get("missing"):
            lines.append("Карточки нет.\n")
            continue
        card = cards[row["n"]]
        lines.append(f"Вход: {row['input']}. Предложений {row['sentences']}, буквально отличаются "
                     f"{row['literal_diff']}, судья правил бы {row['edit']}.\n")
        lines.append(f"**Заголовок:** {plain(card['be_title'])}\n")
        if row["title_verdict"] and row["title_verdict"]["edit"]:
            lines.append(f"> правка заголовка ({row['title_verdict']['category']}): {row['title_verdict']['why']}\n")
        lines.append("**Пересказ:**\n\n" + plain(card["retelling"]) + "\n")
        for e in row["edits"]:
            lines.append(f"- [{e['category']}] «{e['sentence']}» — {e['why']}")
        for n in row["review_notes"]:
            lines.append(f"- заметка пайплайна [{n['kind']}]: {n['detail']}")
    (EVAL_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (EVAL_DIR / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", default="run", choices=["run", "fetch"])
    ap.add_argument("--poll", type=int, default=30)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    corpus = load_corpus(pipeline.site_repo())
    if args.command == "fetch":
        fetch_originals(corpus)
        return
    client = llm.make_client()
    inputs = ensure_inputs(client, corpus, args.poll)
    cards, db = run_pipeline(client, corpus, inputs, args.poll)
    verdicts = judge(client, corpus, cards, args.poll)
    rep = report(corpus, inputs, cards, verdicts, db)
    write_report(rep, corpus, cards)
    t = rep["total"]
    print(f"\nПредложений: {t['sent']}; буквально отличаются: {t['literal']}"
          f" ({100 * t['literal'] / max(t['sent'], 1):.1f}%); судья правил бы: {t['edit']}"
          f" ({100 * t['edit'] / max(t['sent'], 1):.1f}%) {t['cats']}; заголовков: {t['title_edit']}/{len(cards)}")
    print(f"Отчёт: {EVAL_DIR / 'report.md'}")


if __name__ == "__main__":
    main()
