"""Разбор страницы статьи (collect.parse_article): текст берётся из того
<article> или <main>, где его больше всего. Без сети."""

import collect

TEXT = ("This is a real paragraph of the blog post with enough words to count as text. " * 2).strip()

# Каркас страницы блога Hugging Face на 08.10.2026: карточки других постов —
# <article>, пустые или с абзацем-описанием; сам пост — в <main> без article
HF_PAGE = f"""<html><head><meta property="og:title" content="Post"></head><body>
<main>
  <div class="blog-content"><p>{TEXT}</p><p>{TEXT}</p><p>Short</p></div>
  <article><a href="/blog/x">Other post</a></article>
  <article><p>Ten camera, drawing and text demos of a small model, with links.</p></article>
  <article></article>
</main></body></html>""".encode()

# Anthropic: пост — в <article>, навигация — короткие абзацы без точки
ANTHROPIC_PAGE = f"""<html><body><nav><p>Products Research Company News Careers</p></nav>
<article><p>Announcements Sep 23, 2026</p><p>{TEXT}</p></article></body></html>""".encode()


def test_post_text_wins_over_related_post_cards():
    _, body, _ = collect.parse_article(HF_PAGE)
    assert body == f"{TEXT}\n\n{TEXT}"


def test_article_with_the_post_is_still_taken():
    _, body, _ = collect.parse_article(ANTHROPIC_PAGE)
    assert body == TEXT
