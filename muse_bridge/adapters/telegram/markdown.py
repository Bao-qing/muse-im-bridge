"""Translate CommonMark/GFM replies into Telegram Bot API text and MessageEntity dicts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


_FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^[ ]{0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*(?:[ \t]+#+[ \t]*)?$")
_HR_RE = re.compile(r"^[ ]{0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_QUOTE_RE = re.compile(r"^[ ]{0,3}> ?(.*)$")
_UL_RE = re.compile(r"^([ ]*)([-*+])[ \t]+(.*)$")
_OL_RE = re.compile(r"^([ ]*)(\d{1,9})([.)])[ \t]+(.*)$")
_TASK_RE = re.compile(r"^\[([ xX])\][ \t]+(.*)$")
_TABLE_SEP_RE = re.compile(r"^[ ]*\|?(?:[ ]*:?-{1,}:?[ ]*\|)+[ ]*:?-{1,}:?[ ]*\|?[ ]*$")
_LANG_RE = re.compile(r"^[A-Za-z0-9_+#.-]{1,32}$")
_ASCII_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_STYLE_TYPES = frozenset({"bold", "italic", "underline", "strikethrough", "spoiler"})


def utf16_len(text: str) -> int:
    if not text:
        return 0
    if text.isascii():
        return len(text)
    return len(text.encode("utf-16-le")) // 2


def _utf16_offsets(text: str) -> list[int]:
    offsets = [0] * (len(text) + 1)
    pos = 0
    for index, char in enumerate(text):
        pos += 1 if ord(char) <= 0xFFFF else 2
        offsets[index + 1] = pos
    return offsets


def _valid_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    cleaned = url.strip()
    if len(cleaned) >= 2 and cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1].strip()
    if not cleaned or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        return None
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return cleaned


def _split_link_target(raw: str) -> str | None:
    s = raw.strip()
    if not s:
        return None
    if s.startswith("<"):
        end = s.find(">")
        if end > 0:
            rest = s[end + 1 :].strip()
            if not rest or rest[0] in {'"', "'", "("}:
                return _valid_url(s[1:end])
        return None
    parts = s.split(None, 1)
    if len(parts) == 2 and parts[1][0] not in {'"', "'", "("}:
        return None
    return _valid_url(parts[0])


@dataclass
class _Span:
    kind: str
    start: int
    end: int
    url: str | None = None
    language: str | None = None


@dataclass
class _Delim:
    char: str
    orig_len: int
    length: int
    can_open: bool
    can_close: bool
    node_idx: int


def _is_whitespace_or_edge(text: str, index: int) -> bool:
    return index < 0 or index >= len(text) or text[index].isspace()


def _is_punct_or_edge(text: str, index: int) -> bool:
    if index < 0 or index >= len(text):
        return False
    ch = text[index]
    return ch in _ASCII_PUNCT or (not ch.isalnum() and not ch.isspace())


def _flanking(text: str, start: int, end: int, char: str) -> tuple[bool, bool]:
    before_ws = _is_whitespace_or_edge(text, start - 1)
    after_ws = _is_whitespace_or_edge(text, end)
    before_punct = _is_punct_or_edge(text, start - 1)
    after_punct = _is_punct_or_edge(text, end)

    left = not after_ws and (not after_punct or before_ws or before_punct)
    right = not before_ws and (not before_punct or after_ws or after_punct)
    if char == "_":
        return left and (not right or before_punct), right and (not left or after_punct)
    return left, right


def _find_bracket_link(text: str, start: int) -> tuple[str, str, int] | None:
    """Parse [label](url) starting at '['; skips escaped chars and inline code spans."""
    depth = 1
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in _ASCII_PUNCT:
            i += 2
            continue
        if ch == "`":
            ticks = 1
            while i + ticks < n and text[i + ticks] == "`":
                ticks += 1
            close = text.find("`" * ticks, i + ticks)
            while close != -1 and (
                (close > 0 and text[close - 1] == "`")
                or (close + ticks < n and text[close + ticks] == "`")
            ):
                close = text.find("`" * ticks, close + 1)
            i = close + ticks if close != -1 else i + ticks
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0 or i + 1 >= n or text[i + 1] != "(":
        return None
    label = text[start + 1 : i]
    j = i + 2
    paren_depth = 1
    while j < n:
        ch = text[j]
        if ch == "\\" and j + 1 < n and text[j + 1] in _ASCII_PUNCT:
            j += 2
            continue
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
            if paren_depth == 0:
                break
        elif ch == "\n":
            return None
        j += 1
    if paren_depth != 0:
        return None
    url = _split_link_target(text[i + 2 : j])
    if not url:
        return None
    return label, url, j + 1


class _Builder:
    def __init__(self):
        self.parts: list[str] = []
        self.offset = 0
        self.spans: list[_Span] = []

    def append_text(self, text: str) -> None:
        if text:
            self.parts.append(text)
            self.offset += utf16_len(text)

    def add_span(self, kind: str, start: int, end: int, *,
                 url: str | None = None, language: str | None = None) -> None:
        if end > start:
            self.spans.append(_Span(kind, start, end, url=url, language=language))

    def emit_inline(self, text: str, *, in_link: bool = False, in_blockquote: bool = False) -> None:
        nodes = self._tokenize_inline(text, in_link=in_link, in_blockquote=in_blockquote)
        self._resolve_delimiters(nodes)
        self._render_nodes(nodes)

    def _tokenize_inline(self, text: str, *, in_link: bool, in_blockquote: bool) -> list:
        nodes: list = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\\" and i + 1 < n and text[i + 1] in _ASCII_PUNCT:
                nodes.append(("text", text[i + 1]))
                i += 2
                continue
            if ch == "`":
                ticks = 1
                while i + ticks < n and text[i + ticks] == "`":
                    ticks += 1
                close = i + ticks
                found = -1
                while close < n:
                    idx = text.find("`" * ticks, close)
                    if idx == -1:
                        break
                    before_ok = idx == 0 or text[idx - 1] != "`"
                    after_ok = idx + ticks >= n or text[idx + ticks] != "`"
                    if before_ok and after_ok:
                        found = idx
                        break
                    close = idx + 1
                if found != -1:
                    inner = text[i + ticks : found]
                    if len(inner) >= 2 and inner[0] == " " and inner[-1] == " " and inner.strip(" "):
                        inner = inner[1:-1]
                    if in_link or in_blockquote:
                        nodes.append(("text", inner))
                    else:
                        nodes.append(("code", inner))
                    i = found + ticks
                    continue
                nodes.append(("text", "`" * ticks))
                i += ticks
                continue
            if ch == "<":
                gt = text.find(">", i + 1)
                if gt != -1 and "\n" not in text[i + 1 : gt]:
                    candidate = _valid_url(text[i + 1 : gt])
                    if candidate:
                        nodes.append(("text", candidate))
                        i = gt + 1
                        continue
            if ch == "!" and i + 1 < n and text[i + 1] == "[" and not in_link:
                parsed = _find_bracket_link(text, i + 1)
                if parsed is not None:
                    alt, url, next_i = parsed
                    if in_blockquote:
                        nodes.append(("text", f"{alt} ({url})" if alt.strip() else url))
                    elif alt.strip():
                        nodes.append(("link", url, self._tokenize_inline(alt, in_link=True, in_blockquote=False)))
                    else:
                        nodes.append(("text", url))
                    i = next_i
                    continue
            if ch == "[" and not in_link:
                parsed = _find_bracket_link(text, i)
                if parsed is not None:
                    label, url, next_i = parsed
                    if in_blockquote:
                        if label.strip() and label.strip() != url:
                            nodes.extend(self._tokenize_inline(label, in_link=True, in_blockquote=True))
                            nodes.append(("text", f" ({url})"))
                        else:
                            nodes.append(("text", url))
                    elif label.strip():
                        nodes.append(("link", url, self._tokenize_inline(label, in_link=True, in_blockquote=False)))
                    else:
                        nodes.append(("text", url))
                    i = next_i
                    continue
            if ch in {"*", "_"}:
                end = i + 1
                while end < n and text[end] == ch:
                    end += 1
                length = end - i
                can_open, can_close = _flanking(text, i, end, ch)
                if can_open or can_close:
                    nodes.append(["delim", _Delim(ch, length, length, can_open, can_close, len(nodes))])
                else:
                    nodes.append(("text", ch * length))
                i = end
                continue
            if ch in {"~", "|"} and i + 1 < n and text[i + 1] == ch and (i + 2 >= n or text[i + 2] != ch):
                end = i + 2
                can_open, can_close = _flanking(text, i, end, ch)
                if can_open or can_close:
                    nodes.append(["delim", _Delim(ch * 2, 2, 2, can_open, can_close, len(nodes))])
                else:
                    nodes.append(("text", ch * 2))
                i = end
                continue
            start = i
            i += 1
            while i < n and text[i] not in {"\\", "`", "<", "!", "[", "*", "_", "~", "|"}:
                i += 1
            nodes.append(("text", text[start:i]))
        return nodes

    @staticmethod
    def _resolve_delimiters(nodes: list) -> None:
        openers_bottom: dict[tuple[str, int], int] = {}
        active: list[_Delim] = []
        idx = 0
        while idx < len(nodes):
            item = nodes[idx]
            if isinstance(item, list) and item[0] == "delim":
                active.append(item[1])
            idx += 1

        # Match closers left-to-right against the active delimiter list.
        pos = 0
        while pos < len(active):
            closer = active[pos]
            if not closer.can_close or closer.length == 0:
                pos += 1
                continue
            opener_pos = pos - 1
            bottom = openers_bottom.get((closer.char, closer.orig_len % 3), -1)
            matched = False
            while opener_pos > bottom:
                opener = active[opener_pos]
                if opener.can_open and opener.length > 0 and opener.char == closer.char:
                    # CommonMark rule of 3 for interior flanking delimiters.
                    if not (
                        (opener.can_close or closer.can_open)
                        and (opener.orig_len + closer.orig_len) % 3 == 0
                        and (opener.orig_len % 3 != 0 or closer.orig_len % 3 != 0)
                    ):
                        matched = True
                        break
                opener_pos -= 1
            if not matched:
                openers_bottom[(closer.char, closer.orig_len % 3)] = pos - 1
                if not closer.can_open:
                    closer.can_close = False
                pos += 1
                continue

            opener = active[opener_pos]
            if closer.char in {"~~", "||"}:
                take = 2
                kind = "strikethrough" if closer.char == "~~" else "spoiler"
            elif opener.length >= 2 and closer.length >= 2:
                take = 2
                kind = "bold"
            else:
                take = 1
                kind = "italic"

            opener.length -= take
            closer.length -= take
            # Wrap AST nodes between opener.node_idx and closer.node_idx.
            inner_nodes = nodes[opener.node_idx + 1 : closer.node_idx]
            wrapped = ("styled", kind, list(inner_nodes))
            nodes[opener.node_idx + 1 : closer.node_idx] = [wrapped]
            delta = (closer.node_idx - opener.node_idx - 1) - 1
            for d in active[opener_pos + 1 : pos]:
                d.can_open = False
                d.can_close = False
            for d in active[pos:]:
                d.node_idx -= delta
            if closer.length == 0:
                pos += 1

        # Convert remaining delimiter tokens into literal text.
        for i, item in enumerate(nodes):
            if isinstance(item, list) and item[0] == "delim":
                nodes[i] = ("text", _Builder._delim_text(item[1]))

    @staticmethod
    def _delim_text(d: _Delim) -> str:
        if d.char in {"*", "_"}:
            return d.char * d.length
        return d.char if d.length == 2 else ""

    def _render_nodes(self, nodes: list) -> None:
        for node in nodes:
            tag = node[0]
            if tag == "text":
                self.append_text(node[1])
            elif tag == "delim":
                self.append_text(self._delim_text(node[1]))
            elif tag == "code":
                start = self.offset
                self.append_text(node[1])
                self.add_span("code", start, self.offset)
            elif tag == "link":
                _, url, inner = node
                start = self.offset
                self._resolve_delimiters(inner)
                self._render_nodes(inner)
                self.add_span("text_link", start, self.offset, url=url)
            elif tag == "styled":
                _, kind, inner = node
                start = self.offset
                self._render_nodes(inner)
                self.add_span(kind, start, self.offset)

    def build_entities(self) -> list[dict]:
        """Enforce Telegram MessageEntity nesting rules and sort deterministically."""
        code_ranges = [(s.start, s.end) for s in self.spans if s.kind in {"code", "pre"}]
        code_ranges.sort()
        entities: list[dict] = []

        for span in self.spans:
            if span.end <= span.start:
                continue
            if span.kind in _STYLE_TYPES and code_ranges:
                cursor = span.start
                for c_start, c_end in code_ranges:
                    if c_end <= cursor:
                        continue
                    if c_start >= span.end:
                        break
                    if c_start > cursor:
                        entities.append({"type": span.kind, "offset": cursor, "length": c_start - cursor})
                    cursor = max(cursor, c_end)
                if cursor < span.end:
                    entities.append({"type": span.kind, "offset": cursor, "length": span.end - cursor})
                continue
            entry: dict = {"type": span.kind, "offset": span.start, "length": span.end - span.start}
            if span.url is not None:
                entry["url"] = span.url
            if span.language is not None:
                entry["language"] = span.language
            entities.append(entry)

        entities.sort(key=lambda e: (e["offset"], -e["length"], e["type"]))
        deduped: list[dict] = []
        covered_styles: dict[str, list[tuple[int, int]]] = {}
        for entry in entities:
            kind = entry["type"]
            start, end = entry["offset"], entry["offset"] + entry["length"]
            if kind in _STYLE_TYPES:
                existing = covered_styles.setdefault(kind, [])
                if any(s <= start and end <= e for s, e in existing):
                    continue
                existing.append((start, end))
            deduped.append(entry)
        return deduped


def _split_table_row(line: str) -> list[str] | None:
    if "|" not in line:
        return None
    cells: list[str] = []
    cur: list[str] = []
    in_code = 0
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and not in_code and i + 1 < n:
            cur.append(line[i : i + 2])
            i += 2
            continue
        if ch == "`":
            ticks = 1
            while i + ticks < n and line[i + ticks] == "`":
                ticks += 1
            if in_code == 0:
                in_code = ticks
            elif in_code == ticks:
                in_code = 0
            cur.append("`" * ticks)
            i += ticks
            continue
        if ch == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells if cells else None


def parse_markdown(text: str) -> tuple[str, list[dict]]:
    """Parse CommonMark/GFM text into Telegram plain text and MessageEntity dicts."""
    if not text:
        return "", []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    builder = _Builder()

    def emit_line_break():
        if builder.parts:
            builder.append_text("\n")

    def emit_block_content(block_lines: list[str], *, in_blockquote: bool = False):
        idx = 0
        total = len(block_lines)
        while idx < total:
            line = block_lines[idx]
            if not line.strip():
                if builder.parts and not builder.parts[-1].endswith("\n\n"):
                    if builder.parts[-1].endswith("\n"):
                        builder.append_text("\n")
                    else:
                        builder.append_text("\n\n")
                idx += 1
                continue

            if not in_blockquote:
                fence_match = _FENCE_RE.match(line)
                if fence_match:
                    fence = fence_match.group(1)
                    info = fence_match.group(2).strip()
                    if fence[0] != "`" or "`" not in info:
                        lang_token = info.split(None, 1)[0] if info else ""
                        language = lang_token if _LANG_RE.fullmatch(lang_token) else None
                        code_lines: list[str] = []
                        idx += 1
                        close_re = re.compile(rf"^[ ]{{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ ]*$")
                        while idx < total and not close_re.match(block_lines[idx]):
                            code_lines.append(block_lines[idx])
                            idx += 1
                        if idx < total:
                            idx += 1  # consume closing fence
                        if builder.parts and not builder.parts[-1].endswith("\n"):
                            builder.append_text("\n")
                        start = builder.offset
                        code_text = "\n".join(code_lines)
                        builder.append_text(code_text)
                        if code_text:
                            builder.add_span("pre", start, builder.offset, language=language)
                        continue

            if not in_blockquote:
                quote_match = _QUOTE_RE.match(line)
                if quote_match:
                    quote_lines = [quote_match.group(1)]
                    idx += 1
                    while idx < total:
                        nxt = _QUOTE_RE.match(block_lines[idx])
                        if not nxt:
                            break
                        inner = nxt.group(1)
                        # Collapse nested '>' prefix visually; Telegram forbids nested blockquotes.
                        while inner.startswith(">"):
                            inner = inner[1:].lstrip(" ")
                        quote_lines.append(inner)
                        idx += 1
                    while quote_lines and not quote_lines[0].strip():
                        quote_lines.pop(0)
                    while quote_lines and not quote_lines[-1].strip():
                        quote_lines.pop()
                    if not quote_lines:
                        continue
                    if builder.parts and not builder.parts[-1].endswith("\n"):
                        builder.append_text("\n")
                    q_start = builder.offset
                    emit_block_content(quote_lines, in_blockquote=True)
                    builder.add_span("blockquote", q_start, builder.offset)
                    continue

            if builder.parts and not builder.parts[-1].endswith("\n"):
                builder.append_text("\n")

            if _HR_RE.match(line):
                builder.append_text("———")
                idx += 1
                continue

            heading_match = _HEADING_RE.match(line)
            if heading_match:
                heading_text = (heading_match.group(2) or "").strip()
                h_start = builder.offset
                builder.emit_inline(heading_text, in_blockquote=in_blockquote)
                builder.add_span("bold", h_start, builder.offset)
                idx += 1
                continue

            if idx + 1 < total and "|" in line and _TABLE_SEP_RE.match(block_lines[idx + 1]):
                header = _split_table_row(line)
                if header:
                    for col_i, cell in enumerate(header):
                        if col_i:
                            builder.append_text(" | ")
                        c_start = builder.offset
                        builder.emit_inline(cell, in_blockquote=in_blockquote)
                        builder.add_span("bold", c_start, builder.offset)
                    idx += 2
                    while idx < total:
                        row = _split_table_row(block_lines[idx])
                        if not row or _TABLE_SEP_RE.match(block_lines[idx]):
                            break
                        emit_line_break()
                        for col_i, cell in enumerate(row):
                            if col_i:
                                builder.append_text(" | ")
                            builder.emit_inline(cell, in_blockquote=in_blockquote)
                        idx += 1
                    continue

            ul_match = _UL_RE.match(line)
            if ul_match:
                indent, _, body = ul_match.groups()
                prefix_indent = "  " * (len(indent) // 2)
                task_match = _TASK_RE.match(body)
                if task_match:
                    mark = "☑ " if task_match.group(1).lower() == "x" else "☐ "
                    builder.append_text(prefix_indent + mark)
                    builder.emit_inline(task_match.group(2), in_blockquote=in_blockquote)
                else:
                    builder.append_text(prefix_indent + "• ")
                    builder.emit_inline(body, in_blockquote=in_blockquote)
                idx += 1
                continue

            ol_match = _OL_RE.match(line)
            if ol_match:
                indent, num, _, body = ol_match.groups()
                prefix_indent = "  " * (len(indent) // 2)
                builder.append_text(f"{prefix_indent}{num}. ")
                builder.emit_inline(body, in_blockquote=in_blockquote)
                idx += 1
                continue

            builder.emit_inline(line, in_blockquote=in_blockquote)
            idx += 1

    emit_block_content(lines)
    plain = "".join(builder.parts).rstrip("\n")
    max_u16 = utf16_len(plain)
    entities = []
    for entity in builder.build_entities():
        if entity["offset"] >= max_u16:
            continue
        end = min(max_u16, entity["offset"] + entity["length"])
        if end > entity["offset"]:
            entities.append({**entity, "length": end - entity["offset"]})
    return plain, entities


def _slice_entities(entities: list[dict], start_u16: int, end_u16: int) -> list[dict]:
    out: list[dict] = []
    for entity in entities:
        e_start = entity["offset"]
        e_end = e_start + entity["length"]
        if e_end <= start_u16 or e_start >= end_u16:
            continue
        clipped_start = max(e_start, start_u16)
        clipped_end = min(e_end, end_u16)
        if clipped_end > clipped_start:
            out.append({**entity, "offset": clipped_start - start_u16, "length": clipped_end - clipped_start})
    return out


def chunk_formatted(text: str, entities: list[dict], limit: int, max_chunks: int) -> list[tuple[str, list[dict]]]:
    """Split text + UTF-16 entities into bounded chunks without corrupting offsets."""
    if not text:
        return []
    limit = max(1, limit)
    max_chunks = max(1, max_chunks)
    total_cap = limit * max_chunks
    u16 = _utf16_offsets(text)

    if len(text) > total_cap:
        notice = "\n\n[内容已截断]"[:limit]
        keep_cp = max(0, total_cap - len(notice))
        entities = _slice_entities(entities, 0, u16[keep_cp])
        text = text[:keep_cp] + notice
        u16 = _utf16_offsets(text)

    chunks: list[tuple[str, list[dict]]] = []
    pos = 0
    n = len(text)
    while pos < n:
        if n - pos <= limit:
            chunks.append((text[pos:n], _slice_entities(entities, u16[pos], u16[n])))
            break
        nl = text.rfind("\n", pos, pos + limit + 1)
        if nl > pos and nl - pos >= limit // 4:
            split_end = nl
            next_pos = nl + 1
        else:
            sp = text.rfind(" ", pos, pos + limit + 1)
            if sp > pos + limit // 2:
                split_end = sp
                next_pos = sp + 1
            else:
                split_end = pos + limit
                next_pos = split_end
        chunk_text = text[pos:split_end]
        if chunk_text:
            chunks.append((chunk_text, _slice_entities(entities, u16[pos], u16[split_end])))
        pos = next_pos
    return chunks
