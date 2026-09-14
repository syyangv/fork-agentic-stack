#!/usr/bin/env python3
"""
Agent Knowledge Vault CLI.
Provides init and status subcommands for Phase P1.
Follows versioned JSON stdout convention; diagnostics written to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add tools directory to path if needed
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from knowledge_core.config import (
    KnowledgeConfig,
    FORMAL_DIR_ALLOWLIST,
    ALL_DEFAULT_DIRS,
)
from knowledge_core.bootstrap import init_work_vault
from knowledge_core.index import DuplicateNoteIDError, KnowledgeIndex, KnowledgeIndexError
from knowledge_core.context import ContextBuilder
from knowledge_core.capture import capture_result
from knowledge_core.drafts import DraftStore, _is_protected_runtime_path
from knowledge_core.approval import ApprovalApplier, ApprovalError
from knowledge_core.provenance import ProvenanceError, SourceReader
from knowledge_core.retrieval import KnowledgeRetriever


def cmd_status(args: argparse.Namespace) -> int:
    config = KnowledgeConfig.create(
        work_vault_path=args.work_vault,
        personal_vault_path=args.personal_vault,
        runtime_dir=args.runtime_dir,
    )

    wv = config.work_vault
    pv = config.personal_vault
    rt = config.runtime_dir

    wv_exists = wv.path.is_dir()
    pv_exists = pv.path.is_dir()
    rt_exists = rt.is_dir()

    # Check formal directories in work vault if work vault exists
    formal_status: dict[str, bool] = {}
    if wv_exists:
        for d in FORMAL_DIR_ALLOWLIST:
            formal_status[d] = (wv.path / d).is_dir()

    is_ready = wv_exists and all(formal_status.values()) and (wv.path / "Home.md").is_file()

    payload = {
        "protocol_version": config.protocol_version,
        "status": "ready" if is_ready else "uninitialized",
        "work_vault": {
            "vault_id": wv.vault_id,
            "path": str(wv.path),
            "exists": wv_exists,
            "formal_dirs": list(FORMAL_DIR_ALLOWLIST),
            "formal_dirs_present": formal_status,
        },
        "personal_vault": {
            "vault_id": pv.vault_id,
            "path": str(pv.path),
            "exists": pv_exists,
            "indexed": pv.indexed,
            "grant_required": True,
            "note": "Personal vault is not indexed. Access requires process-scoped task authorization.",
        },
        "runtime_dir": {
            "path": str(rt),
            "exists": rt_exists,
        },
    }

    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    config = KnowledgeConfig.create(
        work_vault_path=args.work_vault,
        runtime_dir=args.runtime_dir,
    )

    wv_path = config.work_vault.path
    rt_path = config.runtime_dir

    allow_nonempty = getattr(args, "allow_nonempty", False)

    try:
        result = init_work_vault(wv_path, runtime_dir=rt_path, allow_nonempty=allow_nonempty)
        result["protocol_version"] = config.protocol_version
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return 0
    except FileExistsError as e:
        sys.stderr.write(f"Refused: {e}\n")
        err_payload = {
            "success": False,
            "error": str(e),
            "refused": True,
            "vault_path": str(wv_path),
            "protocol_version": config.protocol_version,
        }
        sys.stdout.write(json.dumps(err_payload, indent=2) + "\n")
        return 1


def cmd_index(args: argparse.Namespace) -> int:
    """Explicitly refresh only the registered work vault's derived index."""

    config = KnowledgeConfig.create(
        work_vault_path=args.work_vault,
        runtime_dir=args.runtime_dir,
    )
    db_path = Path(args.db_path) if args.db_path else config.runtime_dir / "index.sqlite3"
    index = KnowledgeIndex(db_path)
    try:
        result = index.sync(config.work_vault, rebuild=args.rebuild)
    except (DuplicateNoteIDError, KnowledgeIndexError, OSError) as error:
        payload = {
            "success": False,
            "protocol_version": config.protocol_version,
            "error": type(error).__name__,
            "message": str(error),
        }
        sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return 1
    sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
    for diagnostic in result.diagnostics:
        sys.stderr.write(json.dumps(diagnostic.to_dict(), ensure_ascii=False) + "\n")
    return 0


