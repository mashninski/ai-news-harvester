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
  - если у записи есть поле match (регулярное выражение) — берётся оно как есть
    (только ў/э в нём приводятся к у/е, как и текст). Нужно там, где усечённая
    основа ловит чужое слово: «дадзены» (данный) против «дадзеныя» (data);
  - фраза «глагол в инфинитиве + слово» («прыняць удзел») ищется и с одним-двумя
    словами между, и в обратном порядке: «удзел у ім таксама прынялі» — так
    её пропустил первый прогон на корпусе.

Кроме словаря — механика, которой в словаре не место (прогон на корпусе
24.09.2026, разбор правок судьи в журнале сайта):
  - normalize() исправляет сама, без модели: латинская буква-двойник внутри
    кириллического слова («лімiт»), «2$» → «2 $», десятичная точка перед
    % и знаком валюты («42.92%» → «42,92%»). Версии моделей («GPT-5.6», «Opus
    5.5») не трогает: там после числа нет % и валюты;
  - встроенные правила линтера (BUILTIN) помечают для fix-прохода то, что
    кодом не исправить: буквы и/щ/ъ — их нет в беларуском алфавите, и
    оставшуюся смесь латиницы с кириллицей в одном слове.

Ложные срабатывания ожидаемы и не страшны: помеченное предложение уходит
на fix-проход, и модель может вернуть его без изменений.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import tarask

TERM_MARK_RE = re.compile(r"\{\{term:[^|}]*\|([^}]*)\}\}")
SENT_RE = re.compile(r"(?:(?<=[.!?…])|(?<=[.!?…][»\"”)]))\s+(?=[«\"„(A-ZА-ЯЁІЎ0-9{])")
# Окончания беларуских слов — для поиска фразы в склонённом виде. Список
# закрытый: лучше пропустить редкую форму, чем поймать чужое слово. Ищется по
# тексту после _fold, где «ў» уже «у», поэтому и окончания приводятся так же:
# до 25.09.2026 «-аў», «-яў», «-ўся» не совпадали ни с чем, и «рэзультатаў»
# линтер пропускал
ENDINGS = sorted(set("""
а я у ю ы і е о ом ам ем ым ім ой ай ей ую юю ая яя ое ае ыя ія ых іх ымі імі амі
ямі ах ях аў яў оў ага яга ога аму яму ому ы ць ці ла лі ло л ў ўся ся лася ліся
лося цца ецца аецца уецца юцца уцца ацца ыцца іцца ае яе е юць уць аюць яюць іць
ыць ем ім яць аць уе ее ён
""".replace("ў", "у").split()), key=len, reverse=True)
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


def _word(w: str) -> str:
    w = w.strip(",")
    return re.escape(w) if len(w) <= 3 else re.escape(_stem(w)) + ENDING_RE


def _pattern(phrase: str) -> str:
    words = _fold(phrase).split()
    parts = [_word(w) for w in words]
    rx = r"(?<!\w)" + r",?\s+".join(parts) + r"(?!\w)"
    if len(words) == 2 and words[0].endswith(("ць", "цца")):
        # Глагол + дополнение переставляются и разрываются: «прыняла актыўны
        # ўдзел», «удзел у ім таксама прынялі»
        v, n = parts
        # Между ними — только слова, не знаки: через тире и запятую фраза не тянется
        gap = r"(?:\s+[^\W\d_]+)"
        rx += (rf"|(?<!\w){v}{gap}{{0,2}}\s+{n}(?!\w)"
               rf"|(?<!\w){n}{gap}{{0,3}}\s+{v}(?!\w)")
    return rx


# ---------- механика ----------

