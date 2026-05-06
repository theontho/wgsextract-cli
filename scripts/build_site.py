#!/usr/bin/env python3
"""Build the static documentation site from Markdown source files."""

from __future__ import annotations

import html
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "site_src"
OUTPUT_DIR = ROOT / "site"
BASE_URL = "https://theontho.github.io/wgsextract-cli"
GITHUB_URL = "https://github.com/theontho/wgsextract-cli"
ABBR_FILE = SOURCE_DIR / "abbr.toml"

NAV_ITEMS = [
    ("index.html", "Home"),
    ("install.html", "Install"),
    ("cli.html", "CLI"),
    ("gui.html", "GUI"),
    ("workflows.html", "Workflows"),
    ("wgs-guide.html", "WGS guide"),
    ("reference.html", "Reference"),
]


def load_site_config() -> tuple[dict[str, str], dict[str, str]]:
    if not ABBR_FILE.is_file():
        return {}, {}
    data = tomllib.loads(ABBR_FILE.read_text(encoding="utf-8"))

    def read_string_table(name: str) -> dict[str, str]:
        table = data.get(name, {})
        if not isinstance(table, dict):
            raise ValueError(f"{ABBR_FILE} section [{name}] must be a table")
        return {
            str(label): str(value)
            for label, value in table.items()
            if str(label).strip() and str(value).strip()
        }

    return read_string_table("abbreviations"), read_string_table("links")


def build_label_pattern(labels: set[str]) -> re.Pattern[str] | None:
    if not labels:
        return None
    return re.compile(
        r"(?<![\w-])("
        + "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
        + r")(?![\w-])"
    )


ABBREVIATIONS, LINKS = load_site_config()
AUTOMARK_PATTERN = build_label_pattern(set(ABBREVIATIONS) | set(LINKS))


@dataclass(frozen=True)
class Page:
    source: Path
    output_name: str
    title: str
    description: str
    eyebrow: str
    heading: str
    lede: str
    footer_title: str
    footer_text: str
    body: str
    toc: tuple[tuple[str, str], ...] = ()
    actions: tuple[tuple[str, str, str], ...] = ()
    footer_link_text: str = ""
    footer_link_href: str = ""
    footer_extra: str = ""
    include_auto_hero: bool = True


def main() -> None:
    if not SOURCE_DIR.is_dir():
        raise SystemExit(f"Missing site source directory: {SOURCE_DIR}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for source in sorted(SOURCE_DIR.glob("*.md")):
        page = load_page(source)
        (OUTPUT_DIR / page.output_name).write_text(render_page(page), encoding="utf-8")


def load_page(source: Path) -> Page:
    metadata, body = parse_front_matter(source.read_text(encoding="utf-8"))
    output_name = require(metadata, "output")
    include_auto_hero = metadata.get("auto_hero", "true").lower() != "false"
    return Page(
        source=source,
        output_name=output_name,
        title=require(metadata, "title"),
        description=require(metadata, "description"),
        eyebrow=require(metadata, "eyebrow"),
        heading=require(metadata, "heading"),
        lede=require(metadata, "lede"),
        footer_title=require(metadata, "footer_title"),
        footer_text=require(metadata, "footer_text"),
        footer_link_text=metadata.get("footer_link_text", ""),
        footer_link_href=metadata.get("footer_link_href", ""),
        footer_extra=metadata.get("footer_extra", ""),
        toc=parse_pairs(metadata.get("toc", "")),
        actions=parse_actions(metadata.get("actions", "")),
        body=body,
        include_auto_hero=include_auto_hero,
    )


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Markdown source must start with front matter")

    metadata: dict[str, str] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"Invalid front matter line: {line}")
        metadata[key.strip()] = value.strip()
    else:
        raise ValueError("Unterminated front matter")

    return metadata, "\n".join(lines[index:]).strip()


def require(metadata: dict[str, str], key: str) -> str:
    value = metadata.get(key, "")
    if not value:
        raise ValueError(f"Missing required front matter key: {key}")
    return value


