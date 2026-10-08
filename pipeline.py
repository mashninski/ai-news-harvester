#!/usr/bin/env python3
"""
Пайплайн карточек для mashninski.com/naviny — этап 4 плана.

Берёт из состояния коллектора (data/state.sqlite) новые материалы
(seen.status = 'new') и доводит каждый до черновика карточки:

  triage    Haiku 4.5, Batch API: ИИ или нет, категория, вендор, важность 1–3.
            Важность 1 отсекается
  generate  Sonnet 5, Batch API: заголовок, тезис, summary, пересказ наркамаўкай,
            термины глоссария размечены {{term:slug|форма}}
  convert   только при AI_NEWS_ORTHOGRAPHY=tarask: taraskevizer + guard-маркеры
            (tarask.py), наркамаўка → тарашкевіца. По умолчанию (narkamauka)
            шага нет: на сайт идёт текст модели как есть
  lint      антикальки (lint.py), обычный код — по тексту, который пойдёт на сайт
  fix       Sonnet 5, Batch API: только помеченные предложения
  draft     output/cards/<hash>.json, status = draft

Batch API асинхронный: отправленное сегодня приходит через минуты или часы.
Поэтому каждый запуск делает одно и то же: сначала забирает готовые батчи,
отправленные прошлыми запусками (таблица batch), потом отправляет новые на
всё, что ждёт своего шага (таблица item). Запуск можно повторять сколько
угодно — ничего не отправится дважды и не потеряется при падении процесса.

Генерация и fix — только по флагам --generate и --fix: в Actions это один
прогон за цикл для каждого, циклов два в сутки (журнал сайта, 03.10.2026,
«Этап 8г»; 07.10.2026, «Этап 9»). Каждый батч пишет справочный блок в кэш
заново: батч в каждом прогоне платил бы лишние записи. Triage — в каждом запуске: он без кэша.

    python pipeline.py                      # забрать готовое, отправить triage
    python pipeline.py --generate --fix     # то же плюс генерация и fix
    python pipeline.py --wait --generate --fix   # ходить по кругу, пока все батчи не вернутся
    python pipeline.py --status   # только показать, что где лежит и сколько потрачено

Предохранители бюджета (журнал сайта, 02.10.2026, «Этап 8а»; пересмотрены
03.10.2026, «потолок 10 в сутки снят»): не больше MAX_GENERATE_PER_RUN
генераций за запуск и MAX_GENERATE_PER_DAY за сутки по Мінску — аварийные
потолки, а не норма: лента берёт всё, что прошло triage; месячный бюджет
AI_NEWS_MONTHLY_BUDGET_USD (по умолчанию $28) по оценкам таблицы batch
×COST_FACTOR (сейчас 1,0 — без множителя). Бюджет исчерпан — новые батчи
не отправляются, готовые забираются, выход с кодом 0 и предупреждением.

Ключ — переменная окружения AI_NEWS_ANTHROPIC_KEY (llm.py).
Орфография карточек — AI_NEWS_ORTHOGRAPHY: narkamauka (по умолчанию) или tarask.
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
import publish
import tarask

ROOT = Path(__file__).resolve().parent
CARDS_DIR = ROOT / "output" / "cards"

MAX_ATTEMPTS = 3            # столько раз этап может не удаться, потом failed
# Аварийный потолок генераций за сутки по Мінску, считается по таблице batch.
# Не норма: лента берёт всё, что прошло triage (решение автора 03.10.2026,
# журнал сайта, «потолок 10 в сутки снят»). 40 — вдвое выше ожидаемых ~20
# в сутки: ловит ошибку вроде повторной генерации всей очереди, а лишнее
# в день наплыва новостей уходит в следующий суточный батч
MAX_GENERATE_PER_DAY = 40
# Потолок за запуск — не меньше суточного: суточный батч должен взять все 40
MAX_GENERATE_PER_RUN = MAX_GENERATE_PER_DAY
# Месячный бюджет API по оценкам пайплайна. Решение автора 07.10.2026 (журнал
# сайта, «два цикла в сутки»; ai-news-plan.md сайта, «Этап 9»): по факту
# 03–07.10 карточка стоит $0,05–0,07, два цикла — ≈ $20–28/мес; лимит консоли
# $30 — последняя линия. До 07.10.2026 было 16 (этап 8г)
BUDGET_ENV = "AI_NEWS_MONTHLY_BUDGET_USD"
DEFAULT_MONTHLY_BUDGET = 28.0
# Множитель к оценке usage_cost. До 03.10.2026 был 1,5 (оценка занижала счёт
# консоли в 1,6 раза, журнал сайта, 25.09.2026); с 03.10.2026 — 1,0, решение
# и причина — та же запись журнала, что и для бюджета
COST_FACTOR = 1.0
# Fix — не чаще раза за цикл (журнал сайта, 07.10.2026, «Этап 9»: два цикла
# в сутки, fix в 01:17 и 13:17 UTC): каждый батч fix пишет справочный блок
# в кэш заново. Расписание и так даёт fix один прогон за цикл; это страховка
# от повтора того же прогона (запасной прогон GitHub после упавшего прогона
# Worker'а, перезапуск руками). 6 часов, а не 12: прогон в очереди опаздывает
FIX_MIN_INTERVAL = timedelta(hours=6)
MINSK = timezone(timedelta(hours=3))   # в Беларуси перевода часов нет
MIN_BODY_CHARS = 400        # меньше — текста для пересказа мало
MIN_BODY_ANY = 150          # меньше и докачать нельзя — пересказывать нечего
# Докачка страницы, когда в фиде мало текста. Только эти источники: у DeepMind
# и Hugging Face в фиде заголовок и пустой body (журнал сайта, 24.09.2026, этап 3),
# у Mistral — анонс в одно предложение, 100–160 знаков (журнал сайта, 03.10.2026,
# «Ars Technica и Mistral — берём оба»; статья — в <article id="blogpost">).
# У остальных RSS страницы не скрейпим (спека, §3, п. 4) — Anthropic и так
# берётся со страницы коллектором.
# OpenAI — с 07.10.2026 (рашэнне аўтара, журнал сайта, «этап 9»): в фиде
# анонс в 130–150 знаков, меньше MIN_BODY_ANY, и ~4 новости из 10 уходили
# в failed с «мала тэксту»; robots.txt разрешает всё
PAGE_FETCH_SOURCES = {"deepmind", "huggingface", "mistral", "openai"}
PAGE_PARAGRAPHS = 12

# Цены на 24.09.2026, $ за миллион токенов (спека, §4). Batch — половина.
# Запись в кэш — 1,25× входа (5 минут) или 2× (час), чтение — 0.1×; usage_cost.
# Только для отчёта о тратах и бюджета.
PRICES = {llm.TRIAGE_MODEL: (1.0, 5.0), llm.GENERATE_MODEL: (2.0, 10.0)}

# Sonnet 5 по умолчанию думает (adaptive thinking), и размышление идёт в тот же
# лимит, что и ответ: при 4000 токенов 17 генераций из 20 на корпусе обрезались
# посреди размышления (журнал сайта, 24.09.2026). Поэтому лимит с запасом,
# а глубина размышления задаётся явно — effort
MAX_TOKENS = {"triage": 400, "generate": 16000, "fix": 8000, "en": 6000}
EFFORT = {"generate": "medium", "fix": "low", "en": "low"}   # генерация — язык, на ней не экономим (спека, §3)

# Орфография карточек. Тарашкевіца выключена решением автора 02.10.2026 (журнал
# сайта): конвертер taraskevizer ошибается, свой ещё не написан. narkamauka —
# на сайт идёт текст модели как есть, Node не нужен; tarask — прежний путь через
# конвертер. Код tarask.py и guard-словарь остаются: вернуть тарашкевіцу —
# одна переменная, когда будет свой конвертер
ORTHOGRAPHY_ENV = "AI_NEWS_ORTHOGRAPHY"
ORTHOGRAPHIES = ("narkamauka", "tarask")

log = logging.getLogger("pipeline")


# ---------- окружение ----------

def site_repo() -> Path:
    p = Path(os.environ.get("AI_NEWS_SITE_REPO") or ROOT.parent / "mashninski-site")
    if not (p / "claude" / "ai-news-glossary.json").exists():
        raise SystemExit(f"нет репозитория сайта с глоссарием: {p} (задай AI_NEWS_SITE_REPO)")
    return p


def orthography() -> str:
    o = (os.environ.get(ORTHOGRAPHY_ENV) or ORTHOGRAPHIES[0]).strip()
    if o not in ORTHOGRAPHIES:
        raise SystemExit(f"{ORTHOGRAPHY_ENV}={o!r}: можна {' або '.join(ORTHOGRAPHIES)}")
    return o


class Resources:
    """Глоссарий, guard-словарь и линтер — читаются один раз за запуск.
    Орфография — аргументом или из AI_NEWS_ORTHOGRAPHY."""

    def __init__(self, site: Path, ortho: str | None = None):
        c = site / "claude"
        self.site = site
        self.orthography = ortho or orthography()
        self.tarask = self.orthography == "tarask"
        if self.tarask:
            self.guards = tarask.Guards.load(c / "ai-news-converter-guards.json")
            self.linter = lint.Linter.load(c / "ai-news-anti-calques.json")
        else:
            # Без конвертера фраза словаря ищется только так, как записана, —
            # наркамаўкай, как пишет и модель. Node не вызывается
            self.guards = None
            self.linter = lint.Linter.load(c / "ai-news-anti-calques.json", convert=list)
        glossary = json.loads((c / "ai-news-glossary.json").read_text(encoding="utf-8"))["entries"]
        self.slugs = {prompts.slug(e["term"]) for e in glossary}

    def convert(self, texts: list[str]) -> list[str]:
        """Текст модели → текст на сайт. При наркамаўке — как есть."""
        return tarask.convert(texts, self.guards) if self.tarask else list(texts)

    def suspicious(self, before: str, after: str) -> list[dict]:
        """Подозрительные замены конвертера. Без конвертера замен нет."""
        return tarask.suspicious_changes(before, after, self.guards) if self.tarask else []


def now_iso() -> str:
    return collect.iso(datetime.now(timezone.utc))


# ---------- бюджет ----------

def monthly_budget() -> float:
    raw = (os.environ.get(BUDGET_ENV) or "").strip().replace(",", ".")
    if not raw:
        return DEFAULT_MONTHLY_BUDGET
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{BUDGET_ENV}={raw!r}: патрэбны лік у доларах, напрыклад 28")
    if value < 0:
        raise SystemExit(f"{BUDGET_ENV}={raw!r}: бюджэт не можа быць адмоўным")
    return value


def minsk_bounds(now: datetime) -> tuple[str, str]:
    """Начало суток и календарного месяца по Мінску — в UTC, как в таблице batch."""
    local = now.astimezone(MINSK)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return collect.iso(day), collect.iso(day.replace(day=1))


def generated_since(db, since: str) -> int:
    """Сколько генераций отправлено с момента since — и удачных, и нет: платим за каждую."""
    return db.execute("SELECT COALESCE(SUM(requests), 0) FROM batch WHERE stage = 'generate'"
                      " AND submitted_at >= ?", (since,)).fetchone()[0]


def spent_since(db, since: str) -> float:
    """Оценка в $ по забранным батчам, отправленным с момента since (без ×COST_FACTOR)."""
    return sum(json.loads(u)["usd"] for (u,) in db.execute(
        "SELECT usage FROM batch WHERE usage IS NOT NULL AND submitted_at >= ?", (since,)))


def budget_state(db, now: datetime) -> dict:
    day, month = minsk_bounds(now)
    budget = monthly_budget()
    spent = spent_since(db, month) * COST_FACTOR
    return {"budget": budget, "spent": spent, "left": max(0.0, budget - spent),
            "exhausted": spent >= budget, "today": generated_since(db, day), "month": month}


def fix_sent_recently(db, now: datetime) -> bool:
    since = collect.iso(now - FIX_MIN_INTERVAL)
    return db.execute("SELECT 1 FROM batch WHERE stage = 'fix' AND submitted_at >= ? LIMIT 1",
                      (since,)).fetchone() is not None


def waiting_by_day(db) -> list[tuple[str, int]]:
    """Отобранное triage и ждущее генерации — по дням публикации по Мінску,
    старые первыми: [(«01.10», 5), …]."""
    days: dict[str, int] = {}
    for (pub,) in db.execute("SELECT COALESCE(s.published_at, s.first_seen_at) FROM item i"
                             " JOIN seen s ON s.hash = i.hash WHERE i.stage = 'triaged'"):
        key = collect.parse_iso(pub).astimezone(MINSK).strftime("%Y-%m-%d")
        days[key] = days.get(key, 0) + 1
    return [(f"{k[8:10]}.{k[5:7]}", n) for k, n in sorted(days.items())]


def waiting_line(db) -> str:
    days = waiting_by_day(db)
    total = sum(n for _, n in days)
    if not total:
        return "Отобрано и ждёт генерации: 0"
    return (f"Отобрано и ждёт генерации: {total} — по дням публикации (Мінск): "
            + ", ".join(f"{d}: {n}" for d, n in days))


def budget_line(b: dict) -> str:
    return (f"Бюджет месяца: потрачено ≈ ${b['spent']:.4f} (оценка ×{COST_FACTOR}) из ${b['budget']:.2f},"
            f" осталось ≈ ${b['left']:.4f}; генераций за сутки по Мінску: {b['today']} из {MAX_GENERATE_PER_DAY}")


def announce(text: str, warning: bool = False, echo: bool = True):
    """Строка в лог и, в Actions, в summary прогона. Предупреждение — ещё и
    аннотацией ::warning::: видно на странице прогона, но прогон не красный —
    иначе исчерпанный бюджет слал бы письмо 12 раз в сутки."""
    if echo:
        print(text)
    if warning and os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{text}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(("**⚠ " + text + "**" if warning else text) + "\n\n")


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
    if stage == "failed":
        forget_body(db, h)      # дальше материал никуда не пойдёт
    log.warning("%s: %s → %s (попытка %d): %s", h[:8], back_to, stage, attempts, error[:200])
    if stage == "failed":       # повторная попытка — рабочий шум, а снятый материал — уже потеря
        collect.actions_warning(f"{h[:8]}: снят после {attempts} попыток ({back_to}): {error[:200]}")


def forget_body(db, h: str):
    """Текст статьи больше не нужен: в состоянии остаётся только нужное для дедупа.
    Вместе с ним — тексты его повторов из других источников."""
    db.execute("UPDATE article SET body = '' WHERE hash = ? OR hash IN"
               " (SELECT hash FROM seen WHERE primary_hash = ?)", (h, h))


def material(db, h: str) -> dict:
    row = db.execute(
        "SELECT s.source, s.url, s.title, s.published_at, COALESCE(a.body, ''), COALESCE(a.from_page, 0)"
        " FROM seen s LEFT JOIN article a ON a.hash = s.hash WHERE s.hash = ?", (h,)).fetchone()
    source, url, title, pub, body, from_page = row
    # Повторы из других источников; текст есть только у пришедших в одном прогоне
    # с основным (collect.collect) — у поздних и старых записей пусто
    dups = db.execute(
        "SELECT s.source, s.url, s.title, COALESCE(a.body, '') FROM seen s LEFT JOIN article a ON a.hash = s.hash"
        " WHERE s.primary_hash = ? ORDER BY s.published_at", (h,)).fetchall()
    return {"hash": h, "source": source, "url": url, "title": title or "", "published_at": pub,
            "body": body, "from_page": bool(from_page),
            "also": [(s, t, b) for s, _, t, b in dups],
            "sources": [{"source": source, "url": url, "title": title, "primary": True}]
                       + [{"source": s, "url": u, "title": t, "primary": False} for s, u, t, _ in dups]}


def enroll_new(db, now: datetime) -> int:
    """Новые материалы коллектора → строки item. Старше окна свежести — too_old:
    пайплайн мог не запускаться неделю, старьё в ленту не нужно."""
    cutoff = collect.iso(now - timedelta(days=collect.MAX_AGE_DAYS))
    rows = db.execute(
        "SELECT hash, COALESCE(published_at, first_seen_at) FROM seen"
        " WHERE status = 'new' AND hash NOT IN (SELECT hash FROM item)").fetchall()
    stamp = now_iso()
    for h, pub in rows:
        stage = "new" if pub >= cutoff else "too_old"
        db.execute("INSERT INTO item (hash, stage, updated_at) VALUES (?, ?, ?)", (h, stage, stamp))
        if stage == "too_old":
            forget_body(db, h)
    db.commit()
    return len(rows)


# ---------- батчи ----------

def custom_id(stage: str, h: str) -> str:
    return f"{stage[0]}-{h}"   # t-/g-/f-/e- и sha1: 42 знака, в лимит API (64) влезает


def submit(db, client, stage: str, requests: list[dict], hashes: list[str], next_stage: str):
    if not requests:
        return None
    batch = client.messages.batches.create(requests=requests)
    # В requests батча — материалы, а не запросы: у генерации на материал два
    # запроса (беларуский и английский, e-…), а суточный потолок генераций
    # (generated_since) — про беларуские (ai-news-en-spec.md сайта, §4)
    db.execute("INSERT INTO batch (id, stage, requests, submitted_at) VALUES (?, ?, ?, ?)",
               (batch.id, stage, len(hashes), now_iso()))
    for h in hashes:
        set_stage(db, h, next_stage, batch_id=batch.id)
    db.commit()
    extra = f" (з іх англійскіх {len(requests) - len(hashes)})" if len(requests) > len(hashes) else ""
    print(f"Отправлен батч {stage}: {batch.id}, запросов {len(requests)}{extra}")
    return batch.id


def usage_add(acc: dict, model: str, u):
    a = acc.setdefault(model, {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                               "cache_write_1h": 0})
    a["input"] += u.input_tokens or 0
    a["output"] += u.output_tokens or 0
    a["cache_write"] += u.cache_creation_input_tokens or 0
    a["cache_read"] += u.cache_read_input_tokens or 0
    # Часовая запись дороже 5-минутной; разбивку по TTL отдаёт сам ответ
    split = getattr(u, "cache_creation", None)
    a["cache_write_1h"] = a.get("cache_write_1h", 0) + (getattr(split, "ephemeral_1h_input_tokens", 0) or 0)


def usage_cost(acc: dict) -> float:
    """Оценка в $ с учётом скидки Batch API. Запись в кэш — по её TTL:
    5 минут — 1,25× входа, час — 2× (разбивка из ответа, `cache_write_1h`);
    чтение — 0,1×."""
    total = 0.0
    for model, a in acc.items():
        # В ответе модель бывает с датой: claude-haiku-4-5-20251001
        pin, pout = next((v for k, v in PRICES.items() if model.startswith(k)), (0, 0))
        w1h = a.get("cache_write_1h", 0)
        total += (a["input"] * pin + (a["cache_write"] - w1h) * pin * 1.25 + w1h * pin * 2
                  + a["cache_read"] * pin * 0.1 + a["output"] * pout) / 1e6 * 0.5
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
        en_acc: dict = {}       # английские запросы генерации отдельно — их цена в логе своей строкой
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
                if r.custom_id.startswith("e-"):
                    usage_add(en_acc, r.result.message.model, r.result.message.usage)
        handler(db, res, results, acc)
        # Материалы батча, по которым результата не пришло вовсе, — назад
        seen_ids = {r.custom_id for r in results}
        back = {"triage": "new", "generate": "triaged", "fix": "linted"}[stage]
        for (h,) in db.execute("SELECT hash FROM item WHERE batch_id = ? AND stage = ?",
                               (batch_id, sent)).fetchall():
            if custom_id(stage, h) not in seen_ids:
                fail_step(db, h, back, "результата в батче нет")
        cost = usage_cost(acc)
        usage = {"tokens": acc, "usd": round(cost, 4)}
        en_n = sum(r.custom_id.startswith("e-") for r in results)
        if en_n:
            usage["en_usd"] = round(usage_cost(en_acc), 4)
        db.execute("UPDATE batch SET collected_at = ?, usage = ? WHERE id = ?",
                   (now_iso(), json.dumps(usage), batch_id))
        db.commit()
        print(f"Забран батч {stage} {batch_id}: {len(results)} результатов, ≈ ${cost:.4f}"
              + tokens_note(acc))
        if en_n:
            # Цена английского текста отдельно — сверка с оценкой ≈ $0,01 на карточку
            # (ai-news-en-plan.md сайта, §1); она уже входит в сумму строкой выше
            print(f"  из них английский текст: {en_n} результатов, ≈ ${usage['en_usd']:.4f}"
                  + tokens_note(en_acc))
    return pending


def tokens_note(acc: dict) -> str:
    """Токены батча для лога: вход, ответ (с размышлением), запись и чтение
    кэша. По записи и чтению видно, делят ли запросы батча кэш справочного
    блока (открытый вопрос в open-questions.md сайта): общий — одна запись
    и чтения у остальных, нет — запись у каждого."""
    t = {k: sum(a[k] for a in acc.values()) for k in ("input", "output", "cache_write", "cache_read")}
    if not any(t.values()):
        return ""
    return (f"; токены: вход {t['input']}, ответ {t['output']},"
            f" кэш — запись {t['cache_write']}, чтение {t['cache_read']}")


# ---------- triage ----------

# Дубль по смыслу решает triage, а не слова заголовков (этап 11, ai-news-plan.md
# сайта; рашэнне аўтара 08.10.2026): склейка коллектора одно событие в разных
# словах не ловит, а материалы одного источника не сравнивает вовсе — две
# карточки 02.10 про указ Трампа были подкастом и видео одного TechCrunch.
# Haiku и так читает каждый материал, ему добавляется список «что уже есть»:
# отобранное triage раньше — и дальше по пайплайну, опубликованное тоже
LISTED_STAGES = ("triaged", "generate_sent", "linted", "fix_sent", "draft")
# Список — в окне склейки от даты материала, ближние по времени первыми. При
# ~20 карточках в сутки в окно 72 часа входит ~60; потолок — от наплыва
TRIAGE_KNOWN_MAX = 100


def item_dates(db, hashes) -> dict[str, str]:
    marks = ",".join("?" * len(hashes))
    return dict(db.execute(f"SELECT hash, COALESCE(published_at, first_seen_at) FROM seen"
                           f" WHERE hash IN ({marks})", list(hashes)).fetchall())


def triage_known(db, hashes: list[str]) -> dict[str, list[str]]:
    """Для каждого материала пакета — hash'и «что уже есть»: отобранное раньше
    (LISTED_STAGES) и материалы этого же пакета, вышедшие раньше него. Только
    раньше: иначе два материала пакета могли бы назвать дублем друг друга,
    и карточки не вышло бы ни у одного."""
    if not hashes:
        return {}
    window = timedelta(hours=collect.CLUSTER_WINDOW_HOURS)
    own = sorted((d, h) for h, d in item_dates(db, hashes).items())
    lo = collect.iso(collect.parse_iso(own[0][0]) - window)
    marks = ",".join("?" * len(LISTED_STAGES))
    # Источник только для повторов сам основным не бывает: отобранное из него
    # до правила снимает submit_generate, и дубль на него остался бы без карточки
    secondary = ",".join("?" * len(collect.SECONDARY_ONLY))
    listed = db.execute(f"SELECT COALESCE(s.published_at, s.first_seen_at), s.hash FROM item i"
                        f" JOIN seen s ON s.hash = i.hash WHERE i.stage IN ({marks}) AND s.status = 'new'"
                        f" AND s.source NOT IN ({secondary}) AND COALESCE(s.published_at, s.first_seen_at) >= ?",
                        (*LISTED_STAGES, *collect.SECONDARY_ONLY, lo)).fetchall()
    out = {}
    for k, (d, h) in enumerate(own):
        at = collect.parse_iso(d)
        near = sorted((abs(collect.parse_iso(d2) - at), h2) for d2, h2 in [*listed, *own[:k]]
                      if abs(collect.parse_iso(d2) - at) <= window)
        out[h] = [h2 for _, h2 in near[:TRIAGE_KNOWN_MAX]]
    return out


def submit_triage(db, client) -> str | None:
    hashes = [h for (h,) in db.execute("SELECT hash FROM item WHERE stage = 'new'").fetchall()]
    known = triage_known(db, hashes)
    reqs = []
    for h in hashes:
        m = material(db, h)
        listed = [db.execute("SELECT source, COALESCE(title, '') FROM seen WHERE hash = ?", (k,)).fetchone()
                  for k in known[h]]
        reqs.append({"custom_id": custom_id("triage", h), "params": {
            "model": llm.TRIAGE_MODEL,
            "max_tokens": MAX_TOKENS["triage"],
            "system": prompts.TRIAGE_SYSTEM,
            "messages": [{"role": "user", "content": prompts.triage_user(m["source"], m["title"], m["body"],
                                                                         listed)}],
            "output_config": {"format": {"type": "json_schema", "schema": prompts.TRIAGE_SCHEMA}},
        }})
        # Номер в ответе (same_as) — позиция в этом списке; ответ придёт в другом запуске
        db.execute("UPDATE item SET payload = ? WHERE hash = ?", (json.dumps({"triage_known": known[h]}), h))
    return submit(db, client, "triage", reqs, hashes, "triage_sent")


def primary_of(db, h: str, decided: dict[str, str | None], depth: int = 0) -> str | None:
    """Куда ведёт дубль на h: сам h, если он отобран, его основной, если он
    сам повтор, или None — h отсеян или ещё не разобран. decided — исход
    материалов этого пакета: они разбираются по порядку выхода, раньше —
    первыми, так что исход того, на кого можно сослаться, уже известен."""
    if h in decided:
        return decided[h]
    row = db.execute("SELECT s.status, s.primary_hash, i.stage FROM seen s LEFT JOIN item i ON i.hash = s.hash"
                     " WHERE s.hash = ?", (h,)).fetchone()
    if not row:
        return None
    status, primary, stage = row
    if status == "duplicate":
        return primary_of(db, primary, decided, depth + 1) if primary and depth < 3 else None
    return h if stage in LISTED_STAGES else None


def same_event(db, data: dict, known: list[str], decided: dict) -> str | None:
    """Основной, повтором которого triage счёл материал. Тот же пересказ —
    повтор; новые факты или разбор (adds_new) — своя карточка."""
    n = data.get("same_as") or 0
    if not n or data.get("adds_new"):
        return None
    if not 1 <= n <= len(known):
        log.warning("triage: same_as = %s, а ў спісе %d", n, len(known))
        return None
    return primary_of(db, known[n - 1], decided)


def mark_duplicate(db, h: str, primary: str, fields: dict):
    """Повтор события: как повтор склейки коллектора — в seen duplicate
    с primary_hash, своей карточки нет; на сайте он — ссылка в «Крыніцы»
    основного (publish.duplicate_sources). Его повторы склейки переходят
    к основному. Текст остаётся, только если основной ещё ждёт генерации:
    тогда он уйдёт в неё вторым источником (prompts.generate_user)."""
    if db.execute("SELECT stage FROM item WHERE hash = ?", (primary,)).fetchone()[0] != "triaged":
        forget_body(db, h)
    db.execute("UPDATE seen SET status = 'duplicate', primary_hash = ? WHERE hash = ?", (primary, h))
    db.execute("UPDATE seen SET primary_hash = ? WHERE primary_hash = ?", (primary, h))
    set_stage(db, h, "rejected", **fields, error=f"паўтор {primary}")
    (s1, t1), (s2, t2) = (db.execute("SELECT source, COALESCE(title, '') FROM seen WHERE hash = ?", (x,)).fetchone()
                          for x in (h, primary))
    announce(f"Паўтор па сэнсе: {h[:8]} {s1} «{t1}» → {primary[:8]} {s2} «{t2}»")


def on_triage(db, res, results, acc):
    dates = item_dates(db, [r.custom_id.split("-", 1)[1] for r in results]) if results else {}
    decided: dict[str, str | None] = {}      # материал пакета → сам (отобран), основной (повтор), None
    for r in sorted(results, key=lambda r: (dates.get(r.custom_id.split("-", 1)[1], ""), r.custom_id)):
        h = r.custom_id.split("-", 1)[1]
        decided[h] = None
        data, err, _ = result_json(r)
        if err:
            fail_step(db, h, "new", err)
            continue
        known = json.loads(db.execute("SELECT payload FROM item WHERE hash = ?", (h,)).fetchone()[0]
                           or "{}").get("triage_known", [])
        importance = data["importance"] if data["is_ai"] else 1
        fields = dict(category=data["category"], vendor=data["vendor"], importance=importance,
                      reason=data["reason"], payload=json.dumps({"vendor_name": data["vendor_name"]},
                                                                ensure_ascii=False))
        if importance <= 1:
            set_stage(db, h, "rejected", **fields)
            forget_body(db, h)
        elif primary := same_event(db, data, known, decided):
            mark_duplicate(db, h, primary, fields)
            decided[h] = primary
        else:
            set_stage(db, h, "triaged", **fields)
            decided[h] = h


# ---------- генерация ----------

def ensure_body(db, m: dict) -> str | None:
    """Если в фиде мало текста — одна докачка страницы (DeepMind, Hugging Face, Mistral, OpenAI).
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


