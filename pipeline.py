#!/usr/bin/env python3
"""
Пайплайн карточек для mashninski.com/naviny — этап 4 плана.

Берёт из состояния коллектора (data/state.sqlite) новые материалы
(seen.status = 'new') и доводит каждый до черновика карточки:

  triage    Haiku 4.5, Batch API: ИИ или нет, категория, вендор, важность 1–3.
            Важность 1 отсекается
  generate  Sonnet 5, Batch API: заголовок, тезис, summary, пересказ наркамаўкай,
            термины глоссария размечены {{term:slug|форма}}
  convert   taraskevizer + guard-маркеры (tarask.py) — наркамаўка → тарашкевіца
  lint      антикальки после конвертера (lint.py), обычный код
  fix       Sonnet 5, Batch API: только помеченные предложения
  draft     output/cards/<hash>.json, status = draft

Batch API асинхронный: отправленное сегодня приходит через минуты или часы.
Поэтому каждый запуск делает одно и то же: сначала забирает готовые батчи,
отправленные прошлыми запусками (таблица batch), потом отправляет новые на
всё, что ждёт своего шага (таблица item). Запуск можно повторять сколько
угодно — ничего не отправится дважды и не потеряется при падении процесса.

    python pipeline.py            # один проход: забрать готовое, отправить новое
    python pipeline.py --wait     # ходить по кругу, пока все батчи не вернутся
    python pipeline.py --status   # только показать, что где лежит

Ключ — переменная окружения AI_NEWS_ANTHROPIC_KEY (llm.py).
Языковые ресурсы — из репозитория сайта, путь в AI_NEWS_SITE_REPO,
по умолчанию ../mashninski-site.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import collect
import lint
import llm
import prompts
import tarask

ROOT = Path(__file__).resolve().parent
CARDS_DIR = ROOT / "output" / "cards"

MAX_ATTEMPTS = 3            # столько раз этап может не удаться, потом failed
MAX_GENERATE_PER_RUN = 40   # потолок генераций за запуск: страховка от случайной траты
MIN_BODY_CHARS = 400        # меньше — текста для пересказа мало
MIN_BODY_ANY = 150          # меньше и докачать нельзя — пересказывать нечего
# Докачка страницы, когда в фиде мало текста. Только эти источники: у DeepMind
# и Hugging Face в фиде заголовок и пустой body (журнал сайта, 24.09.2026, этап 3).
# У остальных RSS страницы не скрейпим (спека, §3, п. 4) — Anthropic и так
# берётся со страницы коллектором.
PAGE_FETCH_SOURCES = {"deepmind", "huggingface"}
PAGE_PARAGRAPHS = 12

# Цены на 24.09.2026, $ за миллион токенов (спека, §4). Batch — половина.
# Запись в кэш на 1 час — 2× входа, чтение — 0.1×. Только для отчёта о тратах.
PRICES = {llm.TRIAGE_MODEL: (1.0, 5.0), llm.GENERATE_MODEL: (2.0, 10.0)}

# Sonnet 5 по умолчанию думает (adaptive thinking), и размышление идёт в тот же
# лимит, что и ответ: при 4000 токенов 17 генераций из 20 на корпусе обрезались
# посреди размышления (журнал сайта, 24.09.2026). Поэтому лимит с запасом,
# а глубина размышления задаётся явно — effort
MAX_TOKENS = {"triage": 400, "generate": 16000, "fix": 8000}
EFFORT = {"generate": "medium", "fix": "low"}   # генерация — язык, на ней не экономим (спека, §3)

log = logging.getLogger("pipeline")


# ---------- окружение ----------

def site_repo() -> Path:
    p = Path(os.environ.get("AI_NEWS_SITE_REPO") or ROOT.parent / "mashninski-site")
    if not (p / "claude" / "ai-news-glossary.json").exists():
        raise SystemExit(f"нет репозитория сайта с глоссарием: {p} (задай AI_NEWS_SITE_REPO)")
    return p


class Resources:
    """Глоссарий, guard-словарь и линтер — читаются один раз за запуск."""

    def __init__(self, site: Path):
        c = site / "claude"
        self.site = site
        self.guards = tarask.Guards.load(c / "ai-news-converter-guards.json")
        self.linter = lint.Linter.load(c / "ai-news-anti-calques.json")
        glossary = json.loads((c / "ai-news-glossary.json").read_text(encoding="utf-8"))["entries"]
        self.slugs = {prompts.slug(e["term"]) for e in glossary}


def now_iso() -> str:
    return collect.iso(datetime.now(timezone.utc))


# ---------- состояние ----------

def set_stage(db, h: str, stage: str, **fields):
    cols = {"stage": stage, "updated_at": now_iso(), **fields}
    sets = ", ".join(f"{k} = ?" for k in cols)
    db.execute(f"UPDATE item SET {sets} WHERE hash = ?", (*cols.values(), h))


def fail_step(db, h: str, back_to: str, error: str):
    """Шаг не удался: назад на прошлый шаг, а после MAX_ATTEMPTS — в failed."""
    attempts = db.execute("SELECT attempts FROM item WHERE hash = ?", (h,)).fetchone()[0] + 1
    stage = "failed" if attempts >= MAX_ATTEMPTS else back_to
    set_stage(db, h, stage, attempts=attempts, error=error[:500])
    log.warning("%s: %s → %s (попытка %d): %s", h[:8], back_to, stage, attempts, error[:200])


def forget_body(db, h: str):
    """Текст статьи больше не нужен: в состоянии остаётся только нужное для дедупа."""
    db.execute("UPDATE article SET body = '' WHERE hash = ?", (h,))


def material(db, h: str) -> dict:
    row = db.execute(
        "SELECT s.source, s.url, s.title, s.published_at, COALESCE(a.body, ''), COALESCE(a.from_page, 0)"
        " FROM seen s LEFT JOIN article a ON a.hash = s.hash WHERE s.hash = ?", (h,)).fetchone()
    source, url, title, pub, body, from_page = row
    dups = db.execute("SELECT source, url, title FROM seen WHERE primary_hash = ? ORDER BY published_at",
                      (h,)).fetchall()
    return {"hash": h, "source": source, "url": url, "title": title or "", "published_at": pub,
            "body": body, "from_page": bool(from_page),
            "also": [(s, t) for s, _, t in dups],
            "sources": [{"source": source, "url": url, "title": title, "primary": True}]
                       + [{"source": s, "url": u, "title": t, "primary": False} for s, u, t in dups]}


def enroll_new(db, now: datetime) -> int:
    """Новые материалы коллектора → строки item. Старше окна свежести — too_old:
    пайплайн мог не запускаться неделю, старьё в ленту не нужно."""
    cutoff = collect.iso(now - timedelta(days=collect.MAX_AGE_DAYS))
    rows = db.execute(
        "SELECT hash, COALESCE(published_at, first_seen_at) FROM seen"
        " WHERE status = 'new' AND hash NOT IN (SELECT hash FROM item)").fetchall()
    stamp = now_iso()
    for h, pub in rows:
        db.execute("INSERT INTO item (hash, stage, updated_at) VALUES (?, ?, ?)",
                   (h, "new" if pub >= cutoff else "too_old", stamp))
    db.commit()
    return len(rows)


# ---------- батчи ----------

def custom_id(stage: str, h: str) -> str:
    return f"{stage[0]}-{h}"   # t-/g-/f- и sha1: 42 знака, в лимит API (64) влезает


def submit(db, client, stage: str, requests: list[dict], hashes: list[str], next_stage: str):
    if not requests:
        return None
    batch = client.messages.batches.create(requests=requests)
    db.execute("INSERT INTO batch (id, stage, requests, submitted_at) VALUES (?, ?, ?, ?)",
               (batch.id, stage, len(requests), now_iso()))
    for h in hashes:
        set_stage(db, h, next_stage, batch_id=batch.id)
    db.commit()
    print(f"Отправлен батч {stage}: {batch.id}, запросов {len(requests)}")
    return batch.id


def usage_add(acc: dict, model: str, u):
    a = acc.setdefault(model, {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
    a["input"] += u.input_tokens or 0
    a["output"] += u.output_tokens or 0
    a["cache_write"] += u.cache_creation_input_tokens or 0
    a["cache_read"] += u.cache_read_input_tokens or 0


def usage_cost(acc: dict) -> float:
    """Оценка в $ с учётом скидки Batch API. Запись в кэш считаем по цене
    часового TTL (2× входа) — так пишет генерация."""
    total = 0.0
    for model, a in acc.items():
        # В ответе модель бывает с датой: claude-haiku-4-5-20251001
        pin, pout = next((v for k, v in PRICES.items() if model.startswith(k)), (0, 0))
        total += (a["input"] * pin + a["cache_write"] * pin * 2 + a["cache_read"] * pin * 0.1
                  + a["output"] * pout) / 1e6 * 0.5
    return total


def result_json(result) -> tuple[dict | None, str | None, object]:
    """(данные, ошибка, сообщение) из одного результата батча."""
    if result.result.type != "succeeded":
        err = getattr(result.result, "error", None)
        return None, f"батч: {result.result.type} {err}"[:300], None
    msg = result.result.message
    if msg.stop_reason != "end_turn":
        return None, f"stop_reason = {msg.stop_reason}", msg
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(text), None, msg
    except json.JSONDecodeError as ex:
        return None, f"не JSON: {ex}", msg


def collect_batches(db, client, res: Resources) -> int:
    """Забирает все готовые батчи. Возвращает, сколько ещё в работе."""
    pending = 0
    for batch_id, stage in db.execute(
            "SELECT id, stage FROM batch WHERE collected_at IS NULL ORDER BY submitted_at").fetchall():
        info = client.messages.batches.retrieve(batch_id)
        if info.processing_status != "ended":
            c = info.request_counts
            print(f"Батч {stage} {batch_id} ещё в работе: готово {c.succeeded + c.errored}"
                  f" из {c.succeeded + c.errored + c.processing}")
            pending += 1
            continue
        acc: dict = {}
        handler = {"triage": on_triage, "generate": on_generate, "fix": on_fix}[stage]
        sent = f"{stage}_sent"
        # Только материалы, которые всё ещё ждут именно этот батч: если прошлый
        # запуск упал посреди разбора, уже разобранное второй раз не трогаем
        waiting = {h for (h,) in db.execute("SELECT hash FROM item WHERE batch_id = ? AND stage = ?",
                                            (batch_id, sent))}
        results = [r for r in client.messages.batches.results(batch_id)
                   if r.custom_id.split("-", 1)[1] in waiting]
        for r in results:
            if r.result.type == "succeeded":
                usage_add(acc, r.result.message.model, r.result.message.usage)
        handler(db, res, results, acc)
        # Материалы батча, по которым результата не пришло вовсе, — назад
        seen_ids = {r.custom_id for r in results}
        back = {"triage": "new", "generate": "triaged", "fix": "linted"}[stage]
        for (h,) in db.execute("SELECT hash FROM item WHERE batch_id = ? AND stage = ?",
                               (batch_id, sent)).fetchall():
            if custom_id(stage, h) not in seen_ids:
                fail_step(db, h, back, "результата в батче нет")
        cost = usage_cost(acc)
        db.execute("UPDATE batch SET collected_at = ?, usage = ? WHERE id = ?",
                   (now_iso(), json.dumps({"tokens": acc, "usd": round(cost, 4)}), batch_id))
        db.commit()
        print(f"Забран батч {stage} {batch_id}: {len(results)} результатов, ≈ ${cost:.4f}")
    return pending


# ---------- triage ----------

def submit_triage(db, client) -> str | None:
    hashes = [h for (h,) in db.execute("SELECT hash FROM item WHERE stage = 'new'").fetchall()]
    reqs = []
    for h in hashes:
        m = material(db, h)
        reqs.append({"custom_id": custom_id("triage", h), "params": {
            "model": llm.TRIAGE_MODEL,
            "max_tokens": MAX_TOKENS["triage"],
            "system": prompts.TRIAGE_SYSTEM,
            "messages": [{"role": "user", "content": prompts.triage_user(m["source"], m["title"], m["body"])}],
            "output_config": {"format": {"type": "json_schema", "schema": prompts.TRIAGE_SCHEMA}},
        }})
    return submit(db, client, "triage", reqs, hashes, "triage_sent")


def on_triage(db, res, results, acc):
    for r in results:
        h = r.custom_id.split("-", 1)[1]
        data, err, _ = result_json(r)
        if err:
            fail_step(db, h, "new", err)
            continue
        importance = data["importance"] if data["is_ai"] else 1
        fields = dict(category=data["category"], vendor=data["vendor"], importance=importance,
                      reason=data["reason"], payload=json.dumps({"vendor_name": data["vendor_name"]},
                                                                ensure_ascii=False))
        if importance <= 1:
            set_stage(db, h, "rejected", **fields)
            forget_body(db, h)
        else:
            set_stage(db, h, "triaged", **fields)


# ---------- генерация ----------

def ensure_body(db, m: dict) -> str | None:
    """Если в фиде мало текста — одна докачка страницы (DeepMind, Hugging Face).
    Возвращает ошибку или None."""
    if len(m["body"]) >= MIN_BODY_CHARS:
        return None
    if m["source"] in PAGE_FETCH_SOURCES and not m["from_page"]:
        try:
            _, body, _ = collect.parse_article(collect.fetch(m["url"]), max_paragraphs=PAGE_PARAGRAPHS)
        except Exception as ex:
            return f"страница не скачалась: {type(ex).__name__}: {ex}"
        body = collect.truncate_words(body)
        if len(body) > len(m["body"]):
            db.execute("INSERT INTO article (hash, body, from_page) VALUES (?, ?, 1)"
                       " ON CONFLICT(hash) DO UPDATE SET body = excluded.body, from_page = 1",
                       (m["hash"], body))
            m["body"], m["from_page"] = body, True
    if len(m["body"]) < MIN_BODY_ANY:
        return f"мала тэксту для пераказу: {len(m['body'])} знакаў"
    return None


def generate_request(site: Path, m: dict) -> dict:
    return {"custom_id": custom_id("generate", m["hash"]), "params": {
        "model": llm.GENERATE_MODEL,
        "max_tokens": MAX_TOKENS["generate"],
        "system": prompts.cached_system(site, prompts.GENERATE_TASK),
        "messages": [{"role": "user", "content": prompts.generate_user(m)}],
        "output_config": {"effort": EFFORT["generate"],
                          "format": {"type": "json_schema", "schema": prompts.GENERATE_SCHEMA}},
    }}


def submit_generate(db, client, res: Resources, limit: int = MAX_GENERATE_PER_RUN) -> str | None:
    rows = db.execute("SELECT hash FROM item WHERE stage = 'triaged'"
                      " ORDER BY importance DESC, updated_at LIMIT ?", (limit,)).fetchall()
    reqs, hashes = [], []
    for (h,) in rows:
        m = material(db, h)
        err = ensure_body(db, m)
        if err:
            fail_step(db, h, "triaged", err)
            continue
        reqs.append(generate_request(res.site, m))
        hashes.append(h)
    db.commit()
    return submit(db, client, "generate", reqs, hashes, "generate_sent")


FIELDS = ("be_title", "thesis", "summary", "retelling")
TERM_RE = re.compile(r"\{\{term:([^|}]*)\|([^}]*)\}\}")


def check_terms(text: str, slugs: set, notes: list) -> str:
    """Маркер с неизвестным слагом снимается (слово остаётся), в заметки — запись."""
    def fix(m):
        if m.group(1) in slugs:
            return m.group(0)
        notes.append({"kind": "тэрмін", "detail": f"невядомы слаг «{m.group(1)}», маркер зняты"})
        return m.group(2)
    return TERM_RE.sub(fix, text)


def lint_card(res: Resources, tk: dict) -> list[dict]:
    """Попадания линтера по всем полям: [{field, p, s, sentence, hits}]."""
    flagged = []
    for f in FIELDS:
        paras = lint.split_paragraphs(tk[f])
        for pi, para in enumerate(paras):
            for si, sentence in enumerate(para):
                hits = res.linter.check([sentence])
                if hits:
                    flagged.append({"field": f, "p": pi, "s": si, "sentence": sentence,
                                    "hits": [(h.found, h.rule.avoid, h.rule.prefer, h.rule.note) for h in hits]})
    return flagged


def process_generated(res: Resources, drafts: dict[str, dict]) -> dict[str, dict]:
    """Сгенерированное наркамаўкай → тарашкевіца → линтер. drafts: hash → поля.
    Возвращает hash → payload (nk, tk, flagged, notes)."""
    out, fixed = {}, {}
    # Механика (латинская буква в слове, «2$», «42.92%») — кодом, до конвертера
    for h, nk in drafts.items():
        fixed[h] = []
        for f in FIELDS:
            nk[f], changes = lint.normalize(nk[f])
            fixed[h] += [{"kind": "аўтавыпраўленьне", "detail": f"{f}: {c}"} for c in changes]
    order = [(h, f) for h in drafts for f in FIELDS]
    converted = tarask.convert([drafts[h][f] for h, f in order], res.guards)
    tk_all = {}
    for (h, f), text in zip(order, converted):
        tk_all.setdefault(h, {})[f] = text
    for h, nk in drafts.items():
        tk = tk_all[h]
        notes = fixed[h]
        for f in FIELDS:
            for n in tarask.suspicious_changes(nk[f], tk[f], res.guards):
                notes.append({"kind": "канвертар", "detail": f"{f}: «{n['before']}» → «{n['after']}» ({n['kind']})"})
        out[h] = {"nk": nk, "tk": tk, "flagged": lint_card(res, tk), "notes": notes}
    return out


def on_generate(db, res, results, acc):
    drafts, term_notes = {}, {}
    for r in results:
        h = r.custom_id.split("-", 1)[1]
        data, err, _ = result_json(r)
        if err:
            fail_step(db, h, "triaged", err)
            continue
        term_notes[h] = []
        nk = {f: check_terms(data[f].strip(), res.slugs, term_notes[h]) for f in FIELDS}
        # В заголовке маркеров быть не должно — если модель всё же поставила, снимаем
        nk["be_title"] = TERM_RE.sub(lambda m: m.group(2), nk["be_title"])
        drafts[h] = nk
    for h, p in process_generated(res, drafts).items():
        p["notes"] = term_notes[h] + p["notes"]
        prev = json.loads(db.execute("SELECT payload FROM item WHERE hash = ?", (h,)).fetchone()[0] or "{}")
        payload = {**prev, **p}
        if p["flagged"]:
            set_stage(db, h, "linted", payload=json.dumps(payload, ensure_ascii=False))
        else:
            finalize(db, h, payload)


# ---------- fix ----------

def submit_fix(db, client, res: Resources) -> str | None:
    reqs, hashes = [], []
    for h, raw in db.execute("SELECT hash, payload FROM item WHERE stage = 'linted'").fetchall():
        p = json.loads(raw)
        flagged = []
        for i, fl in enumerate(p["flagged"], 1):
            nk_paras = lint.split_paragraphs(p["nk"][fl["field"]])
            tk_paras = lint.split_paragraphs(p["tk"][fl["field"]])
            if [len(x) for x in nk_paras] != [len(x) for x in tk_paras]:
                # Конвертер изменил разбивку на предложения — пару не сопоставить
                fl["skip"] = "разбіўка на сказы да і пасля канвертара не супала"
                continue
            fl["n"] = i
            flagged.append({"n": i, "sentence": nk_paras[fl["p"]][fl["s"]], "hits": fl["hits"]})
        if not flagged:
            p["lint_left"] = p["flagged"]   # править нечем — пусть видно на ревью
            finalize(db, h, p)
            continue
        card_text = "\n\n".join(p["nk"][f] for f in FIELDS)
        reqs.append({"custom_id": custom_id("fix", h), "params": {
            "model": llm.GENERATE_MODEL,
            "max_tokens": MAX_TOKENS["fix"],
            "system": prompts.cached_system(res.site, prompts.FIX_TASK),
            "messages": [{"role": "user", "content": prompts.fix_user(card_text, flagged)}],
            "output_config": {"effort": EFFORT["fix"],
                              "format": {"type": "json_schema", "schema": prompts.FIX_SCHEMA}},
        }})
        hashes.append(h)
        set_stage(db, h, "linted", payload=json.dumps(p, ensure_ascii=False))
    db.commit()
    return submit(db, client, "fix", reqs, hashes, "fix_sent")


def apply_fixes(res: Resources, p: dict, fixes: list[dict]) -> dict:
    """Исправленные предложения (наркамаўка) → конвертер → на место в тексте."""
    by_n = {f["n"]: f for f in fixes}
    todo = [fl for fl in p["flagged"] if "n" in fl and fl["n"] in by_n and by_n[fl["n"]]["changed"]]
    for fl in todo:
        by_n[fl["n"]]["sentence"], _ = lint.normalize(by_n[fl["n"]]["sentence"].strip())
    converted = tarask.convert([by_n[fl["n"]]["sentence"] for fl in todo], res.guards)
    nk = {f: lint.split_paragraphs(p["nk"][f]) for f in FIELDS}
    tk = {f: lint.split_paragraphs(p["tk"][f]) for f in FIELDS}
    for fl, conv in zip(todo, converted):
        nk[fl["field"]][fl["p"]][fl["s"]] = by_n[fl["n"]]["sentence"].strip()
        tk[fl["field"]][fl["p"]][fl["s"]] = conv
        for n in tarask.suspicious_changes(by_n[fl["n"]]["sentence"], conv, res.guards):
            p["notes"].append({"kind": "канвертар", "detail": f"{fl['field']} (fix): «{n['before']}» → «{n['after']}»"})
    for f in FIELDS:
        p["nk"][f] = lint.join_paragraphs(nk[f])
        p["tk"][f] = lint.join_paragraphs(tk[f])
    # Что осталось после fix: либо модель сочла срабатывание ложным, либо не справилась
    p["fix_log"] = [{"field": fl["field"], "before": fl["sentence"],
                     "changed": bool(fl.get("n") in by_n and by_n[fl["n"]]["changed"]),
                     "hits": [x[1] for x in fl["hits"]], "skip": fl.get("skip")} for fl in p["flagged"]]
    p["lint_left"] = lint_card(res, p["tk"])
    return p


def on_fix(db, res, results, acc):
    for r in results:
        h = r.custom_id.split("-", 1)[1]
        data, err, _ = result_json(r)
        if err:
            fail_step(db, h, "linted", err)
            continue
        p = json.loads(db.execute("SELECT payload FROM item WHERE hash = ?", (h,)).fetchone()[0])
        finalize(db, h, apply_fixes(res, p, data["fixes"]))


# ---------- карточка ----------

def build_card(db, h: str, p: dict) -> dict:
    m = material(db, h)
    item = db.execute("SELECT category, vendor, importance FROM item WHERE hash = ?", (h,)).fetchone()
    notes = list(p.get("notes", []))
    if m["from_page"]:
        notes.append({"kind": "крыніца", "detail": "тэкст дакачаны са старонкі: у фідзе было мала"})
    for fl in p.get("lint_left", []):
        notes.append({"kind": "лінтар", "detail": f"{fl['field']}: засталося {', '.join(x[1] for x in fl['hits'])}"
                      f" — «{fl['sentence'][:120]}»"})
    return {
        "id": h,
        "status": "draft",
        "published_at": m["published_at"],
        "vendor": item[1],
        "vendor_name": p.get("vendor_name", ""),
        "category": item[0],
        "importance": item[2],
        "sources": m["sources"],
        # Тарашкевіца, термины размечены {{term:slug|форма}} — подстановка при рендере (этап 6)
        "be_title": tarask.strip_names(p["tk"]["be_title"]),
        "thesis": tarask.strip_names(p["tk"]["thesis"]),
        "summary": tarask.strip_names(p["tk"]["summary"]),
        "retelling": tarask.strip_names(p["tk"]["retelling"]),
        "review_notes": notes,
        # Что пометил линтер и что сделал fix: видно, где срабатывания ложные
        "lint": p.get("fix_log", []),
        # Что сгенерировала модель до конвертера — чтобы разбирать ошибки конвертера
        "narkamauka": p["nk"],
        "generated_at": now_iso(),
        "models": {"triage": llm.TRIAGE_MODEL, "generate": llm.GENERATE_MODEL},
    }


def finalize(db, h: str, p: dict):
    card = build_card(db, h, p)
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    (CARDS_DIR / f"{h}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    set_stage(db, h, "draft", payload=None, error=None)
    forget_body(db, h)


# ---------- прогон ----------

def status(db):
    print("Материалы по шагам:")
    for stage, n in db.execute("SELECT stage, COUNT(*) FROM item GROUP BY stage ORDER BY stage"):
        print(f"  {stage:14} {n}")
    for bid, stage, sub in db.execute(
            "SELECT id, stage, submitted_at FROM batch WHERE collected_at IS NULL"):
        print(f"  в работе: батч {stage} {bid} с {sub}")
    spent = sum(json.loads(u)["usd"] for (u,) in db.execute("SELECT usage FROM batch WHERE usage IS NOT NULL"))
    print(f"Потрачено по забранным батчам: ≈ ${spent:.4f}")


def run_once(db, client, res: Resources, limit: int) -> int:
    pending = collect_batches(db, client, res)
    enroll_new(db, datetime.now(timezone.utc))
    for sub in (submit_triage(db, client), submit_generate(db, client, res, limit), submit_fix(db, client, res)):
        pending += sub is not None
    return pending


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", default=str(collect.STATE_PATH), help="путь к файлу состояния")
    ap.add_argument("--wait", action="store_true", help="ждать батчи и доводить до конца")
    ap.add_argument("--poll", type=int, default=60, help="секунд между проверками при --wait")
    ap.add_argument("--max-wait", type=int, default=120, help="минут ждать при --wait, потом выйти")
    ap.add_argument("--limit", type=int, default=MAX_GENERATE_PER_RUN, help="генераций за запуск")
    ap.add_argument("--status", action="store_true", help="только показать состояние")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    db = collect.open_state(Path(args.state))
    if args.status:
        status(db)
        return
    client = llm.make_client()
    res = Resources(site_repo())
    deadline = time.monotonic() + args.max_wait * 60
    while True:
        pending = run_once(db, client, res, args.limit)
        if not args.wait or not pending or time.monotonic() > deadline:
            break
        time.sleep(args.poll)
    status(db)


if __name__ == "__main__":
    main()
