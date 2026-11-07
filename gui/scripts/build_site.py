#!/usr/bin/env python3
"""Build the static project site from Markdown sources."""

from __future__ import annotations

try:
    from .site_frontmatter import load_page, parse_actions, parse_bool, parse_front_matter, require, unquote_scalar
    from .site_html import escape_attr, escape_text, render_button
    from .site_layout import render_ai_written_banner, render_footer, render_hero, render_nav, render_page
    from .site_markdown import (
        MarkdownRenderer,
        article_with_class,
        div_with_class,
        is_raw_html,
        is_table_start,
        parse_attrs,
        parse_heading,
        parse_link_shortcode,
        render_attrs,
        render_container,
        render_inline,
        render_shortcode,
        replace_link,
        split_table_row,
    )
    from .site_model import (
        BASE_URL,
        GITHUB_URL,
        NAV_ITEMS,
        OUTPUT_DIR,
        ROOT,
        SOCIAL_IMAGE_ALT,
        SOCIAL_IMAGE_HEIGHT,
        SOCIAL_IMAGE_URL,
        SOCIAL_IMAGE_WIDTH,
        SOURCE_DIR,
        Page,
    )
except ImportError:  # pragma: no cover - script execution path
    from site_frontmatter import load_page, parse_actions, parse_bool, parse_front_matter, require, unquote_scalar
    from site_html import escape_attr, escape_text, render_button
    from site_layout import render_ai_written_banner, render_footer, render_hero, render_nav, render_page
    from site_markdown import (
        MarkdownRenderer,
        article_with_class,
        div_with_class,
        is_raw_html,
        is_table_start,
        parse_attrs,
        parse_heading,
        parse_link_shortcode,
        render_attrs,
        render_container,
        render_inline,
        render_shortcode,
        replace_link,
        split_table_row,
    )
    from site_model import (
        BASE_URL,
        GITHUB_URL,
        NAV_ITEMS,
        OUTPUT_DIR,
        ROOT,
        SOCIAL_IMAGE_ALT,
        SOCIAL_IMAGE_HEIGHT,
        SOCIAL_IMAGE_URL,
        SOCIAL_IMAGE_WIDTH,
        SOURCE_DIR,
        Page,
    )


def main() -> None:
    if not SOURCE_DIR.is_dir():
        raise SystemExit(f"Missing site source directory: {SOURCE_DIR}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    for source in sorted(SOURCE_DIR.glob("*.md")):
        page = load_page(source)
        (OUTPUT_DIR / page.output_name).write_text(render_page(page), encoding="utf-8")


if __name__ == "__main__":
    main()