def company(db, h: str) -> str:
    """Компания материала по triage — для английского запроса: название, которое
    дал triage, а без него — слаг вендора (openai, google…), его модель понимает."""
    vendor, payload = db.execute("SELECT vendor, payload FROM item WHERE hash = ?", (h,)).fetchone()
    name = json.loads(payload or "{}").get("vendor_name", "")
    return name or (vendor if vendor and vendor != "other" else "")


def en_request(m: dict, company_name: str) -> dict:
    """Английский текст карточки — тем же батчем, что генерация (e-<hash>):
    текст статьи живёт в состоянии только до черновика. Без справочного блока
    и кэша, effort low (ai-news-en-spec.md сайта, §4)."""
    return {"custom_id": custom_id("en", m["hash"]), "params": {
        "model": llm.GENERATE_MODEL,
        "max_tokens": MAX_TOKENS["en"],
        "system": prompts.EN_TASK,
        "messages": [{"role": "user", "content": prompts.en_user(m, company_name)}],
        "output_config": {"effort": EFFORT["en"],
                          "format": {"type": "json_schema", "schema": prompts.EN_SCHEMA}},
    }}


def generate_request(res: Resources, m: dict) -> dict:
    return {"custom_id": custom_id("generate", m["hash"]), "params": {
        "model": llm.GENERATE_MODEL,
        "max_tokens": MAX_TOKENS["generate"],
        "system": prompts.cached_system(res.site, prompts.generate_task(res.tarask), res.tarask),
        "messages": [{"role": "user", "content": prompts.generate_user(m)}],
        "output_config": {"effort": EFFORT["generate"],
                          "format": {"type": "json_schema", "schema": prompts.GENERATE_SCHEMA}},
    }}