def parse_pairs(value: str) -> tuple[tuple[str, str], ...]:
    if not value:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in value.split(";"):
        label, separator, target = item.strip().partition("|")
        if not separator:
            raise ValueError(f"Expected label|target pair: {item}")
        pairs.append((label.strip(), target.strip()))
    return tuple(pairs)


def parse_actions(value: str) -> tuple[tuple[str, str, str], ...]:
    if not value:
        return ()
    actions: list[tuple[str, str, str]] = []
    for item in value.split(";"):
        parts = [part.strip() for part in item.split("|")]
        if len(parts) not in {2, 3}:
            raise ValueError(f"Expected label|href or label|href|class action: {item}")
        label, href = parts[:2]
        css_class = parts[2] if len(parts) == 3 else ""
        actions.append((label, href, css_class))
    return tuple(actions)


def render_page(page: Page) -> str:
    canonical = (
        f"{BASE_URL}/"
        if page.output_name == "index.html"
        else f"{BASE_URL}/{page.output_name}"
    )
    hero = render_page_hero(page) if page.include_auto_hero else ""
    body = render_markdown(page.body)
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "  <head>",
            '    <meta charset="utf-8" />',
            '    <meta name="viewport" content="width=device-width, initial-scale=1" />',
            f'    <meta name="description" content="{escape_attr(page.description)}" />',
            f"    <title>{escape_text(page.title)}</title>",
            '    <meta property="og:site_name" content="WGS Extract CLI" />',
            '    <meta property="og:type" content="website" />',
            f'    <meta property="og:title" content="{escape_attr(page.title)}" />',
            f'    <meta property="og:description" content="{escape_attr(page.description)}" />',
            f'    <meta property="og:url" content="{escape_attr(canonical)}" />',
            f'    <meta property="og:image" content="{BASE_URL}/assets/social-preview.png" />',
            '    <meta property="og:image:type" content="image/png" />',
            '    <meta property="og:image:width" content="1200" />',
            '    <meta property="og:image:height" content="630" />',
            '    <meta property="og:image:alt" content="WGS Extract CLI local-first whole genome toolkit" />',
            '    <meta name="twitter:card" content="summary_large_image" />',
            f'    <meta name="twitter:title" content="{escape_attr(page.title)}" />',
            f'    <meta name="twitter:description" content="{escape_attr(page.description)}" />',
            f'    <meta name="twitter:image" content="{BASE_URL}/assets/social-preview.png" />',
            f'    <link rel="canonical" href="{escape_attr(canonical)}" />',
            '    <link rel="icon" href=\'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".82em" font-size="84">🧬</text></svg>\' />',
            '    <link rel="stylesheet" href="assets/site.css" />',
            "  </head>",
            "  <body>",
            render_nav(page.output_name),
            "",
            "    <main>",
            hero,
            body,
            "    </main>",
            "",
            render_footer(page),
            render_site_script(),
            "  </body>",
            "</html>",
            "",
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
            '        <a class="brand" href="index.html"><span class="brand-mark" aria-hidden="true"></span><span>WGS Extract CLI</span></a>',
            f'        <div class="nav-links">{"".join(links)}</div>',
            "      </div>",
            "    </nav>",
        ]
    )


def render_page_hero(page: Page) -> str:
    extra = ""
    if page.toc:
        links = "".join(
            f'<a href="#{escape_attr(target)}">{escape_text(label)}</a>'
            for label, target in page.toc
        )
        extra = f'<div class="toc">{links}</div>'
    elif page.actions:
        links = "".join(
            render_button(label, href, css_class)
            for label, href, css_class in page.actions
        )
        extra = f'<div class="actions">{links}</div>'

    extra_line = f"\n          {extra}" if extra else ""
    return "\n".join(
        [
            '      <header class="page-hero">',
            '        <div class="wrap">',
            f'          <p class="eyebrow"><span class="pulse" aria-hidden="true"></span> {escape_text(page.eyebrow)}</p>',
            f"          <h1>{render_inline(page.heading)}</h1>",
            f'          <p class="lede">{render_inline(page.lede)}</p>{extra_line}',
            "        </div>",
            "      </header>",
            "",
        ]
    )


