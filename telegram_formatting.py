"""Render model Markdown to bounded Telegram HTML using only the standard library.

Input remains Markdown in saved results. Never pass model-supplied HTML through.
Unsupported or incomplete syntax stays readable rather than rejecting a message.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser
import re
import string
from urllib.parse import urlsplit


ADMONITION_LABELS = {
    "NOTE": "📝 说明", "TIP": "💡 提示", "IMPORTANT": "❗ 重要",
    "WARNING": "⚠️ 警告", "CAUTION": "🚨 注意",
}
MERMAID_START = re.compile(
    r"^\s*(?:flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|mindmap|timeline)\b",
    re.IGNORECASE,
)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\n]*)$")
HTML_TOKEN = re.compile(
    r"</?(?:b|i|s|code|pre|a|blockquote|tg-spoiler)(?:\s+[^<>]*?)?>"
    r"|&(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);|[\s\S]"
)
TAG_NAME = re.compile(r"</?([a-z-]+)")


def clean_model_reply(text: str) -> str:
    """Make Markdown-oriented model output comfortable to read as Telegram plain text."""
    lines: list[str] = []
    in_code_block = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            lines.append(line)
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "• ", line)
        line = re.sub(r"(?<!\w)(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1", r"\2", line)
        line = re.sub(r"^\s*>\s?", "│ ", line)
        lines.append(line)
    return "\n".join(lines).strip()


def control_html(text: str) -> str:
    """Style control text without interpreting arbitrary Markdown or HTML."""
    lines = [line for line in text.splitlines() if not re.fullmatch(r"━+", line)]
    if not lines:
        return ""

    def inline(value: str) -> str:
        parts = re.split(r"(`[^`\n]+`)", value)
        return "".join("<code>" + html.escape(part[1:-1]) + "</code>"
                       if part.startswith("`") and part.endswith("`") and len(part) > 2
                       else html.escape(part) for part in parts)
    title = "".join(part if part.startswith("<code>") else "<b>" + part + "</b>"
                    for part in re.split(r"(<code>.*?</code>)", inline(lines[0])) if part)
    body = "\n".join(inline(line) for line in lines[1:]).strip()
    return title + ("\n\n" + body if body else "")


def _escaped(text: str) -> str:
    return html.escape(html.unescape(text))


def _safe_url(value: str) -> bool:
    if any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _code_span(text: str, index: int, budget: list[int]) -> tuple[int, str] | None:
    run = re.match(r"`+", text[index:]).group(0)
    previous = 0
    for match in re.finditer(r"`+", text[index + len(run):]):
        budget[0] -= match.end() - previous
        previous = match.end()
        if budget[0] <= 0:
            return None
        if match.group(0) == run:
            end = index + len(run) + match.start()
            value = text[index + len(run):end].replace("\n", " ")
            if value.startswith(" ") and value.endswith(" ") and value.strip():
                value = value[1:-1]
            return end + len(run), value
    return None


def _bracket_end(text: str, index: int, opening: str, closing: str, budget: list[int]) -> int | None:
    depth, position = 1, index + 1
    while position < len(text):
        budget[0] -= 1
        if budget[0] <= 0:
            return None
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if not depth:
                return position
        position += 1
    return None


def _link(text: str, index: int, budget: list[int]) -> tuple[int, str, str] | None:
    if text.find("](", index + 1) < 0:
        return None
    label_end = _bracket_end(text, index, "[", "]", budget)
    if label_end is None or text[label_end + 1:label_end + 2] != "(":
        return None
    end = _bracket_end(text, label_end + 1, "(", ")", budget)
    if end is None:
        return None
    destination = text[label_end + 2:end].strip()
    # Common Markdown destinations can have angle brackets and a quoted title.
    match = re.fullmatch(r'(?:<([^<>]+)>|(\S+?))(?:\s+["\'].*["\'])?', destination)
    if not match:
        return None
    url = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", match.group(1) or match.group(2))
    url = html.unescape(url)
    if not _safe_url(url):
        return None
    return end + 1, text[index + 1:label_end], url


def _closing_emphasis(text: str, index: int, marker: str, budget: list[int], outer: int = 0) -> int | None:
    position = index + len(marker)
    while position < len(text):
        budget[0] -= 1
        if budget[0] <= 0:
            return None
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == "`":
            span = _code_span(text, position, budget)
            if span:
                position = span[0]
                continue
        if char == "[":
            link = _link(text, position, budget)
            if link:
                position = link[0]
                continue
        if text.startswith(marker[0], position):
            end = position
            while end < len(text) and text[end] == marker[0]:
                end += 1
            count = end - position
            after = text[end:end + 1]
            if len(marker) == 1 and count == 2 and after and not after.isspace():
                nested = _closing_emphasis(text, position, marker * 2, budget, outer=1)
                if nested is not None:
                    position = nested + 2
                    continue
            if (count >= len(marker) and position > index + len(marker)
                    and not text[position - 1].isspace()
                    and not (marker[0] == "_" and after and after.isalnum())):
                # Leave a shorter inner delimiter before this outer closing run.
                return end - len(marker) - (outer if count >= len(marker) + outer else 0)
            position = end
            continue
        position += 1
    return None


def _format(tags: tuple[str, ...], body: str) -> str:
    # Telegram forbids code inside emphasis. Suspend formatting around code spans.
    parts = re.split(r"(<code>.*?</code>)", body, flags=re.DOTALL)
    return "".join(part if part.startswith("<code>") else
                   "".join(f"<{tag}>" for tag in tags) + part
                   + "".join(f"</{tag}>" for tag in reversed(tags))
                   for part in parts if part)


def render_inline_markdown(text: str, *, depth: int = 0, rich: bool = True,
                           _budget: list[int] | None = None) -> str:
    """Handle nested emphasis, literal code, escapes and balanced HTTP(S) links."""
    if depth >= 12:
        return _escaped(text)
    # Malformed model output must not make repeated unmatched scans quadratic.
    # At the budget boundary, keep the remaining Markdown as literal text.
    budget = [len(text) * 12 + 512] if _budget is None else _budget
    result: list[str] = []
    plain: list[str] = []

    def flush() -> None:
        if plain:
            result.append(_escaped("".join(plain)))
            plain.clear()

    position = 0
    while position < len(text):
        if budget[0] <= 0:
            flush()
            result.append(_escaped(text[position:]))
            break
        char = text[position]
        if char == "\\" and position + 1 < len(text) and text[position + 1] in string.punctuation:
            # Keep escaped entity-looking text literal, too.
            flush()
            result.append(html.escape(text[position + 1]))
            position += 2
            continue
        if char == "`":
            span = _code_span(text, position, budget)
            if span:
                flush()
                value = html.escape(span[1])
                result.append(f"<code>{value}</code>" if rich else value)
                position = span[0]
                continue
            run = re.match(r"`+", text[position:]).group(0)
            plain.append(run)
            position += len(run)
            continue
        if char == "[":
            link = _link(text, position, budget)
            if link:
                flush()
                label = render_inline_markdown(link[1], depth=depth + 1, rich=False, _budget=budget)
                url = html.escape(link[2], quote=True)
                result.append(f'<a href="{url}">{label}</a>' if rich else f"{label} ({url})")
                position = link[0]
                continue
        if char == "<":
            br = re.match(r"<br\s*/?>", text[position:], re.IGNORECASE)
            auto = re.match(r"<(https?://[^<>\s]+)>", text[position:]) if rich else None
            if br:
                flush()
                result.append("\n")
                position += br.end()
                continue
            if auto and _safe_url(html.unescape(auto.group(1))):
                flush()
                url = html.escape(html.unescape(auto.group(1)), quote=True)
                result.append(f'<a href="{url}">{url}</a>')
                position += auto.end()
                continue
        if char in "*_~|":
            run = 1
            while position + run < len(text) and text[position + run] == char:
                run += 1
            width = min(run, 3) if char in "*_" else (2 if run == 2 else 0)
            marker = char * width
            after = text[position + width:position + width + 1]
            before = text[position - 1:position] if position else ""
            if (width and run <= 3 and after and not after.isspace()
                    and not (char == "_" and before and before.isalnum())):
                end = _closing_emphasis(text, position, marker, budget)
                if end is not None:
                    flush()
                    tags = (("b", "i") if width == 3 else ("b",) if width == 2 and char in "*_"
                            else ("s",) if char == "~" else ("tg-spoiler",) if char == "|" else ("i",))
                    body = render_inline_markdown(text[position + width:end], depth=depth + 1, rich=rich, _budget=budget)
                    result.append(_format(tags, body))
                    position = end + width
                    continue
            # A failed run stays literal; don't reinterpret its last character.
            plain.append(char * run)
            position += run
            continue
        plain.append(char)
        position += 1
    flush()
    return "".join(result)


def table_cells(line: str) -> list[str]:
    source = line.strip()
    if source.startswith("|"):
        source = source[1:]
    if source.endswith("|") and not source.endswith("\\|"):
        source = source[:-1]
    cells, cell = [], []
    position, code_run = 0, ""
    while position < len(source):
        char = source[position]
        if char == "\\" and position + 1 < len(source):
            cell.append(source[position:position + 2])
            position += 2
            continue
        if char == "`":
            run = re.match(r"`+", source[position:]).group(0)
            if not code_run:
                code_run = run
            elif code_run == run:
                code_run = ""
            cell.append(run)
            position += len(run)
            continue
        if char == "|" and not code_run:
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(char)
        position += 1
    cells.append("".join(cell).strip())
    return cells


def is_table_separator(line: str) -> bool:
    cells = table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def render_markdown_html(text: str, *, quoted: bool = False) -> str:
    """Convert common Markdown, consuming fenced code before any block syntax."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    index = 0

    def inline(value: str) -> str:
        return render_inline_markdown(value, rich=not quoted)

    def code_block(value: str, language: str = "") -> str:
        if quoted:
            return html.escape(value)
        attribute = f' class="language-{language}"' if language else ""
        return f"<pre><code{attribute}>{html.escape(value)}</code></pre>"

    while index < len(raw):
        line = raw[index]
        fence = FENCE.match(line)
        if fence:
            marker = fence.group(1)
            info = fence.group(2).strip().split()
            language = info[0] if info and re.fullmatch(r"[A-Za-z0-9_+.#-]{1,32}", info[0]) else ""
            closing = re.compile(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$")
            content = []
            index += 1
            while index < len(raw) and not closing.fullmatch(raw[index]):
                content.append(raw[index])
                index += 1
            lines.append(code_block("\n".join(content), language))
            if index < len(raw):
                index += 1
            continue
        quote = re.match(r"^ {0,3}>\s?(.*)$", line)
        if quote and not quoted:
            content = []
            while index < len(raw):
                match = re.match(r"^ {0,3}>\s?(.*)$", raw[index])
                if not match:
                    break
                # Telegram disallows nested blockquotes. Preserve one quote level.
                content.append(re.sub(r"^(?:>\s?)+", "", match.group(1)))
                index += 1
            first = re.match(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)]\s*(.*)$", content[0], re.IGNORECASE)
            if first:
                label = ADMONITION_LABELS[first.group(1).upper()]
                content[0] = f"**{label}**" + (f"：{first.group(2)}" if first.group(2) else "")
            credit = ""
            code_fence = ""
            for quoted_line in content[:-1]:
                candidate = FENCE.match(quoted_line)
                if candidate:
                    marker = candidate.group(1)
                    if not code_fence:
                        code_fence = marker
                    elif marker[0] == code_fence[0] and len(marker) >= len(code_fence) and not candidate.group(2).strip():
                        code_fence = ""
            if not code_fence and len(content) > 1 and re.match(r"^(?:[—–-]\s*)?(?:来源|出处|Source)\s*[:：]", content[-1], re.IGNORECASE):
                credit = content.pop()
            body = render_markdown_html("\n".join(content), quoted=True)
            attribute = " expandable" if len(content) > 4 or len(body) > 600 else ""
            lines.append(f"<blockquote{attribute}>{body}</blockquote>" if body else "")
            if credit:
                lines.append("— " + render_inline_markdown(re.sub(r"^[—–-]\s*", "", credit)))
            continue
        if "|" in line and index + 1 < len(raw) and is_table_separator(raw[index + 1]):
            headers = table_cells(line)
            if len(headers) == len(table_cells(raw[index + 1])):
                index += 2
                while index < len(raw) and "|" in raw[index] and raw[index].strip():
                    cells = table_cells(raw[index])
                    fields = []
                    for column in range(max(len(headers), len(cells))):
                        header = headers[column] if column < len(headers) else f"列 {column + 1}"
                        value = cells[column] if column < len(cells) else ""
                        fields.append(_format(("b",), inline(header)) + "：" + inline(value))
                    lines.append("\n".join(fields))
                    index += 1
                continue
        if MERMAID_START.match(line):
            diagram = [line]
            index += 1
            while index < len(raw) and raw[index].strip() and not FENCE.match(raw[index]):
                diagram.append(raw[index])
                index += 1
            lines.append(code_block("\n".join(diagram)))
            continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*$", line)
        bullet = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        numbered = re.match(r"^(\s*)(\d{1,9})[.)]\s+(.+)$", line)
        if heading:
            value = re.sub(r"\s+#+\s*$", "", heading.group(1))
            lines.append(_format(("b",), inline(value)))
        elif line.strip() in {"---", "***", "___"}:
            lines.append("")
        elif bullet or numbered:
            match = bullet or numbered
            indent = "　" * min(3, len(match.group(1).expandtabs(2)) // 2)
            value = bullet.group(2) if bullet else numbered.group(3)
            check = re.match(r"^\[([ xX])]\s+(.*)$", value) if bullet else None
            prefix = ("☑ " if check.group(1).lower() == "x" else "☐ ") if check else ("• " if bullet else numbered.group(2) + ". ")
            lines.append(indent + prefix + inline(check.group(2) if check else value))
        else:
            lines.append(inline(line))
        index += 1
    return "\n".join(lines).strip()


def html_chunks(text: str, units: int = 3400) -> list[str]:
    """Split at rendered UTF-16 boundaries, preserving whole entities and tags."""
    if units < 2:
        raise ValueError("HTML chunk limit must accommodate a Unicode character")
    chunks: list[str] = []
    current: list[str] = []
    stack: list[tuple[str, str]] = []
    used = 0
    boundary: tuple[int, int, list[tuple[str, str]]] | None = None

    def close_tags() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(stack))

    for match in HTML_TOKEN.finditer(text):
        token = match.group(0)
        name = TAG_NAME.match(token) if token.startswith("<") else None
        if name:
            if name.group(1) == "pre" and not token.startswith("</") and used:
                end = text.find("</pre>", match.end())
                if end >= 0:
                    size = len(plain_html_text(text[match.start():end + 6]).encode("utf-16-le")) // 2
                    if size <= units and used + size > units:
                        chunks.append("".join(current) + close_tags())
                        current = [opening for _, opening in stack]
                        used, boundary = 0, None
            current.append(token)
            if token.startswith("</"):
                if stack and stack[-1][0] == name.group(1):
                    stack.pop()
            else:
                stack.append((name.group(1), token))
            if token.startswith("</") and name.group(1) in {"pre", "blockquote"}:
                boundary = (len(current), used, list(stack))
            continue
        value = html.unescape(token)
        size = sum(2 if ord(char) > 0xFFFF else 1 for char in value)
        if used and used + size > units:
            if boundary and boundary[1] >= units // 2:
                cut, consumed, previous = boundary
                closing = "".join(f"</{tag}>" for tag, _ in reversed(previous))
                chunks.append("".join(current[:cut]) + closing)
                current = [opening for _, opening in previous] + current[cut:]
                used -= consumed
            else:
                chunks.append("".join(current) + close_tags())
                current = [opening for _, opening in stack]
                used = 0
            boundary = None
        current.append(token)
        used += size
        if token == "\n" and not any(tag in {"pre", "code"} for tag, _ in stack):
            boundary = (len(current), used, list(stack))
    if used or not chunks:
        chunks.append("".join(current) + close_tags())
    return chunks


class _PlainHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.link = ""
        self.link_start = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.link = dict(attrs).get("href") or ""
            self.link_start = len(self.parts)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.link:
            if "".join(self.parts[self.link_start:]) != self.link:
                self.parts.append(f" ({self.link})")
            self.link = ""

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_html_text(text: str) -> str:
    """A definite HTML rejection can fall back without losing link destinations."""
    parser = _PlainHTML()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)
