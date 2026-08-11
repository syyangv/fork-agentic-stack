from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from harness_manager.transfer_bundle import (
    BundleSecurityError,
    apply_import_plan,
    encode_bundle,
    export_bundle,
    import_bundle,
    preflight_import,
)
from harness_manager.transfer_tui import _TargetTransaction, execute_import_transaction
from harness_manager.transfer_plan import DEFAULT_SCOPES, build_plan


ROOT = Path(__file__).resolve().parents[1]


def evidence_row(digit: str = "a", *, summary: str = "Validated symbol evidence.") -> dict:
    evidence_id = "evi_" + digit * 64
    return {
        "schema": "agentic.memory.evidence-ledger.v1",
        "evidence_id": evidence_id,
        "summary": summary,
        "provenance": {
            "kind": "crg_node",
            "provider": "crg",
            "source_id": evidence_id,
            "project_id": "1" * 16,
            "repository_revision": "2" * 40,
            "source_hash": "sha256:" + "3" * 64,
            "observed_at": "2026-07-28T12:00:00Z",
            "confidence": 0.9,
            "freshness": "fresh",
            "locator": {},
        },
        "verification": {
            "repository_reconciled": True,
            "files_reconciled": True,
            "symbols_reconciled": True,
            "executed_test": False,
        },
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def tree_snapshot(root: Path) -> dict[str, tuple[str, int, int, bytes | str | None]]:
    """Capture exact test-tree topology without following symbolic links."""
    snapshot: dict[str, tuple[str, int, int, bytes | str | None]] = {}
    for path in sorted([root, *root.rglob("*")], key=lambda item: str(item)):
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, info.st_mtime_ns, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", mode, info.st_mtime_ns, None)
        else:
            snapshot[relative] = ("file", mode, info.st_mtime_ns, path.read_bytes())
    return snapshot


class Phase9GovernanceTransferTest(unittest.TestCase):
    def run_cli(self, cwd: Path, *args: str):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["AGENTIC_STACK_ROOT"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-m", "harness_manager.cli", *args],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_agent(self, root: Path) -> Path:
        agent = root / ".agent"
        (agent / "memory" / "personal").mkdir(parents=True)
        (agent / "memory" / "semantic").mkdir(parents=True)
        (agent / "memory" / "evidence").mkdir(parents=True)
        (agent / "memory" / "personal" / "PREFERENCES.md").write_text(
            "# Preferences\n\n- Keep transfers bounded.\n", encoding="utf-8"
        )
        (agent / "memory" / "semantic" / "DECISIONS.md").write_text(
            "# Decisions\n\n- Governance stays authoritative.\n", encoding="utf-8"
        )
        write_jsonl(
            agent / "memory" / "semantic" / "lessons.jsonl",
            [
                {
                    "id": "lesson_transfer",
                    "claim": "Preflight every transfer before mutation.",
                    "conditions": ["transfer"],
                    "status": "accepted",
                },
                {
                    "id": "lesson_draft",
                    "claim": "Unreviewed draft.",
                    "conditions": [],
                    "status": "provisional",
                },
            ],
        )
        write_jsonl(agent / "memory" / "evidence" / "ledger.jsonl", [evidence_row()])

        # These exist specifically to prove the default data boundary.
        (agent / "protocols").mkdir()
        (agent / "protocols" / "permissions.md").write_text("local permissions\n")
        (agent / "runtime").mkdir()
        (agent / "runtime" / "event.json").write_text("{}\n")
        (agent / "memory" / "evidence" / "cache.sqlite3").write_bytes(b"sqlite")
        (agent / "__pycache__").mkdir()
        (agent / "__pycache__" / "cache.pyc").write_bytes(b"cache")
        (agent / "skills").mkdir()
        (agent / "skills" / "R7.md").write_text("not promoted\n")
        return agent

    def test_default_scope_is_bounded_governance_and_validated_evidence(self):
        self.assertEqual(
            DEFAULT_SCOPES,
            ("preferences", "decisions", "accepted_lessons", "evidence_ledger"),
        )
        plan = build_plan("transfer my memory into codex", ROOT)
        self.assertEqual(plan.scopes, DEFAULT_SCOPES)
        self.assertEqual(plan.sensitive_scopes, ())

        with tempfile.TemporaryDirectory() as tmp:
            bundle = export_bundle(
                self.make_agent(Path(tmp)),
                targets=["codex"],
                scopes=DEFAULT_SCOPES,
            )

        paths = {entry["path"] for entry in bundle["files"]}
        self.assertEqual(
            paths,
            {
                ".agent/memory/personal/PREFERENCES.md",
                ".agent/memory/semantic/DECISIONS.md",
            },
        )
        self.assertEqual(
            [row["evidence_id"] for row in bundle["evidence"]],
            ["evi_" + "a" * 64],
        )
        self.assertEqual(bundle["evidence"][0], evidence_row())
        self.assertEqual(
            [row["id"] for row in bundle["lessons"]],
            ["lesson_transfer"],
        )
        serialized = json.dumps(bundle)
        for forbidden in (
            "permissions.md",
            "runtime/event.json",
            "cache.sqlite3",
            "cache.pyc",
            "skills/R7.md",
            "lesson_draft",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_crg_derived_state_is_excluded_and_smuggling_fails_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self.make_agent(root / "source")
            portable_paths = (
                agent / "memory" / "working" / "paragraph.md",
                agent / "skills" / "graph-theory.md",
                agent / "skills" / "demographic-notes.md",
            )
            for path in portable_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("portable prose\n", encoding="utf-8")
            derived_paths = (
                agent / "skills" / ".code-review-graph" / "graph.db",
                agent / "skills" / ".code_review_graph" / "nodes.json",
                agent / "skills" / "codeReviewGraph" / "edges.json",
                agent / "memory" / "working" / "crg-registry.json",
                agent / "memory" / "working" / "graphCache" / "state.json",
                agent / "memory" / "working" / "graphSnapshot" / "state.json",
                agent / "memory" / "working" / "graph.snapshot.json",
                agent / "memory" / "working" / ".crg-data" / "nodes.bin",
                agent / "memory" / "working" / "uppercase" / "GRAPH.DB",
                agent / "skills" / ".CACHE" / "state.json",
                agent / "memory" / "working" / "snapshots" / "graph.duckdb",
                agent / "memory" / "working" / "nested" / "graph.db-wal",
            )
            for path in derived_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"derived")
            bundle = export_bundle(
                agent, targets=["terminal"], scopes=["skills", "working", "evidence_ledger"],
            )
            serialized = json.dumps(bundle, sort_keys=True)
            for path in derived_paths:
                self.assertNotIn(path.name, serialized)
            for path in portable_paths:
                self.assertIn(path.relative_to(agent.parent).as_posix(), serialized)
            self.assertEqual(bundle["evidence"], [evidence_row()])

            target = root / "target"
            target.mkdir()
            before = tree_snapshot(target)
            for path in (
                ".agent/skills/.code-review-graph/graph.db",
                ".agent/memory/working/crg-registry.json",
                ".agent/memory/working/nested/graph.db-wal",
                ".agent/memory/working/graph.snapshot",
                ".agent/memory/working/crg-index.json",
                ".agent/memory/working/crgNodes.jsonl",
                ".agent/memory/working/crg-wal",
                ".agent/memory/working/crg-shm",
                ".agent/memory/working/graphRegistry.json",
                ".agent/memory/working/graphSnapshot/state.json",
                ".agent/memory/working/graph.snapshot.json",
                ".agent/memory/working/.crg-data/nodes.bin",
                ".agent/memory/working/uppercase/GRAPH.DB",
                ".agent/skills/.CACHE/state.json",
            ):
                malicious = {
                    "schema_version": 1,
                    "targets": ["terminal"],
                    "scopes": ["skills", "working"],
                    "files": [{
                        "path": path, "encoding": "utf-8",
                        "content_b64": base64.b64encode(b"derived").decode(),
                    }],
                    "lessons": [], "evidence": [],
                }
                with self.assertRaisesRegex(
                    ValueError, "CRG derived|runtime/database/cache",
                ):
                    import_bundle(malicious, target)
                self.assertEqual(tree_snapshot(target), before)

    def test_evidence_export_is_bounded_to_most_recent_thousand_valid_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(Path(tmp))
            rows = [
                evidence_row(f"{index:064x}"[-1], summary=f"row {index}")
                for index in range(1001)
            ]
            # Give every row a genuinely unique ID while retaining schema shape.
            for index, row in enumerate(rows):
                evidence_id = "evi_" + f"{index:064x}"
                row["evidence_id"] = evidence_id
                row["provenance"]["source_id"] = evidence_id
            write_jsonl(agent / "memory" / "evidence" / "ledger.jsonl", rows)

            bundle = export_bundle(
                agent, targets=["terminal"], scopes=["evidence_ledger"]
            )

        self.assertEqual(len(bundle["evidence"]), 1000)
        self.assertEqual(bundle["evidence"][0]["summary"], "row 1")
        self.assertEqual(bundle["evidence"][-1]["summary"], "row 1000")

    def test_invalid_evidence_aborts_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(Path(tmp))
            invalid = evidence_row()
            invalid["provenance"]["source_id"] = "evi_" + "b" * 64
            write_jsonl(agent / "memory" / "evidence" / "ledger.jsonl", [invalid])

            with self.assertRaisesRegex(ValueError, "evidence"):
                export_bundle(
                    agent, targets=["terminal"], scopes=["evidence_ledger"]
                )

    def test_import_preflight_rejects_late_invalid_evidence_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            prefs = target / ".agent" / "memory" / "personal" / "PREFERENCES.md"
            prefs.parent.mkdir(parents=True)
            prefs.write_text("# Preferences\n\n- Existing.\n", encoding="utf-8")
            before = prefs.read_bytes()
            before_mtime = prefs.stat().st_mtime_ns
            invalid = evidence_row()
            invalid["schema"] = "unsupported"
            bundle = {
                "schema_version": 1,
                "targets": ["terminal"],
                "scopes": ["preferences", "evidence_ledger"],
                "files": [
                    {
                        "path": ".agent/memory/personal/PREFERENCES.md",
                        "encoding": "utf-8",
                        "content_b64": base64.b64encode(b"# Preferences\n\n- Imported.\n").decode(),
                    }
                ],
                "lessons": [],
                "evidence": [evidence_row(), invalid],
            }

            with self.assertRaisesRegex(ValueError, "evidence"):
                import_bundle(bundle, target)

            self.assertEqual(prefs.read_bytes(), before)
            self.assertEqual(prefs.stat().st_mtime_ns, before_mtime)
            self.assertFalse((target / ".agent" / "memory" / "evidence").exists())
            self.assertFalse((target / ".agent" / "transfer").exists())

    def test_evidence_dedupes_exact_records_and_rejects_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            ledger = target / ".agent" / "memory" / "evidence" / "ledger.jsonl"
            row = evidence_row()
            write_jsonl(ledger, [row])
            bundle = {
                "schema_version": 1,
                "targets": ["terminal"],
                "scopes": ["evidence_ledger"],
                "files": [],
                "lessons": [],
                "evidence": [row],
            }
            before = ledger.read_bytes()
            before_mtime = ledger.stat().st_mtime_ns

            duplicate = import_bundle(bundle, target)

            self.assertEqual(duplicate["evidence_imported"], 0)
            self.assertEqual(ledger.read_bytes(), before)
            self.assertEqual(ledger.stat().st_mtime_ns, before_mtime)

            conflict = evidence_row(summary="Conflicting content for immutable ID.")
            bundle["evidence"] = [conflict]
            with self.assertRaisesRegex(ValueError, "conflict"):
                import_bundle(bundle, target)
            self.assertEqual(ledger.read_bytes(), before)
            self.assertEqual(ledger.stat().st_mtime_ns, before_mtime)

    def test_late_evidence_conflict_prevents_every_planned_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            ledger = target / ".agent" / "memory" / "evidence" / "ledger.jsonl"
            existing = evidence_row()
            write_jsonl(ledger, [existing])
            prefs = target / ".agent" / "memory" / "personal" / "PREFERENCES.md"
            prefs.parent.mkdir(parents=True)
            prefs.write_text("# Preferences\n\n- Existing.\n", encoding="utf-8")
            before = {
                prefs: (prefs.read_bytes(), prefs.stat().st_mtime_ns),
                ledger: (ledger.read_bytes(), ledger.stat().st_mtime_ns),
            }
            conflict = evidence_row(summary="Same immutable ID, different row.")
            bundle = {
                "schema_version": 1,
                "targets": ["terminal"],
                "scopes": ["preferences", "evidence_ledger"],
                "files": [
                    {
                        "path": ".agent/memory/personal/PREFERENCES.md",
                        "encoding": "utf-8",
                        "content_b64": base64.b64encode(
                            b"# Preferences\n\n- Imported.\n"
                        ).decode(),
                    }
                ],
                "lessons": [],
                "evidence": [evidence_row("b"), conflict],
            }

            with self.assertRaisesRegex(ValueError, "conflict"):
                import_bundle(bundle, target)

            for path, snapshot in before.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)

    def test_accepted_lesson_id_conflict_fails_before_preferences_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            prefs = target / ".agent" / "memory" / "personal" / "PREFERENCES.md"
            prefs.parent.mkdir(parents=True)
            prefs.write_text("# Preferences\n\n- Existing.\n", encoding="utf-8")
            lessons = target / ".agent" / "memory" / "semantic" / "lessons.jsonl"
            write_jsonl(
                lessons,
                [
                    {
                        "id": "lesson_same",
                        "claim": "Existing accepted claim.",
                        "conditions": [],
                        "status": "accepted",
                    }
                ],
            )
            bundle = {
                "schema_version": 1,
                "targets": ["terminal"],
                "scopes": ["preferences", "accepted_lessons"],
                "files": [
                    {
                        "path": ".agent/memory/personal/PREFERENCES.md",
                        "encoding": "utf-8",
                        "content_b64": base64.b64encode(
                            b"# Preferences\n\n- Imported.\n"
                        ).decode(),
                    }
                ],
                "lessons": [
                    {
                        "id": "lesson_same",
                        "claim": "Conflicting accepted claim.",
                        "conditions": [],
                        "status": "accepted",
                    }
                ],
                "evidence": [],
            }
            before = prefs.read_bytes()

            with self.assertRaisesRegex(ValueError, "lesson conflict"):
                import_bundle(bundle, target)

            self.assertEqual(prefs.read_bytes(), before)

    def test_malformed_accepted_lesson_aborts_export_and_import_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_agent(Path(tmp) / "source")
            malformed = {
                "id": "lesson_bad",
                "claim": "",
                "conditions": ["transfer", 7],
                "evidence_ids": "not-a-list",
                "status": "accepted",
                "audit_extension": {"compatible": True},
            }
            write_jsonl(
                source / "memory" / "semantic" / "lessons.jsonl", [malformed]
            )
            with self.assertRaisesRegex(ValueError, "accepted lesson"):
                export_bundle(
                    source, targets=["terminal"], scopes=["accepted_lessons"]
                )

            target = Path(tmp) / "target"
            target.mkdir()
            bundle = {
                "schema_version": 1,
                "targets": ["terminal"],
                "scopes": ["preferences", "accepted_lessons"],
                "files": [
                    {
                        "path": ".agent/memory/personal/PREFERENCES.md",
                        "encoding": "utf-8",
                        "content_b64": base64.b64encode(b"# Imported\n").decode(),
                    }
                ],
                "lessons": [malformed],
                "evidence": [],
            }
            with self.assertRaisesRegex(ValueError, "accepted lesson"):
                import_bundle(bundle, target)
            self.assertEqual(list(target.iterdir()), [])

    def test_repeated_import_is_byte_and_mtime_idempotent(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            bundle = export_bundle(
                self.make_agent(Path(src_tmp)),
                targets=["terminal"],
                scopes=DEFAULT_SCOPES,
            )
            target = Path(dst_tmp)
            first = import_bundle(bundle, target)
            watched = [
                target / ".agent" / "memory" / "personal" / "PREFERENCES.md",
                target / ".agent" / "memory" / "semantic" / "DECISIONS.md",
                target / ".agent" / "memory" / "semantic" / "lessons.jsonl",
                target / ".agent" / "memory" / "semantic" / "LESSONS.md",
                target / ".agent" / "memory" / "evidence" / "ledger.jsonl",
            ]
            snapshot = {
                path: (path.read_bytes(), path.stat().st_mtime_ns) for path in watched
            }
            time.sleep(0.01)

            second = import_bundle(bundle, target)

            self.assertGreater(first["files_imported"], 0)
            self.assertEqual(second["files_imported"], 0)
            self.assertEqual(second["lessons_imported"], 0)
            self.assertEqual(second["evidence_imported"], 0)
            for path in watched:
                self.assertEqual(
                    (path.read_bytes(), path.stat().st_mtime_ns),
                    snapshot[path],
                    path,
                )
            all_files = sorted(path for path in (target / ".agent").rglob("*") if path.is_file())
            all_snapshot = {
                path: (path.read_bytes(), path.stat().st_mtime_ns) for path in all_files
            }
            time.sleep(0.01)
            import_bundle(bundle, target)
            self.assertEqual(
                {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in all_files
                },
                all_snapshot,
            )
            self.assertEqual(
                len(list((target / ".agent" / "transfer" / "imports").glob("*.json"))),
                1,
            )

    def test_permissions_runtime_databases_and_caches_are_rejected_on_import(self):
        forbidden = (
            ".agent/protocols/permissions.md",
            ".agent/runtime/process.json",
            ".agent/memory/evidence/graph.sqlite3",
            ".agent/__pycache__/module.pyc",
        )
        for relative in forbidden:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                bundle = {
                    "schema_version": 1,
                    "targets": ["terminal"],
                    "scopes": ["working"],
                    "files": [
                        {
                            "path": relative,
                            "encoding": "utf-8",
                            "content_b64": base64.b64encode(b"forbidden").decode(),
                        }
                    ],
                    "lessons": [],
                    "evidence": [],
                }
                with self.assertRaises((ValueError, BundleSecurityError)):
                    import_bundle(bundle, Path(tmp))
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_source_evidence_ledger_symlink_is_rejected(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self.make_agent(root)
            ledger = agent / "memory" / "evidence" / "ledger.jsonl"
            outside = root / "outside.jsonl"
            outside.write_bytes(ledger.read_bytes())
            ledger.unlink()
            ledger.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symbolic"):
                export_bundle(
                    agent, targets=["terminal"], scopes=["evidence_ledger"]
                )

    def test_destination_symlink_component_is_rejected_without_outside_write(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            outside = Path(tmp) / "outside"
            target.mkdir()
            outside.mkdir()
            (target / ".agent").symlink_to(outside, target_is_directory=True)
            bundle = {
                "schema_version": 1,
                "targets": ["terminal"],
                "scopes": ["preferences"],
                "files": [
                    {
                        "path": ".agent/memory/personal/PREFERENCES.md",
                        "encoding": "utf-8",
                        "content_b64": base64.b64encode(b"# Preferences\n").decode(),
                    }
                ],
                "lessons": [],
                "evidence": [],
            }

            with self.assertRaisesRegex(ValueError, "symbolic"):
                import_bundle(bundle, target)

            self.assertEqual(list(outside.iterdir()), [])

    def test_cli_invalid_evidence_fails_before_template_or_adapter_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            invalid = evidence_row()
            invalid["schema"] = "unsupported"
            payload, digest = encode_bundle(
                {
                    "schema_version": 1,
                    "targets": ["codex"],
                    "scopes": ["evidence_ledger"],
                    "files": [],
                    "lessons": [],
                    "evidence": [invalid],
                }
            )

            result = self.run_cli(
                target,
                "transfer",
                "import",
                "--payload",
                payload,
                "--sha256",
                digest,
                "--target",
                "codex",
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("evidence", result.stderr)
            self.assertEqual(list(target.iterdir()), [])

    def test_fresh_cli_crg_smuggling_fails_before_bootstrap_and_evidence_import_idempotently_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            malicious_payload, malicious_digest = encode_bundle({
                "schema_version": 1, "targets": ["codex"], "scopes": ["working"],
                "files": [{
                    "path": ".agent/memory/working/crg-index.json", "encoding": "utf-8",
                    "content_b64": base64.b64encode(b"derived").decode(),
                }], "lessons": [], "evidence": [],
            })
            rejected = self.run_cli(
                target, "transfer", "import", "--payload", malicious_payload,
                "--sha256", malicious_digest, "--target", "codex",
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("CRG derived", rejected.stderr)
            self.assertEqual(list(target.iterdir()), [])

            evidence_payload, evidence_digest = encode_bundle({
                "schema_version": 1, "targets": ["terminal"], "scopes": ["evidence_ledger"],
                "files": [], "lessons": [], "evidence": [evidence_row()],
            })
            first = self.run_cli(
                target, "transfer", "import", "--payload", evidence_payload,
                "--sha256", evidence_digest, "--target", "terminal",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("CRG next action:", first.stdout)
            before = tree_snapshot(target)
            second = self.run_cli(
                target, "transfer", "import", "--payload", evidence_payload,
                "--sha256", evidence_digest, "--target", "terminal",
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(tree_snapshot(target), before)

    def test_wizard_apply_surfaces_structured_crg_next_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.make_agent(target)
            lines: list[str] = []
            ui = types.SimpleNamespace(
                intro=lambda _title: None,
                note=lambda _title, _lines: None,
                print_banner=lambda: None,
                outro=lambda output: lines.extend(output),
            )
            widgets = types.SimpleNamespace(
                ask_confirm=lambda *_args, **_kwargs: False if "behavioral" in _args[0].casefold() else True,
                ask_text=lambda *_args, **_kwargs: "move memory into terminal",
                ask_multiselect=lambda prompt, choices, **_kwargs: ["terminal"] if "targets" in prompt else ["evidence_ledger"],
                ask_select=lambda *_args, **_kwargs: "Apply here now",
            )
            result = {
                "files_imported": 0, "lessons_imported": 0,
                "crg_next_action": "fixture rebuild instruction",
            }
            with (patch.dict(sys.modules, {"onboard_ui": ui, "onboard_widgets": widgets}),
                  patch("harness_manager.transfer_tui.execute_import_transaction", return_value=(result, []))):
                from harness_manager.transfer_tui import run_wizard
                self.assertEqual(run_wizard(target, ROOT), 0)
            self.assertIn(
                "CRG next action: fixture rebuild instruction",
                lines,
            )

    def test_fresh_cli_import_excludes_transfer_forbidden_source_artifacts(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            source_agent = self.make_agent(Path(src_tmp))
            payload, digest = encode_bundle(
                export_bundle(
                    source_agent,
                    targets=["codex"],
                    scopes=DEFAULT_SCOPES,
                )
            )
            target = Path(dst_tmp)

            result = self.run_cli(
                target,
                "transfer",
                "import",
                "--payload",
                payload,
                "--sha256",
                digest,
                "--target",
                "codex",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            agent = target / ".agent"
            self.assertEqual(
                (agent / "protocols" / "permissions.md").read_bytes(),
                (ROOT / ".agent" / "protocols" / "permissions.md").read_bytes(),
            )
            self.assertFalse((agent / "runtime").exists())
            self.assertFalse(any(agent.rglob("*.sqlite")))
            self.assertFalse(any(agent.rglob("*.sqlite3")))
            self.assertFalse(any(agent.rglob("*.db")))
            self.assertFalse(any(agent.rglob("*.pyc")))
            state = json.loads((agent / "install.json").read_text())
            self.assertEqual(state["orchestration"]["profile"], "standard")
            self.assertEqual(
                state["orchestration"]["architecture"], "governed-memory-code-evidence"
            )
            config = json.loads(
                (agent / "memory" / "orchestration" / "config.json").read_text()
            )
            self.assertEqual(config["architecture"], "governed-memory-code-evidence")

    def test_evidence_only_fresh_bootstrap_has_canonical_empty_governance_scaffolding(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            source = self.make_agent(Path(src_tmp))
            bundle = export_bundle(
                source, targets=["terminal"], scopes=["evidence_ledger"]
            )
            target = Path(dst_tmp)

            execute_import_transaction(bundle, target, ROOT)
            agent = target / ".agent"
            self.assertEqual(
                (agent / "protocols" / "permissions.md").read_bytes(),
                (ROOT / ".agent" / "protocols" / "permissions.md").read_bytes(),
            )
            self.assertEqual(
                (agent / "memory" / "personal" / "PREFERENCES.md").read_text(),
                "# Preferences\n\n",
            )
            self.assertEqual(
                (agent / "memory" / "semantic" / "DECISIONS.md").read_text(),
                "# Decisions\n\n",
            )
            self.assertEqual(
                (agent / "memory" / "semantic" / "lessons.jsonl").read_bytes(), b"",
            )
            self.assertIn(
                "## Auto-promoted entries will be appended below",
                (agent / "memory" / "semantic" / "LESSONS.md").read_text(),
            )
            self.assertNotIn(
                "Keep transfers bounded",
                (agent / "memory" / "personal" / "PREFERENCES.md").read_text(),
            )
            before = tree_snapshot(target)
            time.sleep(0.01)
            execute_import_transaction(bundle, target, ROOT)
            self.assertEqual(tree_snapshot(target), before)

    def test_fresh_bootstrap_keeps_trusted_permissions_not_bundle_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            source_agent = self.make_agent(Path(tmp) / "source")
            bundle = export_bundle(
                source_agent, targets=["terminal"], scopes=DEFAULT_SCOPES
            )
            trusted = (ROOT / ".agent" / "protocols" / "permissions.md").read_bytes()

            execute_import_transaction(bundle, target, ROOT)

            installed = target / ".agent" / "protocols" / "permissions.md"
            self.assertEqual(installed.read_bytes(), trusted)
            before = tree_snapshot(target)
            bundle["files"].append(
                {
                    "path": ".agent/protocols/permissions.md",
                    "encoding": "utf-8",
                    "content_b64": base64.b64encode(b"bundle policy override").decode(),
                }
            )
            bundle["scopes"].append("working")

            with self.assertRaisesRegex(ValueError, "permissions"):
                execute_import_transaction(bundle, target, ROOT)

            self.assertEqual(tree_snapshot(target), before)

    def test_import_identity_excludes_only_volatile_created_at(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            source = self.make_agent(Path(src_tmp))
            with patch(
                "harness_manager.transfer_bundle.now_iso",
                side_effect=["2026-07-28T00:00:00Z", "2026-07-29T00:00:00Z"],
            ):
                first_bundle = export_bundle(
                    source, targets=["terminal"], scopes=DEFAULT_SCOPES
                )
                second_bundle = export_bundle(
                    source, targets=["terminal"], scopes=DEFAULT_SCOPES
                )
            self.assertNotEqual(first_bundle["created_at"], second_bundle["created_at"])
            target = Path(dst_tmp)

            import_bundle(first_bundle, target)
            before = tree_snapshot(target)
            time.sleep(0.01)
            second = import_bundle(second_bundle, target)

            self.assertEqual(second["files_imported"], 0)
            self.assertEqual(second["lessons_imported"], 0)
            self.assertEqual(second["evidence_imported"], 0)
            self.assertEqual(tree_snapshot(target), before)
            self.assertEqual(
                len(list((target / ".agent" / "transfer" / "imports").glob("*.json"))),
                1,
            )

    def test_malformed_old_ledger_row_aborts_before_newest_thousand_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(Path(tmp))
            valid_rows = []
            for index in range(1000):
                row = evidence_row("a", summary=f"newest {index}")
                evidence_id = "evi_" + f"{index:064x}"
                row["evidence_id"] = evidence_id
                row["provenance"]["source_id"] = evidence_id
                valid_rows.append(row)
            malformed = evidence_row("b")
            malformed["summary"] = ""
            write_jsonl(
                agent / "memory" / "evidence" / "ledger.jsonl",
                [malformed, *valid_rows],
            )

            with self.assertRaisesRegex(ValueError, "evidence ledger line 1"):
                export_bundle(
                    agent, targets=["terminal"], scopes=["evidence_ledger"]
                )

    def test_transaction_rolls_back_fresh_bootstrap_adapter_and_data_failures(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            bundle = export_bundle(
                self.make_agent(Path(src_tmp)),
                targets=["codex"],
                scopes=DEFAULT_SCOPES,
            )
            for phase in (
                "after_bootstrap",
                "after_adapters",
                "before_import_replace",
                "after_import_replace",
            ):
                with self.subTest(phase=phase):
                    target = Path(dst_tmp) / phase
                    target.mkdir()
                    (target / "existing.txt").write_text("unchanged\n")
                    before = tree_snapshot(target)

                    def fail(current: str) -> None:
                        if current == phase:
                            raise OSError(f"injected {phase}")

                    with self.assertRaisesRegex(OSError, phase):
                        execute_import_transaction(bundle, target, ROOT, fault=fail)
                    self.assertEqual(tree_snapshot(target), before, phase)

    def test_terminal_adapter_agreements_and_metadata_rollback_exactly(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            bundle = export_bundle(
                self.make_agent(Path(src_tmp)),
                targets=["terminal"],
                scopes=DEFAULT_SCOPES,
            )
            target = Path(dst_tmp)
            agents = target / "AGENTS.md"
            agents.write_text("# Existing terminal contract\n", encoding="utf-8")
            os.chmod(agents, 0o640)
            before = tree_snapshot(target)

            with self.assertRaisesRegex(OSError, "after_adapters"):
                execute_import_transaction(
                    bundle,
                    target,
                    ROOT,
                    fault=lambda phase: (
                        (_ for _ in ()).throw(OSError("after_adapters"))
                        if phase == "after_adapters" else None
                    ),
                )

            self.assertEqual(tree_snapshot(target), before)

    def test_stale_plan_and_symlink_swap_fail_without_outside_write(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            source = self.make_agent(Path(src_tmp))
            bundle = export_bundle(
                source, targets=["terminal"], scopes=DEFAULT_SCOPES
            )
            target = Path(dst_tmp) / "target"
            target.mkdir()
            prefs = target / ".agent" / "memory" / "personal" / "PREFERENCES.md"
            prefs.parent.mkdir(parents=True)
            prefs.write_text("# Existing\n", encoding="utf-8")
            from harness_manager.transfer_bundle import apply_import_plan, preflight_import

            plan = preflight_import(bundle, target)
            prefs.write_text("# Concurrent edit\n", encoding="utf-8")
            before_stale = tree_snapshot(target)
            with self.assertRaisesRegex(ValueError, "stale"):
                apply_import_plan(plan)
            self.assertEqual(tree_snapshot(target), before_stale)

            plan = preflight_import(bundle, target)
            outside = Path(dst_tmp) / "outside"
            outside.mkdir()
            (target / ".agent" / "memory").rename(target / "memory-original")
            (target / ".agent" / "memory").symlink_to(outside, target_is_directory=True)
            before_swap = tree_snapshot(target)
            with self.assertRaisesRegex(ValueError, "stale|symbolic"):
                apply_import_plan(plan)
            self.assertEqual(tree_snapshot(target), before_swap)
            self.assertEqual(list(outside.iterdir()), [])

    def test_target_root_symlink_is_rejected_before_any_import_mutation(self):
        """The transaction root itself is a trust boundary, not a descendant."""
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            bundle = export_bundle(
                self.make_agent(Path(src_tmp)),
                targets=["terminal"],
                scopes=DEFAULT_SCOPES,
            )
            outside = Path(dst_tmp) / "outside"
            outside.mkdir()
            root_link = Path(dst_tmp) / "target-link"
            root_link.symlink_to(outside, target_is_directory=True)
            before = tree_snapshot(outside)

            with self.assertRaisesRegex(ValueError, "target root.*symbolic|symbolic.*target root"):
                execute_import_transaction(bundle, root_link, ROOT)

            self.assertEqual(tree_snapshot(outside), before)

    def test_target_root_must_be_a_real_directory_before_preflight(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            bundle = export_bundle(
                self.make_agent(Path(src_tmp)), targets=["terminal"], scopes=DEFAULT_SCOPES
            )
            target_file = Path(dst_tmp) / "not-a-directory"
            target_file.write_text("sentinel", encoding="utf-8")
            before = target_file.read_bytes(), target_file.stat().st_mtime_ns

            with self.assertRaisesRegex(ValueError, "target root.*directory"):
                execute_import_transaction(bundle, target_file, ROOT)

            self.assertEqual((target_file.read_bytes(), target_file.stat().st_mtime_ns), before)

    def test_direct_import_plan_rollback_restores_file_metadata_and_created_topology(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            bundle = export_bundle(
                self.make_agent(Path(src_tmp)), targets=["terminal"], scopes=DEFAULT_SCOPES
            )
            target = Path(dst_tmp)
            preferences = target / ".agent" / "memory" / "personal" / "PREFERENCES.md"
            preferences.parent.mkdir(parents=True)
            preferences.write_text("# Existing\n", encoding="utf-8")
            os.chmod(preferences, 0o640)
            original_ns = 1_700_000_000_123_456_789
            os.utime(preferences, ns=(original_ns, original_ns))
            before = tree_snapshot(target)
            plan = preflight_import(bundle, target)
            calls = 0

            def fail(phase: str) -> None:
                nonlocal calls
                if phase == "after_import_replace":
                    calls += 1
                    if calls == 2:
                        raise OSError("mid-plan replacement")

            with self.assertRaisesRegex(OSError, "mid-plan"):
                apply_import_plan(plan, fault=fail)
            after = tree_snapshot(target)
            self.assertEqual(after[preferences.relative_to(target).as_posix()], before[preferences.relative_to(target).as_posix()])
            self.assertFalse((target / ".agent" / "transfer").exists())

    def test_snapshot_entry_bound_counts_missing_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {Path(f"missing-{index}") for index in range(1_001)}
            with self.assertRaisesRegex(ValueError, "entry bound"):
                _TargetTransaction.capture(root, paths)

    def test_transaction_detects_concurrent_edit_before_adapter_and_preserves_it(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            bundle = export_bundle(
                self.make_agent(Path(src_tmp)), targets=["terminal"], scopes=DEFAULT_SCOPES
            )
            target = Path(dst_tmp)
            before = tree_snapshot(target)

            def concurrent_edit(phase: str) -> None:
                if phase == "after_bootstrap":
                    (target / "AGENTS.md").write_text("# concurrent\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "changed concurrently"):
                execute_import_transaction(bundle, target, ROOT, fault=concurrent_edit)
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), "# concurrent\n")
            self.assertFalse((target / ".agent").exists())
            self.assertEqual(set(tree_snapshot(target)), {".", "AGENTS.md"})

    def test_outer_transaction_rollback_never_follows_swapped_parent_symlink(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            bundle = export_bundle(
                self.make_agent(Path(src_tmp)), targets=["terminal"], scopes=DEFAULT_SCOPES
            )
            target = Path(dst_tmp) / "target"
            target.mkdir()
            outside = Path(dst_tmp) / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside must remain unchanged", encoding="utf-8")
            outside_before = tree_snapshot(outside)

            def swap_then_fail(phase: str) -> None:
                if phase == "after_adapters":
                    memory = target / ".agent" / "memory"
                    memory.rename(target / ".agent" / "memory-original")
                    memory.symlink_to(outside, target_is_directory=True)
                    raise OSError("force descriptor-rooted rollback")

            with self.assertRaisesRegex(OSError, "force descriptor-rooted rollback"):
                execute_import_transaction(bundle, target, ROOT, fault=swap_then_fail)
            self.assertEqual(tree_snapshot(outside), outside_before)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside must remain unchanged")


if __name__ == "__main__":
    unittest.main()
