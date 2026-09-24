"""
Общее для вызовов Anthropic API: клиент, модели, Batch API.

Ключ берётся ТОЛЬКО из переменной окружения AI_NEWS_ANTHROPIC_KEY и передаётся
в конструктор явно. Имя ANTHROPIC_API_KEY в облачных сессиях Claude Code занято
самой платформой — полагаться на то, что SDK подхватит его сам, нельзя: вызовы
ушли бы не с того ключа и не на тот счёт.
"""

import os

import anthropic

KEY_ENV = "AI_NEWS_ANTHROPIC_KEY"

TRIAGE_MODEL = "claude-haiku-4-5"   # спека §3 п.2: triage — Haiku
GENERATE_MODEL = "claude-sonnet-5"  # спека §3: генерация и fix — Sonnet 5, не дешевле


class NoKey(RuntimeError):
    pass


def make_client() -> anthropic.Anthropic:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise NoKey(f"не задана переменная окружения {KEY_ENV} — ключ Anthropic API")
    return anthropic.Anthropic(api_key=key, max_retries=3)
