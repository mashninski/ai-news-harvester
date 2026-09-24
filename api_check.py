#!/usr/bin/env python3
"""
Проверка ключа: один маленький синхронный вызов к API и печать ответа.
Критерий этапа 0 плана (ai-news-plan.md, §3): «локальный скрипт делает
один вызов к API и печатает ответ». Стоит долю цента.

    python api_check.py
"""

import sys

from llm import TRIAGE_MODEL, make_client


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    client = make_client()
    msg = client.messages.create(
        model=TRIAGE_MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content": "Адкажы адным словам па-беларуску: якая сталіца Беларусі?"}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    print(f"мадэль: {msg.model}")
    print(f"адказ: {text}")
    print(f"токены: уваход {msg.usage.input_tokens}, выхад {msg.usage.output_tokens}")


if __name__ == "__main__":
    main()