def _index_for_args(args: argparse.Namespace):
    config = KnowledgeConfig.create(
        work_vault_path=args.work_vault,
        runtime_dir=args.runtime_dir,
    )
    db_path = Path(args.db_path) if args.db_path else config.runtime_dir / "index.sqlite3"
    return config, KnowledgeIndex(db_path)


def cmd_search(args: argparse.Namespace) -> int:
    """Search only the registered work vault; CLI invocations cannot grant personal access."""
    config, index = _index_for_args(args)
    query = args.query_option if args.query_option is not None else args.query
    if query is None:
        sys.stderr.write("search requires a query\n")
        return 2
    response = KnowledgeRetriever(index, work_vault=config.work_vault).search(
        query,
        task_id=args.task_id,
        project=args.project,
        limit=args.limit,
        include_related=args.include_related,
    )
    payload = response.to_dict()
    payload["protocol_version"] = config.protocol_version
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0 if response.ok else 1


def cmd_read_source(args: argparse.Namespace) -> int:
    config, index = _index_for_args(args)
    reader = SourceReader(index, work_vault=config.work_vault)
    try:
        read = reader.read_source(
            args.source_id,
            task_id=args.task_id,
            line_start=args.line_start,
            line_end=args.line_end,
            expected_hash=args.expected_hash,
            max_chars=args.max_chars,
        )
    except (ProvenanceError, OSError, ValueError) as error:
        payload = {
            "success": False,
            "protocol_version": config.protocol_version,
            "error": type(error).__name__,
            "message": str(error),
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1
    payload = read.to_dict()
    payload["success"] = True
    payload["protocol_version"] = config.protocol_version
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def cmd_build_context(args: argparse.Namespace) -> int:
    config, index = _index_for_args(args)
    retriever = KnowledgeRetriever(index, work_vault=config.work_vault)
    builder = ContextBuilder(retriever, SourceReader(index, work_vault=config.work_vault))
    bundle = builder.build_context(
        args.task_id,
        args.query,
        project=args.project,
        limit=args.limit,
        max_snippets=args.max_snippets,
        max_chars=args.max_chars,
        exclude_source_ids=args.exclude_source_id,
        include_related=args.include_related,
    )
    payload = bundle.to_dict()
    payload["protocol_version"] = config.protocol_version
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0 if bundle.status in {"ok", "no_results"} else 1


def cmd_propose_note(args: argparse.Namespace) -> int:
    """Capture one completed result into an explicit private draft store."""
    draft_vault = None
    if args.draft_vault:
        draft_vault = KnowledgeConfig.create(work_vault_path=args.draft_vault).work_vault
    try:
        envelope = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        payload = {
            "success": False,
            "capture_status": "capture_incomplete",
            "diagnostics": [{"code": "input_json_invalid", "message": "Input must be a JSON object."}],
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1
    if not isinstance(envelope, dict):
        payload = {
            "success": False,
            "capture_status": "capture_incomplete",
            "diagnostics": [{"code": "input_schema_invalid", "message": "Input must be a JSON object."}],
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1

    result_text = envelope.get("result")
    if result_text is None:
        result_text = envelope.get("output")
    if not isinstance(result_text, str):
        payload = {
            "success": False,
            "capture_status": "capture_incomplete",
            "diagnostics": [{"code": "result_not_text", "message": "Input must contain a text result or output field."}],
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1

    try:
        with DraftStore(
            args.draft_store,
            allow_protected_runtime=args.allow_protected_runtime,
        ) as draft_store:
            result = capture_result(
                args.task_id,
                args.turn_id,
                result_text,
                task_status=args.task_status,
                turn_status=envelope.get("turn_status"),
                source_manifest=envelope.get("source_manifest", []),
                personal_context_used=envelope.get("personal_context_used") is True,
                persistence_decision=envelope.get("persistence_decision"),
                draft_store=draft_store,
                draft_vault=draft_vault,
            )
            payload = result.to_dict()
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        # Do not echo the exception: paths and input content are not diagnostics.
        payload = {
            "success": False,
            "capture_status": "capture_incomplete",
            "diagnostics": [{"code": "draft_store_unavailable", "message": "Private draft storage is unavailable."}],
        }
        sys.stderr.write(f"propose_note: {type(error).__name__}\n")
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0 if result.capture_status in {"draft_created", "duplicate", "quarantined"} else 1


def _review_envelope() -> dict[str, object]:
    """Read only a bounded human-review envelope from stdin when supplied."""

    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("Review input must be a JSON object") from error
    if not isinstance(envelope, dict):
        raise ValueError("Review input must be a JSON object")
    return envelope


def _review_applier(
    args: argparse.Namespace,
    *,
    human_gate: bool = False,
) -> tuple[KnowledgeConfig, DraftStore, ApprovalApplier]:
    if not args.work_vault or not args.draft_store or not args.journal_path:
        raise ValueError("review commands require explicit work-vault, draft-store, and journal-path")
    config = KnowledgeConfig.create(work_vault_path=args.work_vault)
    store = DraftStore(
        args.draft_store,
        allow_protected_runtime=human_gate
        and _is_protected_runtime_path(args.draft_store),
    )
    index = KnowledgeIndex(args.db_path) if args.db_path else None
    return config, store, ApprovalApplier(
        config.work_vault,
        store,
        args.journal_path,
        index=index,
    )


def _review_error(config: KnowledgeConfig | None, error: Exception) -> dict[str, object]:
    return {
        "success": False,
        "protocol_version": config.protocol_version if config is not None else "knowledge-v1",
        "error": getattr(error, "code", type(error).__name__),
        "message": str(error),
    }


def cmd_review_apply(args: argparse.Namespace) -> int:
    config: KnowledgeConfig | None = None
    store: DraftStore | None = None
    try:
        if not args.human_approved:
            raise ApprovalError("review_apply requires the explicit --human-approved human boundary")
        envelope = _review_envelope()
        config, store, applier = _review_applier(
            args,
            human_gate=args.human_approved,
        )
        draft_id = args.draft_id or envelope.get("draft_id")
        draft_hash = args.draft_hash or envelope.get("draft_hash")
        if not isinstance(draft_id, str) or not isinstance(draft_hash, str):
            raise ValueError("review_apply requires draft-id and draft-hash")
        target_hash = args.target_hash if args.target_hash is not None else envelope.get("target_hash")
        content = envelope.get("content", envelope.get("new_content"))
        if args.content_file:
            content = Path(args.content_file).read_text(encoding="utf-8")
        approval = envelope.get("human_approval")
        selected_target_path = args.target_path
        if selected_target_path is None and isinstance(envelope.get("target_path"), str):
            selected_target_path = envelope["target_path"]
        if selected_target_path is None and isinstance(approval, dict) and isinstance(approval.get("target_path"), str):
            selected_target_path = approval["target_path"]
        if isinstance(approval, dict):
            approval = dict(approval)
            if selected_target_path is not None:
                approval.setdefault("target_path", selected_target_path)
        else:
            approval = {
                "decision": "accept",
                "human": True,
                "approved_by": args.approved_by,
                "confirmed_draft_hash": draft_hash,
                "confirmed_target_hash": target_hash,
                "target_path": selected_target_path,
            }
        result = applier.apply(
            draft_id,
            draft_hash=draft_hash,
            target_hash=target_hash if isinstance(target_hash, str) else None,
            content=content if isinstance(content, str) else None,
            target_path=selected_target_path if isinstance(selected_target_path, str) else None,
            human_approval=approval,
            approved_by=args.approved_by,
        )
        payload = result.to_dict()
        payload["protocol_version"] = config.protocol_version
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (ApprovalError, OSError, RuntimeError, ValueError, TypeError) as error:
        payload = _review_error(config, error)
        sys.stderr.write(f"review_apply: {payload['error']}\n")
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1
    finally:
        if store is not None:
            store.close()


def cmd_review_reject(args: argparse.Namespace) -> int:
    config: KnowledgeConfig | None = None
    store: DraftStore | None = None
    try:
        if not args.human_rejected:
            raise ApprovalError("review_reject requires the explicit --human-rejected human boundary")
        envelope = _review_envelope()
        config, store, applier = _review_applier(
            args,
            human_gate=args.human_rejected,
        )
        draft_id = args.draft_id or envelope.get("draft_id")
        reason = args.reason if args.reason is not None else envelope.get("reason")
        if not isinstance(draft_id, str) or not isinstance(reason, str):
            raise ValueError("review_reject requires draft-id and reason")
        result = applier.reject(
            draft_id,
            reason=reason,
            human_approval={"decision": "reject", "human": True, "approved_by": args.rejected_by},
            rejected_by=args.rejected_by,
        )
        result["protocol_version"] = config.protocol_version
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (ApprovalError, OSError, RuntimeError, ValueError, TypeError) as error:
        payload = _review_error(config, error)
        sys.stderr.write(f"review_reject: {payload['error']}\n")
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1
    finally:
        if store is not None:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent Knowledge Vault CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # status
    p_status = subparsers.add_parser("status", help="Check status of knowledge vaults and runtime")
    p_status.add_argument("--work-vault", help="Override work vault path")
    p_status.add_argument("--personal-vault", help="Override personal vault path")
    p_status.add_argument("--runtime-dir", help="Override runtime state directory")

    # init
    p_init = subparsers.add_parser("init", help="Initialize a work knowledge vault without overwriting")
    p_init.add_argument("--work-vault", help="Override work vault path")
    p_init.add_argument("--runtime-dir", help="Override runtime state directory")
    p_init.add_argument("--allow-nonempty", action="store_true", help="Allow initializing into a nonempty directory")

    p_index = subparsers.add_parser("index", help="Refresh the derived Markdown index")
    p_index.add_argument("--work-vault", help="Override work vault path")
    p_index.add_argument("--runtime-dir", help="Override runtime state directory")
    p_index.add_argument("--db-path", help="Override the derived SQLite index path")
    p_index.add_argument("--rebuild", action="store_true", help="Build a fresh SQLite database and replace the old one")

    p_search = subparsers.add_parser("search", help="Search the derived work-vault index")
    p_search.add_argument("query", nargs="?", help="Literal query text")
    p_search.add_argument("--query", dest="query_option", help="Literal query text")
    p_search.add_argument("--task-id", help="Optional task binding")
    p_search.add_argument("--project", help="Explicit 10-Projects subdirectory filter")
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument("--include-related", action="store_true")
    p_search.add_argument("--work-vault")
    p_search.add_argument("--runtime-dir")
    p_search.add_argument("--db-path")

    p_read = subparsers.add_parser("read_source", help="Read a verified indexed source range")
    p_read.add_argument("source_id")
    p_read.add_argument("--task-id", required=True)
    p_read.add_argument("--line-start", type=int)
    p_read.add_argument("--line-end", type=int)
    p_read.add_argument("--expected-hash")
    p_read.add_argument("--max-chars", type=int)
    p_read.add_argument("--work-vault")
    p_read.add_argument("--runtime-dir")
    p_read.add_argument("--db-path")

    p_context = subparsers.add_parser("build_context", help="Build bounded verified task context")
    p_context.add_argument("--task-id", required=True)
    p_context.add_argument("--query", required=True)
    p_context.add_argument("--project")
    p_context.add_argument("--limit", type=int, default=8)
    p_context.add_argument("--max-snippets", type=int, default=8)
    p_context.add_argument("--max-chars", type=int, default=12_000)
    p_context.add_argument("--exclude-source-id", action="append", default=[])
    p_context.add_argument("--include-related", action="store_true")
    p_context.add_argument("--work-vault")
    p_context.add_argument("--runtime-dir")
    p_context.add_argument("--db-path")

    p_propose = subparsers.add_parser(
        "propose_note",
        help="Capture one completed structured candidate into a private draft store",
    )
    p_propose.add_argument("--task-id", required=True)
    p_propose.add_argument("--turn-id", required=True)
    p_propose.add_argument(
        "--task-status",
        required=True,
        help="Recorded P4 task status; only 'completed' can create a draft",
    )
    p_propose.add_argument(
        "--draft-store",
        required=True,
        help="Explicit temporary/private SQLite draft-store path; no default is used",
    )
    p_propose.add_argument(
        "--allow-protected-runtime",
        action="store_true",
        help="Allow the explicit production draft store under ~/.agent/knowledge",
    )
    p_propose.add_argument(
        "--draft-vault",
        "--work-vault",
        dest="draft_vault",
        help="Optional explicit synthetic work vault for materializing a 00-Inbox draft file",
    )

    def add_review_storage_options(parser_: argparse.ArgumentParser) -> None:
        parser_.add_argument("--work-vault", required=True, help="Synthetic work vault path")
        parser_.add_argument("--draft-store", required=True, help="Private draft SQLite path")
        parser_.add_argument("--journal-path", required=True, help="Private apply journal JSONL path")
        parser_.add_argument("--db-path", help="Optional derived index SQLite path")
        parser_.add_argument("--draft-id", help="Draft identifier; may also be supplied in stdin JSON")

    p_review_apply = subparsers.add_parser(
        "review_apply", help="Human-only acceptance and conflict-safe Markdown apply"
    )
    add_review_storage_options(p_review_apply)
    p_review_apply.add_argument("--draft-hash", help="SHA-256 of the exact approved Markdown")
    p_review_apply.add_argument("--target-hash", help="Confirmed existing target SHA-256; omit for new notes")
    p_review_apply.add_argument("--target-path", help="Explicit human-selected formal destination for a new note")
    p_review_apply.add_argument("--content-file", help="Optional human-edited Markdown file")
    p_review_apply.add_argument("--approved-by", default="human-review")
    p_review_apply.add_argument(
        "--human-approved",
        action="store_true",
        help="Required explicit human approval marker; this is not an agent RPC",
    )

    p_review_reject = subparsers.add_parser(
        "review_reject", help="Human-only terminal rejection of a knowledge draft"
    )
    add_review_storage_options(p_review_reject)
    p_review_reject.add_argument("--reason", help="Bounded human rejection reason")
    p_review_reject.add_argument("--rejected-by", default="human-review")
    p_review_reject.add_argument(
        "--human-rejected",
        action="store_true",
        help="Required explicit human rejection marker; this is not an agent RPC",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help(sys.stderr)
        return 1

    if args.command == "status":
        return cmd_status(args)
    elif args.command == "init":
        return cmd_init(args)
    if args.command == "index":
        return cmd_index(args)
    elif args.command == "search":
        return cmd_search(args)
    elif args.command == "read_source":
        return cmd_read_source(args)
    elif args.command == "build_context":
        return cmd_build_context(args)
    elif args.command == "propose_note":
        return cmd_propose_note(args)
    elif args.command == "review_apply":
        return cmd_review_apply(args)
    elif args.command == "review_reject":
        return cmd_review_reject(args)
    else:
        sys.stderr.write(f"Unknown command: {args.command}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