def render_button(label: str, href: str, css_class: str = "") -> str:
    classes = "btn"
    if css_class:
        classes = f"{classes} {css_class}"
    return (
        f'<a class="{escape_attr(classes)}" href="{escape_attr(href)}"'
        f"{render_tooltip_attrs(label)}>{escape_text(label)}</a>"
    )


def render_footer(page: Page) -> str:
    link = ""
    if page.footer_extra:
        link = f"<p>{render_inline(page.footer_extra)}</p>"
    elif page.footer_link_text and page.footer_link_href:
        link = f'<p><a href="{escape_attr(page.footer_link_href)}">{escape_text(page.footer_link_text)}</a></p>'
    return "\n".join(
        [
            '    <footer class="footer">',
            '      <div class="wrap">',
            f"        <div><strong>{escape_text(page.footer_title)}</strong><p>{render_inline(page.footer_text)}</p></div>",
            f"        {link}",
            "      </div>",
            "    </footer>",
        ]
    )


def render_site_script() -> str:
    return "\n".join(
        [
            "    <script>",
            "      (() => {",
            "        const copyText = async (text) => {",
            "          if (navigator.clipboard && window.isSecureContext) {",
            "            await navigator.clipboard.writeText(text);",
            "            return;",
            "          }",
            '          const textarea = document.createElement("textarea");',
            "          textarea.value = text;",
            '          textarea.setAttribute("readonly", "");',
            '          textarea.style.position = "fixed";',
            '          textarea.style.left = "-9999px";',
            "          document.body.appendChild(textarea);",
            "          textarea.select();",
            '          document.execCommand("copy");',
            "          textarea.remove();",
            "        };",
            "",
            '        document.addEventListener("click", async (event) => {',
            '          const button = event.target.closest(".copy-button");',
            "          if (!button) return;",
            '          const panel = button.closest(".code-panel");',
            '          const code = panel?.querySelector("pre code");',
            "          if (!code) return;",
            "          const previous = button.textContent;",
            "          try {",
            "            await copyText(code.innerText);",
            '            button.textContent = "Copied";',
            "          } catch {",
            '            button.textContent = "Failed";',
            "          }",
            "          window.setTimeout(() => {",
            "            button.textContent = previous;",
            "          }, 1400);",
            "        });",
            "",
            "        let tooltip;",
            "        let activeTooltipTarget;",
            "        let tooltipTimer;",
            "",
            "        const ensureTooltip = () => {",
            "          if (!tooltip) {",
            '            tooltip = document.createElement("div");',
            '            tooltip.className = "site-tooltip";',
            "            tooltip.hidden = true;",
            '            tooltip.setAttribute("role", "tooltip");',
            "            document.body.appendChild(tooltip);",
            "          }",
            "          return tooltip;",
            "        };",
            "",
            "        const positionTooltip = () => {",
            "          if (!tooltip || !activeTooltipTarget) return;",
            "          const rect = activeTooltipTarget.getBoundingClientRect();",
            "          const tipRect = tooltip.getBoundingClientRect();",
            "          const margin = 12;",
            "          let left = rect.left + rect.width / 2 - tipRect.width / 2;",
            "          left = Math.max(margin, Math.min(left, window.innerWidth - tipRect.width - margin));",
            "          let top = rect.top - tipRect.height - 10;",
            "          if (top < margin) top = rect.bottom + 10;",
            "          tooltip.style.left = `${Math.round(left + window.scrollX)}px`;",
            "          tooltip.style.top = `${Math.round(top + window.scrollY)}px`;",
            "        };",
            "",
            "        const showTooltip = (target) => {",
            "          const text = target.dataset.tooltip;",
            "          if (!text) return;",
            "          activeTooltipTarget = target;",
            "          const tip = ensureTooltip();",
            "          tip.textContent = text;",
            "          tip.hidden = false;",
            '          tip.classList.remove("visible");',
            "          window.requestAnimationFrame(() => {",
            "            positionTooltip();",
            '            tip.classList.add("visible");',
            "          });",
            "        };",
            "",
            "        const scheduleTooltip = (target) => {",
            "          window.clearTimeout(tooltipTimer);",
            "          tooltipTimer = window.setTimeout(() => showTooltip(target), 120);",
            "        };",
            "",
            "        const hideTooltip = () => {",
            "          window.clearTimeout(tooltipTimer);",
            "          activeTooltipTarget = undefined;",
            "          if (!tooltip) return;",
            '          tooltip.classList.remove("visible");',
            "          tooltip.hidden = true;",
            "        };",
            "",
            '        document.addEventListener("mouseover", (event) => {',
            "          const target = event.target.closest?.('[data-tooltip]');",
            "          if (!target || target === activeTooltipTarget) return;",
            "          scheduleTooltip(target);",
            "        });",
            "",
            '        document.addEventListener("mouseout", (event) => {',
            "          const target = event.target.closest?.('[data-tooltip]');",
            "          if (!target || target.contains(event.relatedTarget)) return;",
            "          hideTooltip();",
            "        });",
            "",
            '        document.addEventListener("focusin", (event) => {',
            "          const target = event.target.closest?.('[data-tooltip]');",
            "          if (target) scheduleTooltip(target);",
            "        });",
            '        document.addEventListener("focusout", hideTooltip);',
            '        window.addEventListener("scroll", positionTooltip, { passive: true });',
            '        window.addEventListener("resize", positionTooltip);',
            "      })();",
            "    </script>",
        ]
    )


