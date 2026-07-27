from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from markupsafe import Markup

_ORDERED_ITEM = re.compile(r"^\d+[.)]\s+(.+)$")
_UNORDERED_ITEM = re.compile(r"^[-+*]\s+(.+)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_HORIZONTAL_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")


def render_public_markdown(source: str) -> Markup:
    """Render the deliberately small Markdown subset used for public operator text."""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rendered: list[str] = []
    paragraph: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            content = "<br>\n".join(_render_inline(line) for line in paragraph)
            rendered.append(f"<p>{content}</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            index += 1
            continue

        heading = _HEADING.fullmatch(stripped)
        if heading:
            flush_paragraph()
            level = min(len(heading.group(1)) + 1, 6)
            rendered.append(f"<h{level}>{_render_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if _HORIZONTAL_RULE.fullmatch(stripped):
            flush_paragraph()
            rendered.append("<hr>")
            index += 1
            continue

        ordered = _ORDERED_ITEM.fullmatch(stripped)
        unordered = _UNORDERED_ITEM.fullmatch(stripped)
        if ordered or unordered:
            flush_paragraph()
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while index < len(lines):
                candidate = lines[index].strip()
                match = (
                    _ORDERED_ITEM.fullmatch(candidate)
                    if tag == "ol"
                    else _UNORDERED_ITEM.fullmatch(candidate)
                )
                if not match:
                    break
                items.append(f"<li>{_render_inline(match.group(1))}</li>")
                index += 1
            rendered.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        paragraph.append(stripped)
        index += 1

    flush_paragraph()
    return Markup("\n".join(rendered))


def _render_inline(value: str, depth: int = 0) -> str:
    if depth >= 8:
        return html.escape(value)

    rendered: list[str] = []
    plain: list[str] = []
    index = 0

    def flush_plain() -> None:
        if plain:
            rendered.append(html.escape("".join(plain)))
            plain.clear()

    while index < len(value):
        if value.startswith("![", index):
            closing_label = value.find("](", index + 2)
            closing_url = value.find(")", closing_label + 2) if closing_label >= 0 else -1
            if closing_url >= 0:
                plain.append(value[index:closing_url + 1])
                index = closing_url + 1
                continue

        if value.startswith("**", index):
            closing = value.find("**", index + 2)
            if closing > index + 2:
                flush_plain()
                rendered.append(
                    f"<strong>{_render_inline(value[index + 2:closing], depth + 1)}</strong>"
                )
                index = closing + 2
                continue

        if value[index] in {"*", "_"}:
            marker = value[index]
            closing = value.find(marker, index + 1)
            if closing > index + 1:
                flush_plain()
                rendered.append(
                    f"<em>{_render_inline(value[index + 1:closing], depth + 1)}</em>"
                )
                index = closing + 1
                continue

        if value[index] == "[":
            closing_label = value.find("](", index + 1)
            closing_url = value.find(")", closing_label + 2) if closing_label >= 0 else -1
            if closing_url >= 0:
                label = value[index + 1:closing_label]
                url = value[closing_label + 2:closing_url].strip()
                if _safe_link(url):
                    flush_plain()
                    rendered.append(
                        f'<a href="{html.escape(url, quote=True)}" '
                        f'rel="noopener noreferrer">{_render_inline(label, depth + 1)}</a>'
                    )
                    index = closing_url + 1
                    continue
                plain.append(value[index:closing_url + 1])
                index = closing_url + 1
                continue

        plain.append(value[index])
        index += 1

    flush_plain()
    return "".join(rendered)


def _safe_link(value: str) -> bool:
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        return False
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        return bool(parsed.netloc)
    if parsed.scheme == "mailto":
        address = parsed.path
        return bool(address and address.count("@") == 1)
    return False