def expire_triaged(db, now: datetime) -> int:
    """Ждущее генерации дольше окна свежести — too_old. Упрётся очередь
    в потолок генераций или бюджет — хвост иначе дождался бы своей карточки
    через неделю."""
    cutoff = collect.iso(now - timedelta(days=collect.MAX_AGE_DAYS))
    rows = db.execute("SELECT i.hash FROM item i JOIN seen s ON s.hash = i.hash WHERE i.stage = 'triaged'"
                      " AND COALESCE(s.published_at, s.first_seen_at) < ?", (cutoff,)).fetchall()
    for (h,) in rows:
        set_stage(db, h, "too_old")
        forget_body(db, h)
    db.commit()
    return len(rows)


def submit_generate(db, client, res: Resources, limit: int = MAX_GENERATE_PER_RUN) -> str | None:
    # Самые важные первыми, внутри важности — самые свежие: в день наплыва
    # отобранного больше аварийного потолка (MAX_GENERATE_PER_DAY), и «старые
    # первыми» превратили бы ленту в новости недельной давности
    rows = db.execute("SELECT i.hash FROM item i JOIN seen s ON s.hash = i.hash WHERE i.stage = 'triaged'"
                      " ORDER BY i.importance DESC, COALESCE(s.published_at, s.first_seen_at) DESC, i.updated_at"
                      " LIMIT ?", (limit,)).fetchall()
    reqs, hashes = [], []
    for (h,) in rows:
        m = material(db, h)
        # Источник только для повторов (collect.SECONDARY_ONLY) своей карточки
        # не получает; так снимаются и материалы, отобранные до этого правила
        if m["source"] in collect.SECONDARY_ONLY:
            set_stage(db, h, "rejected", error="крыніца толькі для паўтораў: у фідзе толькі анонс")
            forget_body(db, h)
            continue
        err = ensure_body(db, m)
        if err:
            fail_step(db, h, "triaged", err)
            continue
        reqs.append(generate_request(res, m))
        reqs.append(en_request(m, company(db, h)))
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
    """Сгенерированное наркамаўкай → (тарашкевіца, если включена) → линтер.
    drafts: hash → поля. Возвращает hash → payload (nk, tk, flagged, notes):
    nk — текст модели, tk — текст на сайт; при наркамаўке они совпадают."""
    out, fixed = {}, {}
    # Механика (латинская буква в слове, «2$», «42.92%») — кодом, до конвертера
    for h, nk in drafts.items():
        fixed[h] = []
        for f in FIELDS:
            nk[f], changes = lint.normalize(nk[f])
            fixed[h] += [{"kind": "аўтавыпраўленьне", "detail": f"{f}: {c}"} for c in changes]
    order = [(h, f) for h in drafts for f in FIELDS]
    converted = res.convert([drafts[h][f] for h, f in order])
    tk_all = {}
    for (h, f), text in zip(order, converted):
        tk_all.setdefault(h, {})[f] = text
    for h, nk in drafts.items():
        tk = tk_all[h]
        notes = fixed[h]
        for f in FIELDS:
            for n in res.suspicious(nk[f], tk[f]):
                notes.append({"kind": "канвертар", "detail": f"{f}: «{n['before']}» → «{n['after']}» ({n['kind']})"})
        out[h] = {"nk": nk, "tk": tk, "flagged": lint_card(res, tk), "notes": notes,
                  "orthography": res.orthography}
    return out