# Латинские буквы, которые на вид не отличить от кириллических
LOOKALIKE = str.maketrans("aceiopxyACEHIKMOPTXY", "асеіорхуАСЕНІКМОРТХУ")
WORD_RE = re.compile(r"[^\W\d_]+")
CYR, LAT = re.compile(r"[а-яёіўэА-ЯЁІЎЭ]"), re.compile(r"[A-Za-z]")
MONEY_SPACE_RE = re.compile(r"(\d)([$€£])")
# Служебный комментарий в тексте: стайлгайд до 25.09.2026 велел помечать им
# место для guard-маркера, и модель так и писала — «<!-- guard -->» доехал до
# карточки (прогон на корпусе 25.09.2026)
COMMENT_RE = re.compile(r"\s*<!--.*?-->\s*")
# Прямые кавычки парой: «"max"» → «max». Только пара внутри одного предложения
# и без кавычек внутри, иначе не угадать, где открывающая
STRAIGHT_QUOTES_RE = re.compile(r'(?<![\w"])"([^"\n]+?)"(?![\w"])')
# Текст должен заканчиваться концом предложения. Без него — обрыв: на корпусе
# 25.09.2026 модель поставила прямую кавычку перед цитатой, JSON-схема приняла
# её за конец поля, и пересказ кончился на «працуе як»
TERMINAL_RE = re.compile(r"[.!?…][»\"”)]*\s*$")


def truncated(text: str) -> bool:
    """Поле кончается не концом предложения — модель оборвала текст."""
    return bool(text.strip()) and not TERMINAL_RE.search(text.strip())
DECIMAL_RE = re.compile(r"(?<![\w.\-])(\d+)\.(\d+)(?=\s?[%$€£])")


def normalize(text: str) -> tuple[str, list[str]]:
    """Механические поправки, которые не нужно доверять модели. Возвращает
    (текст, что поменялось) — второе уходит в заметки карточки."""
    changes = []

    def mixed(m):
        w = m.group()
        if CYR.search(w) and LAT.search(w):
            fixed = w.translate(LOOKALIKE)
            if not LAT.search(fixed):
                changes.append(f"лацінская літара ў слове: «{w}» → «{fixed}»")
                return fixed
        return w

    text = WORD_RE.sub(mixed, text)
    new = COMMENT_RE.sub(lambda m: " " if m.start() and m.end() < len(text) else "", text)
    if new != text:
        changes.append("выдалены службовы каментар <!-- -->")
        text = new
    for rx, rep, what in ((STRAIGHT_QUOTES_RE, r"«\1»", "простыя двукоссі → «ёлачкі»"),
                          (MONEY_SPACE_RE, r"\1 \2", "знак валюты праз прабел"),
                          (DECIMAL_RE, r"\1,\2", "дзесятковая коска")):
        new = rx.sub(rep, text)
        if new != text:
            changes.append(what)
            text = new
    return text, changes


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


# Встроенные правила: не лексика словаря, а алфавит. Ищутся по тексту после
# _fold (нижний регистр, ў → у, э → е)
BUILTIN = [
    Rule("и / щ / ъ", ["і", "шч", "ʼ (апостраф)"], "альфабэт",
         "літары рускага альфабэту ўнутры беларускага слова: «транскриптазы» → «транскрыптазы»",
         re.compile(r"[^\W\d_]*[ищъ][^\W\d_]*")),
    Rule("лацінка ўнутры кірылічнага слова", ["адзін альфабэт на слова"], "альфабэт",
         "змешаныя альфабэты ў адным слове",
         re.compile(r"[^\W\d_]*(?:[а-яёіу][a-z]|[a-z][а-яёіу])[^\W\d_]*")),
]


class Linter:
    def __init__(self, rules: list[Rule]):
        self.rules = rules + BUILTIN

    @classmethod
    def load(cls, path: Path, convert=tarask.run_converter) -> "Linter":
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
        converted = convert([e["avoid"] for e in entries])
        rules = []
        for e, conv in zip(entries, converted):
            if e.get("match"):
                rx = e["match"].replace("ў", "у").replace("э", "е")   # как _fold у текста
            else:
                rx = "|".join(sorted({_pattern(e["avoid"]), _pattern(conv)}))
            rules.append(Rule(e["avoid"], e.get("prefer", []), e.get("category", ""),
                              e.get("note", ""), re.compile(rx, re.IGNORECASE)))
        return cls(rules)

    def check(self, sentences: list[str]) -> list[Hit]:
        hits = []
        for i, s in enumerate(sentences):
            # Маркеры терминов — к одному слову: слаг латиницей рядом с
            # кириллицей правило алфавита приняло бы за смесь
            plain = TERM_MARK_RE.sub(r"\1", tarask.strip_names(s))
            folded = _fold(plain)
            for r in self.rules:
                m = r.regex.search(folded)
                if m:
                    hits.append(Hit(i, m.group(), r))
        return hits
