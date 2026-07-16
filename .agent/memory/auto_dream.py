"""Staging-only dream cycle. Mechanical work, no reasoning.

Responsibilities (in order):
  1. load episodic entries
  2. cluster + extract → structured patterns
  3. stage candidates (lifecycle metadata baked in)
  4. heuristic prefilter (length + exact-duplicate; obvious junk goes to rejected/)
  5. decay old episodes + archive stale workspace
  6. write REVIEW_QUEUE.md summary so the next host session sees the backlog

Never:
  - subjective validation (host agent reviews via CLI tools)
  - promotion to LESSONS.md (graduate.py does that)
  - git commit (unattended repo writes are dangerous on a host hook)
"""
import contextlib, datetime, json, os, sys, time
from promote import cluster_and_extract, write_candidates
from validate import heuristic_check
from review_state import mark_rejected, write_review_queue_summary
from decay import decay_old_entries
from archive import archive_stale_workspace
from dream_state import fail_cycle, finish_cycle, start_cycle

# fcntl is POSIX-only. On Windows the dream cycle is best-effort: concurrent
# writers there are rare (no shutdown hook = no parallel exits), and the lack
# of locking matches the existing _episodic_io.py fallback.
try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

ROOT = os.path.abspath(os.path.dirname(__file__))
EPISODIC = os.path.join(ROOT, "episodic/AGENT_LEARNINGS.jsonl")
CANDIDATES = os.path.join(ROOT, "candidates")
SEMANTIC = os.path.join(ROOT, "semantic")
REVIEW_QUEUE = os.path.join(ROOT, "working/REVIEW_QUEUE.md")
DREAM_STATE = os.path.join(ROOT, "dream-state.json")
STOP_ENTRY_MARKER = os.path.join(os.path.expanduser("~"), ".claude", "stop-hook-entry.jsonl")
STOP_COMPLETION_MARKER = os.path.join(os.path.expanduser("~"), ".claude", "stop-hook-fired.jsonl")
PROMOTION_THRESHOLD = 7.0
CLUSTER_SIMILARITY = 0.3
MAX_CLUSTER_ENTRIES = max(100, int(os.environ.get("AGENTIC_DREAM_MAX_CLUSTER_ENTRIES", "3000")))


def _status(message):
    """Keep Stop-hook stdout clean while preserving useful manual output."""
    if sys.stdout.isatty():
        print(message)


