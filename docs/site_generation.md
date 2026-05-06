# Static site source

Edit the Markdown pages in `site_src/`, then run:

```bash
pixi run site-build
```

The generator writes the deployable HTML files into `site/`. The Markdown renderer is intentionally small; it supports the components already used by the current pages, plus normal headings, paragraphs, links, inline code, fenced code blocks, lists, and tables.

Common acronyms are defined once in `site_src/abbr.toml`. The generator adds matching abbreviation markup and fast site tooltips to generated HTML automatically, so Markdown pages should use plain acronym text rather than embedding abbreviation tags by hand.
