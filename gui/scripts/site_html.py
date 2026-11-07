from __future__ import annotations

import html


def render_button(label: str, href: str, css_class: str = "") -> str:
    classes = "btn"
    if css_class:
        classes = f"{classes} {css_class}"
    return f'<a class="{escape_attr(classes)}" href="{escape_attr(href)}">{escape_text(label)}</a>'


def escape_text(value: str) -> str:
    return html.escape(value, quote=False)


def escape_attr(value: str) -> str:
    return html.escape(value, quote=True)