def english(result) -> tuple[dict | None, str | None]:
    """(поля en_* для черновика, почему их нет). Механика — кодом: точка
    в конце заголовка снимается. Сломанное — None с причиной: беларускую
    карточку оно не держит, повтора нет — текст статьи к тому времени
    стёрт (ai-news-en-spec.md сайта, §4)."""
    if result is None:
        return None, "адказу ў батчы няма"
    data, err, _ = result_json(result)
    if err:
        return None, err
    en = {f: str(data.get(f, "")).strip() for f in publish.EN_FIELDS}
    if en["en_title"].endswith(".") and not en["en_title"].endswith(".."):
        en["en_title"] = en["en_title"][:-1].rstrip()
    problems = publish.en_fatal(en) or [f"{f} пусты" for f in publish.EN_FIELDS if not en[f]]
    if any(x in en[f] for f in publish.EN_FIELDS for x in ("<!--", "http://", "https://")):
        problems.append("у тэксце HTML-камэнтар ці спасылка")
    return (None, "; ".join(problems)) if problems else (en, None)


def on_generate(db, res, results, acc):
    drafts, term_notes = {}, {}
    # Английские ответы (e-…) — к своему материалу; беларуские разбираются как раньше
    en_results = {r.custom_id.split("-", 1)[1]: r for r in results if r.custom_id.startswith("e-")}
    results = [r for r in results if not r.custom_id.startswith("e-")]
    for r in results:
        h = r.custom_id.split("-", 1)[1]
        data, err, _ = result_json(r)
        if err:
            fail_step(db, h, "triaged", err)
            continue
        cut = [f for f in FIELDS if f != "be_title" and lint.truncated(data[f])]
        if cut:
            # Обрыв не чинится ни конвертером, ни fix — только новой генерацией
            fail_step(db, h, "triaged", f"тэкст абарваны: {', '.join(cut)}")
            continue
        term_notes[h] = []
        nk = {f: check_terms(data[f].strip(), res.slugs, term_notes[h]) for f in FIELDS}
        # В заголовке маркеров быть не должно — если модель всё же поставила, снимаем
        nk["be_title"] = TERM_RE.sub(lambda m: m.group(2), nk["be_title"])
        drafts[h] = nk
    for h, p in process_generated(res, drafts).items():
        p["notes"] = term_notes[h] + p["notes"]
        en, why = english(en_results.get(h))
        if en:
            p["en"] = en
        else:
            p["notes"].append({"kind": "en", "detail": f"англійскага тэксту няма: {why}"})
            log.warning("%s: англійскага тэксту няма: %s", h[:8], why[:200])
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
            "system": prompts.cached_system(res.site, prompts.FIX_TASK, res.tarask),
            "messages": [{"role": "user", "content": prompts.fix_user(card_text, flagged)}],
            "output_config": {"effort": EFFORT["fix"],
                              "format": {"type": "json_schema", "schema": prompts.FIX_SCHEMA}},
        }})
        hashes.append(h)
        set_stage(db, h, "linted", payload=json.dumps(p, ensure_ascii=False))
    db.commit()
    return submit(db, client, "fix", reqs, hashes, "fix_sent")


