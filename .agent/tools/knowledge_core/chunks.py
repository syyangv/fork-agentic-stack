"""Bounded, provenance-preserving chunks derived from parsed Markdown."""

from __future__ import annotations

from dataclasses import dataclass

from .parser import MarkdownBlock, ParsedMarkdown


DEFAULT_MAX_CHARS = 2_000


@dataclass(frozen=True)
class MarkdownChunk:
    ordinal: int
    kind: str
    content: str
    heading: str | None
    line_start: int
    line_end: int

    @property
    def text(self) -> str:
        return self.content


def _slice_lines(block: MarkdownBlock, start: int, end: int) -> tuple[int, int]:
    line_start = block.line_start + block.content[:start].count("\n")
    line_end = block.line_start + block.content[:end].count("\n")
    line_end = max(line_start, min(block.line_end, line_end))
    return line_start, line_end


def _split_block(block: MarkdownBlock, max_chars: int) -> list[tuple[str, int, int]]:
    if not block.content:
        return []
    pieces: list[tuple[str, int, int]] = []
    for start in range(0, len(block.content), max_chars):
        end = min(len(block.content), start + max_chars)
        line_start, line_end = _slice_lines(block, start, end)
        pieces.append((block.content[start:end], line_start, line_end))
    return pieces


def chunk_markdown(parsed: ParsedMarkdown, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[MarkdownChunk]:
    """Chunk each semantic block independently; fences cannot consume later text."""

    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    result: list[MarkdownChunk] = []
    heading_stack: list[tuple[int, str]] = []
    ordinal = 0
    for block in parsed.blocks:
        if block.kind == "heading":
            assert block.heading is not None
            heading_stack = [(level, text) for level, text in heading_stack if level < (block.level or 1)]
            heading_stack.append((block.level or 1, block.heading))
            heading_context = block.heading
        else:
            heading_context = heading_stack[-1][1] if heading_stack else None

        for content, line_start, line_end in _split_block(block, max_chars):
            result.append(
                MarkdownChunk(
                    ordinal=ordinal,
                    kind=block.kind,
                    content=content,
                    heading=heading_context,
                    line_start=line_start,
                    line_end=line_end,
                )
            )
            ordinal += 1
    return result
