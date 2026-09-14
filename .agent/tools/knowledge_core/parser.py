"""Conservative, non-executing Markdown parsing for the knowledge index.

The parser intentionally understands only the block shapes needed by the
index.  It does not render Markdown, evaluate frontmatter, or execute code
fences.  Every parsed block points back to one-based inclusive source lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


@dataclass(frozen=True)
class ParseDiagnostic:
    code: str
    message: str
    severity: str = "warning"
    path: str | None = None

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message, "severity": self.severity}
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True)
class WikiLink:
    target: str
    alias: str | None
    raw: str
    line_start: int
    line_end: int

    @property
    def line(self) -> int:
        return self.line_start

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "alias": self.alias,
            "raw": self.raw,
            "line_start": self.line_start,
            "line_end": self.line_end,
        }


@dataclass(frozen=True)
class MarkdownBlock:
    kind: str
    line_start: int
    line_end: int
    content: str
    heading: str | None = None
    level: int | None = None

    @property
    def type(self) -> str:
        """Compatibility alias for callers that call blocks types."""

        return self.kind

    @property
    def start_line(self) -> int:
        return self.line_start

    @property
    def end_line(self) -> int:
        return self.line_end

    @property
    def text(self) -> str:
        return self.content

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "content": self.content,
            "heading": self.heading,
            "level": self.level,
        }


@dataclass
class ParsedMarkdown:
    content: str
    blocks: list[MarkdownBlock] = field(default_factory=list)
    wiki_links: list[WikiLink] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)
    note_id: str | None = None

    @property
    def links(self) -> list[WikiLink]:
        return self.wiki_links

    @property
    def front_matter(self) -> dict[str, Any]:
        return self.frontmatter

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": [block.to_dict() for block in self.blocks],
            "wiki_links": [link.to_dict() for link in self.wiki_links],
            "frontmatter": self.frontmatter,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "note_id": self.note_id,
        }


class MarkdownDecodeError(UnicodeDecodeError):
    """Retained as a named error for callers that need strict decoding."""


_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
_WIKI_LINK_RE = re.compile(r"(?<!\!)\[\[([^\]\n]+)\]\]")
_KEY_VALUE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)$")


def _frontmatter_value(raw: str) -> Any:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def _read_frontmatter(lines: list[str]) -> tuple[int, dict[str, Any], list[ParseDiagnostic]]:
    if not lines or lines[0].lstrip("\ufeff") != "---":
        return 0, {}, []

    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            values: dict[str, Any] = {}
            for line in lines[1:index]:
                match = _KEY_VALUE_RE.match(line)
                if match:
                    values[match.group(1)] = _frontmatter_value(match.group(2))
            return index + 1, values, []

    return 0, {}, [
        ParseDiagnostic(
            "unterminated_frontmatter",
            "Frontmatter opening marker has no closing marker; retained as data.",
        )
    ]


def _block_links(block: MarkdownBlock) -> list[WikiLink]:
    if block.kind in {"frontmatter", "code_fence"}:
        return []
    links: list[WikiLink] = []
    for match in _WIKI_LINK_RE.finditer(block.content):
        inner = match.group(1).strip()
        if not inner:
            continue
        target, separator, alias = inner.partition("|")
        target = target.strip()
        if not target:
            continue
        line = block.line_start + block.content[: match.start()].count("\n")
        links.append(
            WikiLink(
                target=target,
                alias=alias.strip() if separator else None,
                raw=match.group(0),
                line_start=line,
                line_end=line,
            )
        )
    return links


def parse_markdown(content: str) -> ParsedMarkdown:
    """Parse Markdown blocks without rendering or interpreting embedded code."""

    if not isinstance(content, str):
        raise TypeError("parse_markdown expects text, not bytes")

    lines = content.splitlines()
    parsed = ParsedMarkdown(content=content)
    position, parsed.frontmatter, frontmatter_diagnostics = _read_frontmatter(lines)
    parsed.diagnostics.extend(frontmatter_diagnostics)
    if position:
        parsed.blocks.append(
            MarkdownBlock(
                kind="frontmatter",
                line_start=1,
                line_end=position,
                content="\n".join(lines[:position]),
            )
        )
    raw_id = parsed.frontmatter.get("id")
    if raw_id is not None and str(raw_id).strip():
        parsed.note_id = str(raw_id).strip()

    while position < len(lines):
        if not lines[position].strip():
            position += 1
            continue

        line = lines[position]
        start = position
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            marker_char = marker[0]
            marker_length = len(marker)
            position += 1
            closed = False
            while position < len(lines):
                if re.match(
                    rf"^ {{0,3}}{re.escape(marker_char)}{{{marker_length},}}[ \t]*$",
                    lines[position],
                ):
                    closed = True
                    position += 1
                    break
                position += 1
            if not closed:
                parsed.diagnostics.append(
                    ParseDiagnostic(
                        "unterminated_fence",
                        "Code fence has no closing marker; retained as data.",
                    )
                )
            block = MarkdownBlock(
                kind="code_fence",
                line_start=start + 1,
                line_end=position if position else len(lines),
                content="\n".join(lines[start:position]),
            )
            parsed.blocks.append(block)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            heading_text = (heading.group(2) or "").strip()
            heading_text = re.sub(r"[ \t]+#+[ \t]*$", "", heading_text).strip()
            block = MarkdownBlock(
                kind="heading",
                line_start=start + 1,
                line_end=start + 1,
                content=line,
                heading=heading_text,
                level=level,
            )
            parsed.blocks.append(block)
            parsed.wiki_links.extend(_block_links(block))
            position += 1
            continue

        position += 1
        while position < len(lines) and lines[position].strip():
            if _FENCE_RE.match(lines[position]) or _HEADING_RE.match(lines[position]):
                break
            position += 1
        block = MarkdownBlock(
            kind="paragraph",
            line_start=start + 1,
            line_end=position,
            content="\n".join(lines[start:position]),
        )
        parsed.blocks.append(block)
        parsed.wiki_links.extend(_block_links(block))

    if parsed.blocks and parsed.blocks[0].kind == "frontmatter":
        # The frontmatter is a block for provenance but never a source of wiki links.
        parsed.blocks[0] = parsed.blocks[0]
    return parsed


def parse_markdown_bytes(data: bytes, *, path: str | None = None) -> ParsedMarkdown:
    """Decode UTF-8 strictly and return a diagnostic instead of guessing bytes."""

    if not isinstance(data, bytes):
        raise TypeError("parse_markdown_bytes expects bytes")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as error:
        diagnostic = ParseDiagnostic(
            "invalid_encoding",
            f"Source is not valid UTF-8: {error}",
            severity="error",
            path=path,
        )
        return ParsedMarkdown(content="", diagnostics=[diagnostic])
    parsed = parse_markdown(content)
    if path:
        parsed.diagnostics = [
            ParseDiagnostic(d.code, d.message, d.severity, path) for d in parsed.diagnostics
        ]
    return parsed
