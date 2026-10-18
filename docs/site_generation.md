# Static site source

Edit the Markdown pages in `site_src/`, then run:

```bash
pixi run site-build
```

The generator (`scripts/build_site.py`) writes one HTML file per page into `site/`. The Markdown renderer is intentionally small; it supports the components already used by the current pages, plus normal headings, paragraphs, links, inline code, fenced code blocks, lists, and tables.

Common acronyms are defined once in `site_src/abbr.toml`. The generator adds matching abbreviation markup and fast site tooltips to generated HTML automatically, so Markdown pages should use plain acronym text rather than embedding abbreviation tags by hand.

The site Markdown files use YAML-compatible front matter. Run `pixi run lint` before committing; it validates Python, site front matter, and `site_src/abbr.toml`, and the same site-source check runs from pre-commit and CI.

## Hand-maintained files in `site/`

The generator only writes `site/*.html`. These files are maintained by hand and must stay committed:

- `site/assets/site.css`: the stylesheet every page links to.
- `site/assets/cli-screenshot.webp`: the home page screenshot.
- `site/assets/social-preview.webp`: the Open Graph/Twitter preview image.
- `site/.nojekyll`: tells GitHub Pages to serve the files as-is without Jekyll processing.

## Deployment

`.github/workflows/pages.yml` runs `scripts/build_site.py` on every push to `main` that touches `site/`, `site_src/`, or the generator, then publishes `site/`. The deployed HTML is always rebuilt from `site_src/`, so committed `site/*.html` only needs to be current for local previews and reviews. Run `pixi run site-build` after editing `site_src/` to keep it in sync.
