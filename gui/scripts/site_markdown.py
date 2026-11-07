from __future__ import annotations

import html
import re
import shlex

try:
    from .site_html import escape_attr, escape_text, render_button
except ImportError:  # pragma: no cover - script execution path
    from site_html import escape_attr, escape_text, render_button


class MarkdownRenderer:
    def __init__(self, markdown: str) -> None:
        self.lines = markdown.splitlines()
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
                self.output.append(line)
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
            self.output.append(f"<p>{render_inline(' '.join(self.paragraph))}</p>")
            self.paragraph = []

    def open_container(self, spec: str) -> None:
        opener, closer = render_container(spec)
        self.output.append(opener)
        self.closers.append(closer)

    def read_raw_block(self) -> str:
        self.index += 1
        raw_lines: list[str] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            self.index += 1
            if line.strip() == ":::":
                return "\n".join(raw_lines)
            raw_lines.append(line)
        raise ValueError("Unterminated raw block")

    def read_code_block(self) -> str:
        language = self.lines[self.index].strip()[3:].strip()
        self.index += 1
        code_lines: list[str] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            self.index += 1
            if line.strip().startswith("```"):
                code = "\n".join(html.escape(item) for item in code_lines)
                return f'<pre data-language="{escape_attr(language)}"><code>{code}</code></pre>'
            code_lines.append(line)
        raise ValueError("Unterminated code block")

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
        row_html = "\n".join(
            "<tr>" + "".join(f"<td>{render_inline(cell)}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        return (
            '<div class="table-wrap"><table>\n'
            f"<thead><tr>{header_html}</tr></thead>\n"
            f"<tbody>\n{row_html}\n</tbody>\n"
            "</table></div>"
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


def is_raw_html(line: str) -> bool:
    return bool(re.match(r"^<[A-Za-z/!]", line)) and line.endswith(">")


def render_shortcode(line: str) -> str | None:
    match = re.match(r"^\{\{\s*([a-z-]+):\s*(.*?)\s*\}\}$", line)
    if not match:
        return None
    name, value = match.groups()
    if name == "kicker":
        return f'<div class="kicker">{render_inline(value)}</div>'
    if name == "tag":
        return f'<span class="tag">{render_inline(value)}</span>'
    if name == "button":
        label, href, css_class = parse_link_shortcode(value)
        return render_button(label, href, css_class)
    if name == "link":
        label, href, css_class = parse_link_shortcode(value)
        class_attr = f' class="{escape_attr(css_class)}"' if css_class else ""
        return f'<a{class_attr} href="{escape_attr(href)}">{escape_text(label)}</a>'
    raise ValueError(f"Unknown shortcode: {name}")


def parse_link_shortcode(value: str) -> tuple[str, str, str]:
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
    name, values = parts[0], parts[1:]
    attrs = parse_attrs(values)
    if name == "section":
        id_attr = f' id="{escape_attr(attrs.pop("id"))}"' if "id" in attrs else ""
        return f"<section{id_attr}>", "</section>"
    if name == "wrap":
        return div_with_class("wrap", values, attrs)
    if name == "grid":
        return div_with_class("grid", values, attrs)
    if name == "card":
        return article_with_class("card", values, attrs)
    if name == "split":
        return div_with_class("wrap split", values, attrs)
    if name == "section-head":
        return div_with_class("section-head", values, attrs)
    if name == "timeline":
        return div_with_class("timeline", values, attrs)
    if name == "node":
        return div_with_class("node", values, attrs)
    if name == "callout":
        return div_with_class("callout", values, attrs)
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
    return "".join(f' {key}="{escape_attr(value)}"' for key, value in attrs.items())


def render_inline(text: str) -> str:
    tokens: list[str] = []

    def stash(token: str) -> str:
        tokens.append(token)
        return f"\0{len(tokens) - 1}\0"

    text = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)(\{([^}]+)\})?", replace_link, text)
    for index, token in enumerate(tokens):
        text = text.replace(f"\0{index}\0", token)
    return text


def replace_link(match: re.Match[str]) -> str:
    label = match.group(1)
    href = html.unescape(match.group(2))
    attrs = match.group(4) or ""
    classes = " ".join(part[1:] for part in attrs.split() if part.startswith("."))
    class_attr = f' class="{escape_attr(classes)}"' if classes else ""
    return f'<a{class_attr} href="{escape_attr(href)}">{label}</a>'
