# Static site generation

Edit the Markdown pages in `site_src/`, then run:

```bash
python3 scripts/validate_site_sources.py
python3 scripts/build_site.py
open site/index.html
```

The generator writes deployable HTML files into `site/`. It is intentionally small and dependency-free, matching the static-site technique used by the WGSExtract CLI repository: Markdown sources, simple front matter, a Python generator, committed CSS, and generated static HTML output.

The generated site is focused on the public product story: what GUI for CLI does, how the TypeScript and Tauri app is put together, and how WGSExtract became the first real app for the toolkit.