def apply_fixes(res: Resources, p: dict, fixes: list[dict]) -> dict:
    """Исправленные предложения (наркамаўка) → конвертер, если включён → на место
    в тексте. При наркамаўке исправленное идёт как есть, второй конвертации нет."""
    by_n = {f["n"]: f for f in fixes}
    todo = [fl for fl in p["flagged"] if "n" in fl and fl["n"] in by_n and by_n[fl["n"]]["changed"]]
    for fl in todo:
        by_n[fl["n"]]["sentence"], _ = lint.normalize(by_n[fl["n"]]["sentence"].strip())
    converted = res.convert([by_n[fl["n"]]["sentence"] for fl in todo])
    nk = {f: lint.split_paragraphs(p["nk"][f]) for f in FIELDS}
    tk = {f: lint.split_paragraphs(p["tk"][f]) for f in FIELDS}
    for fl, conv in zip(todo, converted):
        nk[fl["field"]][fl["p"]][fl["s"]] = by_n[fl["n"]]["sentence"].strip()
        tk[fl["field"]][fl["p"]][fl["s"]] = conv
        for n in res.suspicious(by_n[fl["n"]]["sentence"], conv):
            p["notes"].append({"kind": "канвертар", "detail": f"{fl['field']} (fix): «{n['before']}» → «{n['after']}»"})
    for f in FIELDS:
        # Вычеркнутое fix'ом предложение пустое (prompts.FIX_TASK, «спасылка на
        # крыніцу»). Поле, где не осталось ничего, — как было: пустой тезис
        # не пустил бы карточку в PR, а остаток линтера виден на ревью
        if lint.join_paragraphs(tk[f]).strip():
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
        # Сколько слов источников видела генерация — для заметки о коротком
        # пересказе (publish.content_notes). На сайт не идёт: site_card — белый список
        "source_words": prompts.source_words(m),
        # Орфография — по AI_NEWS_ORTHOGRAPHY; термины размечены {{term:slug|форма}},
        # подстановка при рендере (этап 6)
        "be_title": tarask.strip_names(p["tk"]["be_title"]),
        "thesis": tarask.strip_names(p["tk"]["thesis"]),
        "summary": tarask.strip_names(p["tk"]["summary"]),
        "retelling": tarask.strip_names(p["tk"]["retelling"]),
        # Английский текст для /en/ai-naviny — когда он пришёл и прошёл проверки
        **(p.get("en") or {}),
        "review_notes": notes,
        # Что пометил линтер и что сделал fix: видно, где срабатывания ложные
        "lint": p.get("fix_log", []),
        # Что сгенерировала модель до конвертера — чтобы разбирать ошибки конвертера.
        # При наркамаўке совпадает с текстом выше
        "narkamauka": p["nk"],
        "orthography": p.get("orthography", "tarask"),
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