def render_markdown(markdown: str) -> str:
    lines = markdown.splitlines()
    renderer = MarkdownRenderer(lines)
    return renderer.render()


class MarkdownRenderer:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.index = 0
        self.output: list[str] = []
        self.paragraph: list[str] = []
        self.closers: list[str] = []

    def render(self) -> str:
        while self.index < len(self.lines):
            line = self.lines[self.index]
            stripped = line.strip()
            if not stripped:
                self.flush_paragraph()
                self.index += 1
            elif stripped == ":::":
                self.flush_paragraph()
                if not self.closers:
                    raise ValueError("Unexpected container close")
                self.output.append(self.closers.pop())
                self.index += 1
            elif stripped == "::: raw":
                self.flush_paragraph()
                self.output.append(self.read_raw_block())
            elif stripped.startswith(":::"):
                self.flush_paragraph()
                self.open_container(stripped[3:].strip())
                self.index += 1
            elif stripped.startswith("```"):
                self.flush_paragraph()
                self.output.append(self.read_code_block())
            elif heading := parse_heading(stripped):
                self.flush_paragraph()
                level, text = heading
                self.output.append(f"<h{level}>{render_inline(text)}</h{level}>")
                self.index += 1
            elif stripped.startswith("- "):
                self.flush_paragraph()
                self.output.append(self.read_list())
            elif is_table_start(self.lines, self.index):
                self.flush_paragraph()
                self.output.append(self.read_table())
            elif shortcode := render_shortcode(stripped):
                self.flush_paragraph()
                self.output.append(shortcode)
                self.index += 1
            elif is_raw_html(stripped):
                self.flush_paragraph()
                self.output.append(add_abbreviations_to_html(line))
                self.index += 1
            else:
                self.paragraph.append(stripped)
                self.index += 1

        self.flush_paragraph()
        if self.closers:
            raise ValueError("Unclosed containers in Markdown source")
        return "\n".join(self.output)

    def flush_paragraph(self) -> None:
        if self.paragraph:
            text = " ".join(self.paragraph)
            self.output.append(f"<p>{render_inline(text)}</p>")
            self.paragraph = []

    def open_container(self, spec: str) -> None:
        opener, closer = render_container(spec)
        self.output.append(opener)
        self.closers.append(closer)

    def read_code_block(self) -> str:
        language = self.lines[self.index].strip()[3:].strip()
        self.index += 1
        code_lines: list[str] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            self.index += 1
            if line.strip().startswith("```"):
                return (
                    f"<pre><code>{render_code_lines(code_lines, language)}</code></pre>"
                )
            code_lines.append(line)
        raise ValueError("Unterminated code block")

    def read_raw_block(self) -> str:
        self.index += 1
        raw_lines: list[str] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            self.index += 1
            if line.strip() == ":::":
                return add_abbreviations_to_html("\n".join(raw_lines))
            raw_lines.append(line)
        raise ValueError("Unterminated raw block")

    def read_list(self) -> str:
        items: list[str] = []
        while self.index < len(self.lines):
            stripped = self.lines[self.index].strip()
            if not stripped.startswith("- "):
                break
            items.append(f"<li>{render_inline(stripped[2:].strip())}</li>")
            self.index += 1
        return "<ul>\n" + "\n".join(items) + "\n</ul>"

    def read_table(self) -> str:
        header = split_table_row(self.lines[self.index])
        self.index += 2
        rows: list[list[str]] = []
        while self.index < len(self.lines):
            stripped = self.lines[self.index].strip()
            if not stripped.startswith("|"):
                break
            rows.append(split_table_row(stripped))
            self.index += 1

        header_html = "".join(f"<th>{render_inline(cell)}</th>" for cell in header)
        body_rows = [
            "<tr>"
            + "".join(f"<td>{render_inline(cell)}</td>" for cell in row)
            + "</tr>"
            for row in rows
        ]
        return (
            "<table>\n"
            f"<thead><tr>{header_html}</tr></thead>\n"
            "<tbody>\n" + "\n".join(body_rows) + "\n</tbody>\n"
            "</table>"
        )


