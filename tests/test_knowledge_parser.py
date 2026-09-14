import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".agent" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


class TestKnowledgeMarkdownParser(unittest.TestCase):
    def test_conservative_blocks_and_wiki_links_keep_line_ranges(self):
        from knowledge_core.parser import parse_markdown

        content = (
            "---\n"
            "id: k-parser-001\n"
            "title: Parser example\n"
            "---\n"
            "# Overview\n"
            "A paragraph with [[Design Note|the design]] and [[20-Research/中文]].\n"
            "\n"
            "```python\n"
            "print('do not execute')\n"
            "[[not-a-link]]\n"
            "```\n"
            "\n"
            "## Follow-up\n"
            "Second paragraph.\n"
        )

        parsed = parse_markdown(content)
        self.assertEqual(parsed.note_id, "k-parser-001")
        self.assertEqual(
            [block.kind for block in parsed.blocks],
            ["frontmatter", "heading", "paragraph", "code_fence", "heading", "paragraph"],
        )
        self.assertEqual((parsed.blocks[0].line_start, parsed.blocks[0].line_end), (1, 4))
        self.assertEqual((parsed.blocks[1].line_start, parsed.blocks[1].line_end), (5, 5))
        self.assertEqual((parsed.blocks[3].line_start, parsed.blocks[3].line_end), (8, 11))
        self.assertEqual(parsed.blocks[1].level, 1)
        self.assertEqual(parsed.blocks[1].heading, "Overview")
        self.assertEqual([link.target for link in parsed.wiki_links], ["Design Note", "20-Research/中文"])
        self.assertEqual(parsed.wiki_links[0].alias, "the design")
        self.assertEqual(parsed.wiki_links[0].line_start, 6)
        self.assertNotIn("not-a-link", [link.target for link in parsed.wiki_links])

    def test_unterminated_fence_is_data_not_executable_code(self):
        from knowledge_core.parser import parse_markdown

        parsed = parse_markdown("# Safe\n\n```sh\nrm -rf /\n")
        self.assertEqual(parsed.blocks[-1].kind, "code_fence")
        self.assertEqual((parsed.blocks[-1].line_start, parsed.blocks[-1].line_end), (3, 4))
        self.assertTrue(any(d.code == "unterminated_fence" for d in parsed.diagnostics))
        self.assertEqual(parsed.wiki_links, [])

    def test_chunking_is_bounded_and_provenance_is_preserved(self):
        from knowledge_core.chunks import chunk_markdown
        from knowledge_core.parser import parse_markdown

        content = "# Long\n\n" + ("中英 mixed text " * 120) + "\n\n```text\n" + ("x" * 120) + "\n```\n"
        chunks = chunk_markdown(parse_markdown(content), max_chars=80)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(0 < len(chunk.content) <= 80 for chunk in chunks))
        self.assertTrue(all(chunk.line_start <= chunk.line_end for chunk in chunks))
        self.assertTrue(any(chunk.kind == "code_fence" for chunk in chunks))
        self.assertEqual(chunks[0].heading, "Long")

    def test_empty_and_multilingual_documents_are_valid(self):
        from knowledge_core.parser import parse_markdown

        empty = parse_markdown("")
        self.assertEqual(empty.blocks, [])
        self.assertEqual(empty.wiki_links, [])

        multilingual = parse_markdown("# 日本語\n\n中文知识 and English\n")
        self.assertEqual(multilingual.blocks[0].heading, "日本語")
        self.assertIn("中文知识", multilingual.blocks[1].content)


if __name__ == "__main__":
    unittest.main()