def status(db, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    print("Материалы по шагам:")
    for stage, n in db.execute("SELECT stage, COUNT(*) FROM item GROUP BY stage ORDER BY stage"):
        print(f"  {stage:14} {n}")
    for bid, stage, sub in db.execute(
            "SELECT id, stage, submitted_at FROM batch WHERE collected_at IS NULL"):
        print(f"  в работе: батч {stage} {bid} с {sub}")
    print(waiting_line(db))
    print(f"Потрачено по забранным батчам: ≈ ${spent_since(db, ''):.4f} (оценка, за всё время)")
    print(budget_line(budget_state(db, now)))


def run_once(db, client, res: Resources, limit: int = MAX_GENERATE_PER_RUN,
             now: datetime | None = None, run_started: str | None = None,
             generate: bool = True, fix: bool = True) -> int:
    """Забрать готовое, отправить новое в пределах потолков. run_started — начало
    процесса: потолок за запуск считается по батчам с этого момента (--wait
    проходит run_once много раз). generate и fix — можно ли в этом запуске
    отправлять батчи генерации и fix; triage идёт всегда."""
    now = now or datetime.now(timezone.utc)
    pending = collect_batches(db, client, res)
    enroll_new(db, now)
    expire_triaged(db, now)
    b = budget_state(db, now)
    if b["exhausted"]:
        # Готовое уже забрано выше, черновики дойдут до публикации; новых трат нет
        announce(f"Бюджет месяца исчерпан: ≈ ${b['spent']:.2f} из ${b['budget']:.2f} (оценка ×{COST_FACTOR}). "
                 "Новые батчи не отправляются до следующего месяца, готовые забираются, публикация идёт.",
                 warning=True)
        return pending
    this_run = generated_since(db, run_started) if run_started else 0
    allowed = max(0, min(limit - this_run, MAX_GENERATE_PER_DAY - b["today"])) if generate else 0
    if generate and allowed == 0 and db.execute("SELECT 1 FROM item WHERE stage = 'triaged' LIMIT 1").fetchone():
        why = "за сутки" if b["today"] >= MAX_GENERATE_PER_DAY else "за запуск"
        print(f"Потолок генераций {why} достигнут — генерация ждёт следующего запуска")
    fix_now = fix and not fix_sent_recently(db, now)
    if fix and not fix_now and db.execute("SELECT 1 FROM item WHERE stage = 'linted' LIMIT 1").fetchone():
        print("Fix уже отправлялся за последние сутки — исправления ждут следующего")
    for sub in (submit_triage(db, client),
                submit_generate(db, client, res, allowed) if allowed else None,
                submit_fix(db, client, res) if fix_now else None):
        pending += sub is not None
    return pending


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", default=str(collect.STATE_PATH), help="путь к файлу состояния")
    ap.add_argument("--wait", action="store_true", help="ждать батчи и доводить до конца")
    ap.add_argument("--poll", type=int, default=60, help="секунд между проверками при --wait")
    ap.add_argument("--max-wait", type=int, default=120, help="минут ждать при --wait, потом выйти")
    ap.add_argument("--limit", type=int, default=MAX_GENERATE_PER_RUN, help="генераций за запуск")
    ap.add_argument("--generate", action="store_true", help="отправлять батч генерации")
    ap.add_argument("--fix", action="store_true", help="отправлять батч fix (не чаще раза в сутки)")
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
    monthly_budget()   # кривое значение переменной — ошибка сразу, до вызовов API
    client = llm.make_client()
    res = Resources(site_repo())
    started = now_iso()
    deadline = time.monotonic() + args.max_wait * 60
    while True:
        pending = run_once(db, client, res, args.limit, run_started=started,
                           generate=args.generate, fix=args.fix)
        if not args.wait or not pending or time.monotonic() > deadline:
            break
        time.sleep(args.poll)
    status(db)
    if not args.generate and waiting_by_day(db):
        announce("Генерация в этом прогоне не отправляется: два раза в сутки по расписанию"
                 " (23:17 и 11:17 UTC) или вручную — Run workflow с видом generate")
    # В лог это уже напечатал status, здесь — только в summary прогона
    announce(waiting_line(db), echo=False)
    announce(budget_line(budget_state(db, datetime.now(timezone.utc))), echo=False)


if __name__ == "__main__":
    main()
