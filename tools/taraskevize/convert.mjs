// Наркамаўка → тарашкевіца. Вызывается из tarask.py отдельным процессом.
//
// Вход (stdin): JSON-массив строк. Выход (stdout): JSON-массив той же длины.
// Одна строка на вход — один абзац или один текст целиком, как решит вызывающий.
//
// Настройки повторяют веб-версию gooseob.github.io/taraskevizatar, через которую
// делался эталонный корпус: ґ выключено (в корпусе ни одной ґ), й вместо і
// не ставится, варианты не подставляются (variations: "no"). Последнее важно:
// "first" берёт первую АЛЬТЕРНАТИВУ, а не основную форму, и даёт «у вадным»,
// «дадзеных» → «зьвестак», «Гродна» → «Горадня». Веб-версия по умолчанию
// показывает основную форму — её и берём, с ней сходится корпус.
//
// Guard-маркеры — синтаксис самого пакета: <.текст> проходит нетронутым.
// Расставляет их tarask.py, здесь только конвертация.
import { pipelines, TaraskConfig } from "taraskevizer";

const cfg = new TaraskConfig({ g: false, j: "never", variations: "no" });

let raw = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) raw += chunk;

const texts = JSON.parse(raw);
if (!Array.isArray(texts)) throw new Error("на вход ожидается JSON-массив строк");
process.stdout.write(JSON.stringify(texts.map((t) => pipelines.tarask(t, cfg))));
