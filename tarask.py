"""
Наркамаўка → тарашкевіца: guard-маркеры, вызов конвертера, поиск подозрительных замен.

Сам конвертер — npm-пакет taraskevizer в tools/taraskevize/ (Node), здесь он
вызывается отдельным процессом: один вызов на пачку текстов. Правила
тарашкевіцы сами не переписываем (журнал сайта, 24.09.2026, «путь
тарашкевизации»): любая своя реализация — это то же ненадёжное «угадать
правило», от чего спека (§6) уходит детерминированным конвертером.

Что защищается от конвертера синтаксисом самого пакета (<.текст> проходит
нетронутым):
  - латиница целиком — названия моделей и компаний, аббревиатуры
    (CERT-UA → CERT—UA, этап 1), слаг в {{term:slug|…}};
  - URL и `код`;
  - слова из словаря converter-guards.json (репозиторий сайта): известные
    детерминированные поломки конвертера — имена (Шах → Шаг) и подмена
    смысла (трэніроўкамі → трэнаваньнемі).

Чего словарь ещё не знает, ловится после конвертации и не молча:
  - изменилось имя внутри {{name:…}} — модель помечает так личные имена;
  - слово изменилось сильнее обычной орфографической правки (похожесть
    меньше SUSPICIOUS_RATIO) — так выглядит подмена слова другим.
Оба случая уходят в заметки карточки для ревью, словарь пополняется руками.
"""

import json
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "tools" / "taraskevize" / "convert.mjs"

# Похожесть слова до и после, ниже которой замена считается подозрительной.
# Обычная орфография тарашкевіцы даёт 0.75–0.95 (сістэма → сыстэма 0.86,
# навучанне → навучаньне 0.95, зала → заля 0.75). Подмены смысла из корпуса:
# трэнажорнай → сымулятарнай 0.52, трэніроўкамі → трэнаваньнемі 0.56.
SUSPICIOUS_RATIO = 0.7

# Символы, которые конвертер трактует как свой синтаксис, — прячем на время
# конвертации.  — его внутренний заполнитель, во входе его быть не может.
_LT, _GT = "", ""

LATIN = r"[A-Za-z0-9][A-Za-z0-9.+#&_/'’@:-]*[A-Za-z0-9+#]|[A-Za-z]"
GUARD_RE = re.compile(
    r"https?://[^\s<>]+[^\s<>.,;:!?)»\"]"   # URL без хвостовой пунктуации
    r"|`[^`\n]*`"                            # код
    r"|\{\{term:[^|}]+\|"                    # открывающая часть маркера термина
    r"|\{\{name:"                            # открывающая часть маркера имени
    rf"|(?<![\w\-])(?:{LATIN})(?![\w])"      # латиница
)
NAME_RE = re.compile(r"\{\{name:([^}]*)\}\}")
WORD_RE = re.compile(r"[^\W\d_](?:[\w'’ʼ\-]*\w)?")


def has_latin(s: str) -> bool:
    return re.search(r"[A-Za-z]", s) is not None


class Guards:
    """Словарь converter-guards.json: слова, которые конвертер ломает детерминированно."""

    def __init__(self, entries: list[dict]):
        parts = []
        for e in entries:
            w = re.escape(e["match"])
            # stem — любое слово, начинающееся с match (все падежные формы);
            # word — только это слово целиком. Регистр не важен: «шах» ломается так же
            parts.append(rf"{w}\w*" if e.get("form") == "stem" else rf"{w}(?!\w)")
        self.re = re.compile(rf"(?<!\w)(?:{'|'.join(parts)})", re.IGNORECASE) if parts else None

    @classmethod
    def load(cls, path: Path) -> "Guards":
        return cls(json.loads(path.read_text(encoding="utf-8"))["entries"])

    def spans(self, text: str):
        return [m.span() for m in self.re.finditer(text)] if self.re else []


def protect(text: str, guards: Guards | None = None) -> str:
    """Оборачивает всё, что конвертер трогать не должен, в <.…>."""
    text = text.replace("", "").replace("<", _LT).replace(">", _GT)
    # Чистые числа («2026», «3.5») регулярка тоже ловит — их конвертер не трогает
    spans = [m.span() for m in GUARD_RE.finditer(text)
             if has_latin(m.group()) or m.group()[0] in "`{"]
    if guards:
        spans += guards.spans(text)
    # Пересечения склеиваем, чтобы <.…> не вкладывались друг в друга
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    out, pos = [], 0
    for a, b in merged:
        out += [text[pos:a], "<.", text[a:b], ">"]
        pos = b
    out.append(text[pos:])
    return "".join(out)


def restore(text: str) -> str:
    return text.replace(_LT, "<").replace(_GT, ">")


def run_converter(texts: list[str]) -> list[str]:
    """Пачка текстов через taraskevizer. Guard-маркеры должны быть уже расставлены."""
    if not texts:
        return []
    proc = subprocess.run(["node", str(TOOL)], input=json.dumps(texts, ensure_ascii=False),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"конвертер упал: {proc.stderr.strip()[:500]}")
    out = json.loads(proc.stdout)
    if len(out) != len(texts):
        raise RuntimeError("конвертер вернул не столько текстов, сколько получил")
    return out


def convert(texts: list[str], guards: Guards | None = None) -> list[str]:
    """Наркамаўка → тарашкевіца с guard-маркерами. Маркеры {{name:…}} остаются —
    их снимает strip_names после проверки."""
    return [restore(t) for t in run_converter([protect(t, guards) for t in texts])]


def strip_names(text: str) -> str:
    return NAME_RE.sub(lambda m: m.group(1), text)


def suspicious_changes(before: str, after: str, guards: Guards | None = None) -> list[dict]:
    """Что конвертер поменял подозрительно: имена в {{name:…}} и слова,
    изменившиеся сильнее обычной орфографии. Возвращает [{kind, before, after}]."""
    notes = []
    names_b, names_a = NAME_RE.findall(before), NAME_RE.findall(after)
    if len(names_b) == len(names_a):
        for b, a in zip(names_b, names_a):
            if b != a:
                notes.append({"kind": "імя", "before": b, "after": a})

    wb = WORD_RE.findall(strip_names(before))
    wa = WORD_RE.findall(strip_names(after))
    sm = SequenceMatcher(a=[w.lower() for w in wb], b=[w.lower() for w in wa], autojunk=False)
    named = {w.lower() for n in names_b for w in WORD_RE.findall(n)}
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or i2 - i1 != j2 - j1:
            continue
        for b, a in zip(wb[i1:i2], wa[j1:j2]):
            if b.lower() in named:
                continue  # уже сказано выше как об имени
            if len(b) <= 2:
                continue  # з → зь, не → ня: обычная орфография, на коротком слове похожесть мала сама по себе
            if SequenceMatcher(a=b.lower(), b=a.lower()).ratio() < SUSPICIOUS_RATIO:
                notes.append({"kind": "слова", "before": b, "after": a})
    return notes
