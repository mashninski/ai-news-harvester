"""
Линтер лексики: русизмы и кальки из ai-news-anti-calques.json (репозиторий сайта).
Обычный код, не модель (спека, §3, п. 6). Работает по тексту ПОСЛЕ конвертера.

Как ищется запись словаря. Поле avoid — фраза в начальной форме («прыняць
удзел»), а в тексте она склонена и уже в тарашкевіцы («прыняла ўдзел»). Поэтому:
  - avoid прогоняется через тот же конвертер, ищутся обе формы — до и после;
  - у слова отрезается окончание из закрытого списка ENDINGS и разрешается
    любое окончание из того же списка («прыняць» → «прыня(ла|лі|ць|…)»).
    Не «любое продолжение слова»: так «лічыцца» ловило бы «лічба», а «на
    працягу» — «на працяглыя». Слова до трёх букв («у», «з», «на») — целиком;
  - у/ў, е/э, апострофы ' ’ ʼ считаются одним символом;
  - если у записи есть поле match (регулярное выражение) — берётся оно как есть.
    Нужно там, где усечённая основа ловит чужое слово: «дадзены» (данный)
    против «дадзеныя» (data, термин глоссария).

Ложные срабатывания ожидаемы и не страшны: помеченное предложение уходит
на fix-проход, и модель может вернуть его без изменений.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import tarask

SENT_RE = re.compile(r"(?:(?<=[.!?…])|(?<=[.!?…][»\"”)]))\s+(?=[«\"„(A-ZА-ЯЁІЎ0-9{])")
# Окончания беларуских слов — для поиска фразы в склонённом виде. Список
# закрытый: лучше пропустить редкую форму, чем поймать чужое слово
ENDINGS = sorted(set("""
а я у ю ы і е о ом ам ем ым ім ой ай ей ую юю ая яя ое ае ыя ія ых іх ымі імі амі
ямі ах ях аў яў оў ага яга ога аму яму ому ы ць ці ла лі ло л ў ўся ся лася ліся
лося цца ецца аецца уецца юцца уцца ацца ыцца іцца ае яе е юць уць аюць яюць іць
ыць ем ім яць аць уе ее ён
""".split()), key=len, reverse=True)
ENDING_RE = "(?:" + "|".join(ENDINGS) + ")?"


def split_paragraphs(text: str) -> list[list[str]]:
    """Абзацы — через пустую строку, внутри абзаца — предложения по концу
    предложения. Точки внутри чисел и версий (Opus 5.5) не режут: после них
    нет пробела."""
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        para = " ".join(para.split())
        if para:
            out.append([s for s in SENT_RE.split(para) if s])
    return out


def join_paragraphs(paras: list[list[str]]) -> str:
    return "\n\n".join(" ".join(p) for p in paras)


def split_sentences(text: str) -> list[str]:
    return [s for p in split_paragraphs(text) for s in p]


def _fold(s: str) -> str:
    return s.lower().replace("ў", "у").replace("э", "е").replace("’", "'").replace("ʼ", "'")


def _stem(w: str) -> str:
    """Основа слова: у глагола — без -цца/-ць (прыняць → прыня: прыняла, прыняў),
    у остального — без самого короткого окончания (працягу → працяг). Самое
    длинное отрезать нельзя: «лічыцца» → «ліч» ловило бы «лічаць»."""
    for suffix in ("цца", "ць"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    for e in sorted(ENDINGS, key=len):
        if w.endswith(e) and len(w) - len(e) >= 3:
            return w[: -len(e)]
    return w


def _pattern(phrase: str) -> str:
    parts = []
    for w in _fold(phrase).split():
        w = w.strip(",")
        if len(w) <= 3:
            parts.append(re.escape(w))
        else:
            parts.append(re.escape(_stem(w)) + ENDING_RE)
    return r"(?<!\w)" + r",?\s+".join(parts) + r"(?!\w)"


@dataclass
class Rule:
    avoid: str
    prefer: list
    category: str
    note: str
    regex: re.Pattern


@dataclass
class Hit:
    sentence: int   # номер предложения в тексте
    found: str      # что нашлось в тексте
    rule: Rule


class Linter:
    def __init__(self, rules: list[Rule]):
        self.rules = rules

    @classmethod
    def load(cls, path: Path, convert=tarask.run_converter) -> "Linter":
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
        converted = convert([e["avoid"] for e in entries])
        rules = []
        for e, conv in zip(entries, converted):
            if e.get("match"):
                rx = e["match"]
            else:
                rx = "|".join(sorted({_pattern(e["avoid"]), _pattern(conv)}))
            rules.append(Rule(e["avoid"], e.get("prefer", []), e.get("category", ""),
                              e.get("note", ""), re.compile(rx, re.IGNORECASE)))
        return cls(rules)

    def check(self, sentences: list[str]) -> list[Hit]:
        hits = []
        for i, s in enumerate(sentences):
            folded = _fold(tarask.strip_names(s))
            for r in self.rules:
                m = r.regex.search(folded)
                if m:
                    hits.append(Hit(i, m.group(), r))
        return hits