def parse_heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^(#{1,3})\s+(.+)$", line)
    if not match:
        return None
    return len(match.group(1)), match.group(2)


def is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    current = lines[index].strip()
    separator = lines[index + 1].strip()
    return current.startswith("|") and bool(re.match(r"^\|[\s:-]+\|", separator))


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render_code_lines(code_lines: list[str], language: str) -> str:
    if language.lower() in {"toml", "ini"}:
        return "\n".join(render_config_line(line) for line in code_lines)
    return "\n".join(render_terminal_line(line) for line in code_lines)


def render_terminal_line(line: str) -> str:
    escaped = html.escape(line)
    stripped = line.lstrip()
    if not stripped:
        return ""
    if stripped.startswith("#"):
        return f'<span class="comment">{escaped}</span>'
    return f'<span class="cmd">{escaped}</span>'


def render_config_line(line: str) -> str:
    escaped = html.escape(line)
    stripped = line.lstrip()
    if not stripped:
        return ""
    if stripped.startswith("#"):
        return f'<span class="comment">{escaped}</span>'
    key_match = re.match(r"^(\s*)([A-Za-z0-9_.-]+)(\s*=)(.*)$", line)
    if not key_match:
        return escaped
    leading, key, separator, value = key_match.groups()
    return (
        f"{html.escape(leading)}"
        f'<span class="config-key">{html.escape(key)}</span>'
        f"{html.escape(separator)}"
        f'<span class="config-value">{html.escape(value)}</span>'
    )


def is_raw_html(line: str) -> bool:
    return line.startswith("<") and line.endswith(">")


def render_shortcode(line: str) -> str | None:
    match = re.match(r"^\{\{\s*([a-z-]+):\s*(.*?)\s*\}\}$", line)
    if not match:
        return None
    name, value = match.groups()
    if name == "kicker":
        return f'<div class="kicker">{render_inline(value)}</div>'
    if name == "kicker-p":
        return f'<p class="kicker">{render_inline(value)}</p>'
    if name == "lede":
        return f'<p class="lede">{render_inline(value)}</p>'
    if name == "text":
        return render_inline(value)
    if name == "tag":
        return f'<span class="tag">{render_inline(value)}</span>'
    if name == "link":
        label, href, css_class = parse_shortcode_link(value)
        class_attr = f' class="{escape_attr(css_class)}"' if css_class else ""
        return (
            f'<a{class_attr} href="{escape_attr(href)}"'
            f"{render_tooltip_attrs(label)}>{escape_text(label)}</a>"
        )
    if name == "button":
        label, href, css_class = parse_shortcode_link(value)
        return render_button(label, href, css_class)
    if name == "node-text":
        title, separator, text = value.partition("|")
        if not separator:
            raise ValueError(f"Expected title|text for node-text: {value}")
        return f"<strong>{render_inline(title.strip())}</strong><span>{render_inline(text.strip())}</span>"
    if name == "screenshot":
        src, _, alt = value.partition("|")
        return (
            f'<img src="{escape_attr(src.strip())}" alt="{escape_attr(alt.strip())}" />'
        )
    raise ValueError(f"Unknown shortcode: {name}")


def parse_shortcode_link(value: str) -> tuple[str, str, str]:
    parts = [part.strip() for part in value.split("|")]
    if len(parts) not in {2, 3}:
        raise ValueError(f"Expected label|href or label|href|class: {value}")
    label, href = parts[:2]
    css_class = parts[2] if len(parts) == 3 else ""
    return label, href, css_class


def render_container(spec: str) -> tuple[str, str]:
    parts = shlex.split(spec)
    if not parts:
        raise ValueError("Empty container")
    name = parts[0]
    values = parts[1:]
    attrs = parse_attrs(values)

    if name == "section":
        id_attr = f' id="{escape_attr(attrs.pop("id"))}"' if "id" in attrs else ""
        return f"<section{id_attr}>", "</section>"
    if name == "wrap":
        return div_with_class("wrap", values, attrs)
    if name == "block":
        return div_with_class("", values, attrs)
    if name == "split":
        return div_with_class("wrap split", values, attrs)
    if name == "showcase":
        return div_with_class("wrap showcase", values, attrs)
    if name == "grid":
        return div_with_class("grid", values, attrs)
    if name == "section-head":
        return div_with_class("section-head", values, attrs)
    if name == "steps":
        return div_with_class("steps", values, attrs)
    if name == "step":
        return article_with_class("step", values, attrs)
    if name == "card":
        return article_with_class("card", values, attrs)
    if name == "code-panel":
        title = attrs.pop("title", "")
        attrs.pop("subtitle", "")
        open_tag, close_tag = div_with_class("code-panel", values, attrs)
        header = (
            '<header class="code-panel-top">'
            '<div class="window-controls" aria-hidden="true">'
            '<span class="dot"></span><span class="dot"></span><span class="dot"></span>'
            "</div>"
            f'<span class="code-panel-title">{escape_text(title)}</span>'
            '<button class="copy-button" type="button">Copy</button>'
            "</header>"
        )
        return f"{open_tag}\n{header}", close_tag
    if name == "table-wrap":
        return div_with_class("table-wrap", values, attrs)
    if name == "callout":
        return div_with_class("callout", values, attrs)
    if name == "guide-panel":
        return div_with_class("guide-panel", values, attrs)
    if name == "timeline":
        return div_with_class("timeline", values, attrs)
    if name == "node":
        return div_with_class("node", values, attrs)
    if name == "screenshot":
        return div_with_class("screenshot", values, attrs)
    if name == "actions":
        return div_with_class("actions", values, attrs)
    if name == "button-row":
        return div_with_class("button-row", values, attrs)
    if name == "raw":
        return "", ""

    raise ValueError(f"Unknown container: {name}")


def parse_attrs(values: list[str]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    remaining: list[str] = []
    for value in values:
        key, separator, attr_value = value.partition("=")
        if separator:
            attrs[key] = attr_value
        else:
            remaining.append(value)
    values[:] = remaining
    return attrs


def div_with_class(
    base_class: str, extra_classes: list[str], attrs: dict[str, str]
) -> tuple[str, str]:
    classes = " ".join([base_class, *extra_classes]).strip()
    attrs_text = render_attrs({"class": classes, **attrs})
    return f"<div{attrs_text}>", "</div>"


def article_with_class(
    base_class: str, extra_classes: list[str], attrs: dict[str, str]
) -> tuple[str, str]:
    classes = " ".join([base_class, *extra_classes]).strip()
    attrs_text = render_attrs({"class": classes, **attrs})
    return f"<article{attrs_text}>", "</article>"


def render_attrs(attrs: dict[str, str]) -> str:
    attrs = {key: value for key, value in attrs.items() if value}
    if not attrs:
        return ""
    return "".join(f' {key}="{escape_attr(value)}"' for key, value in attrs.items())


def render_inline(text: str) -> str:
    safe_tokens: list[str] = []

    def stash_token(token: str) -> str:
        safe_tokens.append(token)
        return f"\x00{len(safe_tokens) - 1}\x00"

    def stash_code(match: re.Match[str]) -> str:
        return stash_token(f"<code>{html.escape(match.group(1))}</code>")

    text = re.sub(r"`([^`]+)`", stash_code, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)(\{([^}]+)\})?", replace_link, text)
    text = add_abbreviations_to_html(text)

    for index, token in enumerate(safe_tokens):
        text = text.replace(f"\x00{index}\x00", token)
    return text


def add_auto_markup(text: str) -> str:
    if AUTOMARK_PATTERN is None:
        return text

    def replace(match: re.Match[str]) -> str:
        label = match.group(1)
        if label in LINKS:
            href = LINKS[label]
            tooltip_attrs = render_tooltip_attrs(label)
            return (
                f'<a class="term-link" href="{escape_attr(href)}"{tooltip_attrs}>'
                f"{label}</a>"
            )
        if label in ABBREVIATIONS:
            return f"<abbr{render_tooltip_attrs(label)}>{label}</abbr>"
        return label

    return AUTOMARK_PATTERN.sub(replace, text)


def add_abbreviations_to_html(raw_html: str) -> str:
    if AUTOMARK_PATTERN is None:
        return raw_html

    pieces = re.split(r"(<[^>]+>)", raw_html)
    output: list[str] = []
    skip_depth = 0
    skipped_tags = {"a", "abbr", "code", "pre", "script", "style"}
    tag_pattern = re.compile(r"^</?\s*([a-zA-Z0-9-]+)")

    for piece in pieces:
        if not piece:
            continue
        if piece.startswith("<") and piece.endswith(">"):
            match = tag_pattern.match(piece)
            if match:
                tag_name = match.group(1).lower()
                is_end = piece.startswith("</")
                is_self_closing = piece.endswith("/>")
                if tag_name in skipped_tags:
                    if is_end:
                        skip_depth = max(0, skip_depth - 1)
                    elif not is_self_closing:
                        skip_depth += 1
            output.append(piece)
        elif skip_depth:
            output.append(piece)
        else:
            output.append(add_auto_markup(piece))

    return "".join(output)


def replace_link(match: re.Match[str]) -> str:
    label = match.group(1)
    href = match.group(2)
    attr_text = match.group(4) or ""
    classes = " ".join(part[1:] for part in attr_text.split() if part.startswith("."))
    class_attr = f' class="{escape_attr(classes)}"' if classes else ""
    return (
        f'<a{class_attr} href="{escape_attr(href)}"'
        f"{render_tooltip_attrs(html.unescape(label))}>{label}</a>"
    )


def render_tooltip_attrs(text: str) -> str:
    if AUTOMARK_PATTERN is None:
        return ""
    titles: list[str] = []
    seen: set[str] = set()
    for match in AUTOMARK_PATTERN.finditer(text):
        label = match.group(1)
        title = ABBREVIATIONS.get(label)
        if title and label not in seen:
            titles.append(f"{label}: {title}")
            seen.add(label)
    if not titles:
        return ""
    tooltip = " | ".join(titles)
    return f' data-tooltip="{escape_attr(tooltip)}" aria-label="{escape_attr(tooltip)}"'


def escape_text(value: str) -> str:
    return html.escape(value, quote=False)


def escape_attr(value: str) -> str:
    return html.escape(value, quote=True)


def indent(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line else "" for line in text.splitlines())


if __name__ == "__main__":
    main()
