from __future__ import annotations

try:
    from .site_html import escape_attr, escape_text, render_button
    from .site_markdown import MarkdownRenderer, render_inline
    from .site_model import (
        BASE_URL,
        GITHUB_URL,
        NAV_ITEMS,
        SOCIAL_IMAGE_ALT,
        SOCIAL_IMAGE_HEIGHT,
        SOCIAL_IMAGE_URL,
        SOCIAL_IMAGE_WIDTH,
        Page,
    )
except ImportError:  # pragma: no cover - script execution path
    from site_html import escape_attr, escape_text, render_button
    from site_markdown import MarkdownRenderer, render_inline
    from site_model import (
        BASE_URL,
        GITHUB_URL,
        NAV_ITEMS,
        SOCIAL_IMAGE_ALT,
        SOCIAL_IMAGE_HEIGHT,
        SOCIAL_IMAGE_URL,
        SOCIAL_IMAGE_WIDTH,
        Page,
    )


def render_page(page: Page) -> str:
    canonical = (
        f"{BASE_URL}/"
        if page.output_name == "index.html"
        else f"{BASE_URL}/{page.output_name}"
    )
    body = MarkdownRenderer(page.body).render()
    hero = render_hero(page) if page.auto_hero else ""
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "  <head>",
            '    <meta charset="utf-8" />',
            '    <meta name="viewport" content="width=device-width, initial-scale=1" />',
            f'    <meta name="description" content="{escape_attr(page.description)}" />',
            f"    <title>{escape_text(page.title)}</title>",
            '    <meta property="og:site_name" content="GUI for CLI" />',
            '    <meta property="og:type" content="website" />',
            f'    <meta property="og:title" content="{escape_attr(page.title)}" />',
            f'    <meta property="og:description" content="{escape_attr(page.description)}" />',
            f'    <meta property="og:url" content="{escape_attr(canonical)}" />',
            f'    <meta property="og:image" content="{escape_attr(SOCIAL_IMAGE_URL)}" />',
            f'    <meta property="og:image:secure_url" content="{escape_attr(SOCIAL_IMAGE_URL)}" />',
            '    <meta property="og:image:type" content="image/webp" />',
            f'    <meta property="og:image:width" content="{SOCIAL_IMAGE_WIDTH}" />',
            f'    <meta property="og:image:height" content="{SOCIAL_IMAGE_HEIGHT}" />',
            f'    <meta property="og:image:alt" content="{escape_attr(SOCIAL_IMAGE_ALT)}" />',
            '    <meta name="twitter:card" content="summary_large_image" />',
            f'    <meta name="twitter:image" content="{escape_attr(SOCIAL_IMAGE_URL)}" />',
            f'    <meta name="twitter:image:alt" content="{escape_attr(SOCIAL_IMAGE_ALT)}" />',
            f'    <link rel="canonical" href="{escape_attr(canonical)}" />',
            f'    <link rel="image_src" href="{escape_attr(SOCIAL_IMAGE_URL)}" />',
            "    <link rel=\"icon\" href='data:image/svg+xml,<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 100\"><text y=\".82em\" font-size=\"84\">%E2%8C%98</text></svg>' />",
            '    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" />',
            '    <link rel="stylesheet" href="assets/site.css" />',
            "  </head>",
            "  <body>",
            render_ai_written_banner(),
            render_nav(page.output_name),
            "    <main>",
            hero,
            body,
            "    </main>",
            render_footer(page),
            "  </body>",
            "</html>",
            "",
        ]
    )


def render_ai_written_banner() -> str:
    issues_url = f"{escape_attr(GITHUB_URL)}/issues/new"
    pulls_url = f"{escape_attr(GITHUB_URL)}/pulls"
    tooltip_html = (
        "I'm tired and bad at writing things, help appreciated - "
        + f'<a href="{issues_url}" target="_blank" rel="noopener noreferrer">file an issue</a>'
        + " or "
        + f'<a href="{pulls_url}" target="_blank" rel="noopener noreferrer">PR on GitHub</a>.'
    )
    return "\n".join(
        [
            '    <details class="ai-written-banner">',
            '      <summary class="ai-written-banner-text">AI written</summary>',
            f'      <div class="ai-written-tooltip" id="ai-written-tooltip">{tooltip_html}</div>',
            "    </details>",
        ]
    )


def render_nav(active_output: str) -> str:
    links = []
    for href, label in NAV_ITEMS:
        active = ' class="active"' if href == active_output else ""
        links.append(f'<a{active} href="{href}">{escape_text(label)}</a>')
    links.append(
        f'<a href="{GITHUB_URL}" target="_blank" rel="noopener noreferrer">GitHub</a>'
    )
    return "\n".join(
        [
            '    <nav class="nav" aria-label="Primary navigation">',
            '      <div class="wrap nav-inner">',
            '        <a class="brand" href="index.html"><span class="brand-mark" aria-hidden="true"></span><span>GUI for CLI</span></a>',
            f'        <div class="nav-links">{"".join(links)}</div>',
            "      </div>",
            "    </nav>",
        ]
    )


def render_hero(page: Page) -> str:
    actions = "".join(render_button(*action) for action in page.actions)
    action_block = f'\n          <div class="actions">{actions}</div>' if actions else ""
    return "\n".join(
        [
            '      <header class="page-hero">',
            '        <div class="wrap">',
            f'          <p class="eyebrow"><span class="pulse" aria-hidden="true"></span> {escape_text(page.eyebrow)}</p>',
            f"          <h1>{render_inline(page.heading)}</h1>",
            f'          <p class="lede">{render_inline(page.lede)}</p>{action_block}',
            "        </div>",
            "      </header>",
        ]
    )


def render_footer(page: Page) -> str:
    return "\n".join(
        [
            '    <footer class="footer">',
            '      <div class="wrap">',
            f"        <div><strong>{escape_text(page.footer_title)}</strong><p>{render_inline(page.footer_text)}</p></div>",
            f'        <p><a href="{GITHUB_URL}">Source on GitHub</a></p>',
            "      </div>",
            "    </footer>",
        ]
    )