def _append_marker(path, phase, run_id):
    """Write legacy harness telemetry without affecting cycle correctness."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "script": os.path.basename(__file__),
            "phase": phase,
            "run_id": run_id,
        }
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def _entries_for_clustering(entries):
    """Bound quadratic clustering without deleting or rewriting history."""
    return entries[-MAX_CLUSTER_ENTRIES:]


@contextlib.contextmanager
def _episodic_locked():
    """Hold an exclusive flock on AGENT_LEARNINGS.jsonl across the entire
    dream-cycle read-modify-write window.

    Without a window-spanning lock, an `append_jsonl()` call that lands
    between `_load_entries_locked()` and `_write_entries_locked(kept)` is
    silently truncated away by the rewrite. With this context manager,
    every appender (`_episodic_io.append_jsonl`, which takes LOCK_EX on
    the same file) blocks until the dream cycle releases the lock.

    Yields the open file descriptor so callers can read/write without
    racing on a second open(). On Windows (no fcntl) yields None and
    falls back to the historical best-effort behavior.
    """
    if fcntl is None:
        yield None
        return
    os.makedirs(os.path.dirname(EPISODIC), exist_ok=True)
    fd = os.open(EPISODIC, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _load_entries_locked(fd):
    """Read all entries from the locked fd, or fall back to plain read on
    Windows (fd is None when fcntl is unavailable).
    """
    entries = []
    if fd is None:
        if not os.path.exists(EPISODIC):
            return entries
        with open(EPISODIC) as f:
            stream = f.read()
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        while True:
            buf = os.read(fd, 65536)
            if not buf:
                break
            chunks.append(buf)
        stream = b"".join(chunks).decode("utf-8", errors="replace")
    for line in stream.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _write_entries_locked(fd, entries):
    """Truncate-and-rewrite under the same lock _load_entries_locked used.

    Holding one fd across read+write is what makes the operation atomic
    against concurrent `append_jsonl()` calls.
    """
    payload = "".join(json.dumps(e) + "\n" for e in entries).encode("utf-8")
    if fd is None:
        # Windows: best-effort, matches _episodic_io fallback.
        with open(EPISODIC, "w") as f:
            f.write(payload.decode("utf-8"))
        return
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("episodic rewrite made no progress")
        written += count
    os.fsync(fd)


# Compatibility shims for any external caller that still imports the
# pre-refactor names. Internal callers in run_dream_cycle use the locked
# helpers directly so the lock spans the full cycle.
def _load_entries():
    with _episodic_locked() as fd:
        return _load_entries_locked(fd)


def _write_entries(entries):
    with _episodic_locked() as fd:
        _write_entries_locked(fd, entries)


def _heuristic_prefilter(candidates_dir, semantic_dir):
    """Move obvious junk (too-short, exact duplicate) to rejected/ automatically.

    Anything subjective — "is this really a useful lesson?" — is the host
    agent's call, not this function's.
    """
    if not os.path.isdir(candidates_dir):
        return 0
    lessons_path = os.path.join(semantic_dir, "LESSONS.md")
    existing = open(lessons_path).read() if os.path.exists(lessons_path) else ""
    rejected = 0
    for fname in sorted(os.listdir(candidates_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(candidates_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                cand = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        check = heuristic_check(cand, existing)
        if not check["passed"]:
            reason = ", ".join(check["reasons"])
            # Record the specific lesson(s) that triggered the duplicate
            # rejection so write_candidates can check whether THIS blocker
            # is still there, not just whether LESSONS.md as a whole changed.
            mark_rejected(cand["id"], "heuristic_prefilter", reason,
                          candidates_dir,
                          duplicate_claims=check.get("duplicates", []))
            rejected += 1
    return rejected


def run_dream_cycle():
    # Hold the lock across the FULL read-modify-write window. Any
    # append_jsonl() call from another harness blocks until we release.
    # Without this, an append landing between read and rewrite would be
    # truncated away.
    with _episodic_locked() as fd:
        entries = _load_entries_locked(fd)
        if not entries:
            # Still refresh the review queue — candidates may have been staged
            # in a previous cycle and the host agent loads REVIEW_QUEUE.md
            # into every session via build_context, so a stale/missing file
            # hides real work.
            pending = write_review_queue_summary(CANDIDATES, REVIEW_QUEUE)
            _status(f"dream cycle: no entries (queue has {pending} pending)")
            return

        cluster_entries = _entries_for_clustering(entries)
        patterns = cluster_and_extract(cluster_entries, threshold=CLUSTER_SIMILARITY)
        promotable = {k: p for k, p in patterns.items()
                      if p.get("canonical_salience", 0) >= PROMOTION_THRESHOLD}

        staged = write_candidates(promotable, CANDIDATES)
        prefiltered = _heuristic_prefilter(CANDIDATES, SEMANTIC)

        kept, archived = decay_old_entries(
            entries, archive_dir=os.path.join(ROOT, "episodic/snapshots"))
        _write_entries_locked(fd, kept)
        archive_stale_workspace(
            working_dir=os.path.join(ROOT, "working"),
            archive_dir=os.path.join(ROOT, "episodic/snapshots"))

        pending = write_review_queue_summary(CANDIDATES, REVIEW_QUEUE)

    _status(
        f"dream cycle: patterns={len(patterns)} staged={staged} "
        f"prefiltered_out={prefiltered} pending_review={pending} "
        f"archived={len(archived)} kept={len(kept)} "
        f"clustered={len(cluster_entries)}/{len(entries)}"
    )


def main():
    started = time.monotonic()
    run_id = start_cycle(DREAM_STATE)
    try:
        _append_marker(STOP_ENTRY_MARKER, "entry", run_id)
        run_dream_cycle()
        _append_marker(STOP_COMPLETION_MARKER, "completed", run_id)
    except BaseException as exc:
        fail_cycle(DREAM_STATE, run_id, exc, started_monotonic=started)
        raise
    else:
        # This is deliberately the last write: success means the entire cycle,
        # including queue rendering and archival, completed without error.
        finish_cycle(DREAM_STATE, run_id, started_monotonic=started)


if __name__ == "__main__":
    main()
