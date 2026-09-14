import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".agent" / "tools" / "knowledge.py"


class TestKnowledgeIndexCLI(unittest.TestCase):
    def test_index_is_explicit_and_writes_only_derived_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            (vault / "10-Projects").mkdir(parents=True)
            for directory in ("20-Research", "30-Decisions", "40-Lessons"):
                (vault / directory).mkdir()
            database = root / "runtime" / "index.sqlite3"
            note = vault / "10-Projects" / "cli.md"
            note.write_text("# CLI\n\nexplicit index command\n", encoding="utf-8")
            before = note.read_bytes()

            command = [
                sys.executable,
                str(CLI),
                "index",
                "--work-vault",
                str(vault),
                "--db-path",
                str(database),
            ]
            first = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["added"], 1)
            self.assertEqual(note.read_bytes(), before)

            second = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["unchanged"], 1)


    def test_p3_search_read_and_context_are_structured(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            for directory in ("10-Projects", "20-Research", "30-Decisions", "40-Lessons"):
                (vault / directory).mkdir(parents=True, exist_ok=True)
            database = root / "runtime" / "index.sqlite3"
            note = vault / "10-Projects" / "cli-p3.md"
            note.write_text("---\ntitle: P3 CLI\n---\n\ncli retrieval marker\n", encoding="utf-8")
            index_command = [
                sys.executable,
                str(CLI),
                "index",
                "--work-vault",
                str(vault),
                "--db-path",
                str(database),
            ]
            indexed = subprocess.run(index_command, capture_output=True, text=True, check=False)
            self.assertEqual(indexed.returncode, 0, indexed.stderr)

            search_command = [
                sys.executable,
                str(CLI),
                "search",
                "--work-vault",
                str(vault),
                "--db-path",
                str(database),
                "--query",
                "cli retrieval marker",
            ]
            searched = subprocess.run(search_command, capture_output=True, text=True, check=False)
            self.assertEqual(searched.returncode, 0, searched.stderr)
            search_payload = json.loads(searched.stdout)
            self.assertEqual(search_payload["status"], "ok")
            source_id = search_payload["results"][0]["source_id"]
            self.assertIn("content_hash", search_payload["results"][0]["source_ref"])

            read_command = [
                sys.executable,
                str(CLI),
                "read_source",
                source_id,
                "--task-id",
                "cli-task",
                "--work-vault",
                str(vault),
                "--db-path",
                str(database),
            ]
            read = subprocess.run(read_command, capture_output=True, text=True, check=False)
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertIn("cli retrieval marker", json.loads(read.stdout)["content"])

            context_command = [
                sys.executable,
                str(CLI),
                "build_context",
                "--task-id",
                "cli-task",
                "--query",
                "cli retrieval marker",
                "--work-vault",
                str(vault),
                "--db-path",
                str(database),
            ]
            context = subprocess.run(context_command, capture_output=True, text=True, check=False)
            self.assertEqual(context.returncode, 0, context.stderr)
            context_payload = json.loads(context.stdout)
            self.assertEqual(context_payload["status"], "ok")
            self.assertLessEqual(context_payload["char_count"], 12_000)

    def test_human_review_cli_requires_and_applies_an_explicit_formal_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            for directory in ("00-Inbox", "10-Projects", "20-Research", "30-Decisions", "40-Lessons"):
                (vault / directory).mkdir(parents=True, exist_ok=True)
            draft_store = root / "runtime" / "drafts.sqlite3"
            journal = root / "runtime" / "apply.jsonl"
            database = root / "runtime" / "index.sqlite3"
            source = {
                "source_id": "source_cli_t18",
                "vault_id": "work",
                "note_id": "note_cli_t18",
                "path": "10-Projects/source.md",
                "heading": "CLI",
                "line_start": 1,
                "line_end": 2,
                "content_hash": "a" * 64,
            }
            candidate = {
                "summary": "CLI target selection",
                "candidate_notes": ["CLI formal target marker"],
                "source_refs": [source],
                "validation_evidence": ["synthetic"],
                "uncertainties": [],
            }
            result = "<knowledge-candidate>" + json.dumps(candidate) + "</knowledge-candidate>"
            proposed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "propose_note",
                    "--task-id",
                    "cli-t18-task",
                    "--turn-id",
                    "cli-t18-turn",
                    "--task-status",
                    "completed",
                    "--draft-store",
                    str(draft_store),
                    "--draft-vault",
                    str(vault),
                ],
                input=json.dumps({"result": result, "turn_status": "completed", "source_manifest": [source]}),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            draft = json.loads(proposed.stdout)["draft"]
            target = "10-Projects/cli-approved.md"
            applied = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "review_apply",
                    "--work-vault",
                    str(vault),
                    "--draft-store",
                    str(draft_store),
                    "--journal-path",
                    str(journal),
                    "--db-path",
                    str(database),
                    "--draft-id",
                    draft["draft_id"],
                    "--draft-hash",
                    draft["draft_hash"],
                    "--target-path",
                    target,
                    "--human-approved",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            applied_payload = json.loads(applied.stdout)
            self.assertEqual(applied_payload["target_path"], target)
            self.assertTrue((vault / target).is_file())
            self.assertTrue((vault / draft["target_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
