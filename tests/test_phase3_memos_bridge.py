from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.memos_bridge import (
    PINNED_CAPABILITIES,
    BridgeConfig,
    MemOSBridgeClient,
    MemOSProtocolError,
    MemOSTimeoutError,
    MemOSTransportError,
    MemOSUnavailableError,
    MemOSUpstreamError,
    MemOSVersionError,
)


def _script(tmp_path: Path, body: str) -> tuple[str, ...]:
    path = tmp_path / "fake_bridge.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return (sys.executable, "-u", str(path))


def test_calls_use_monotonic_ids_and_ignore_notifications(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        for line in sys.stdin:
            request = json.loads(line)
            print(json.dumps({"jsonrpc":"2.0", "method":"progress", "params":{"n":1}}), flush=True)
            print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":request["id"]}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(command=command)) as client:
        assert client.call("episode.add", {"value": 1}) == 1
        assert client.call("episode.add", {"value": 2}) == 2


def test_known_upstream_preinit_console_log_is_bounded_and_ignored(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        request = json.loads(sys.stdin.readline())
        print('21:46:39.826 INFO  [storage] sqlite.open filepath="redacted"', flush=True)
        print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":True}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(command=command)) as client:
        assert client.call("core.health") is True
        assert "sqlite.open" in client.recent_stderr


def test_environment_is_sanitized_and_keeps_runtime_roots_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-boundary")
    command = _script(
        tmp_path,
        """
        import json, os, sys
        request = json.loads(sys.stdin.readline())
        result = [os.environ.get("HOME"), os.environ.get("MEMOS_HOME"), os.environ.get("MEMOS_CONFIG_FILE"), os.environ.get("OPENAI_API_KEY")]
        print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":result}), flush=True)
        """,
    )
    config = BridgeConfig(
        command=command,
        home=str(tmp_path / "home"),
        memos_home=str(tmp_path / "memos"),
        memos_config_file=str(tmp_path / "config.yaml"),
    )
    with MemOSBridgeClient(config) as client:
        assert client.call("env") == [
            config.home, config.memos_home, config.memos_config_file, None,
        ]


def test_health_validates_pinned_version_and_supplies_pinned_capabilities(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        request = json.loads(sys.stdin.readline())
        print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":{"version":"2.0.10", "ok":True}}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(command=command)) as client:
        health = client.health()
    assert health["version"] == "2.0.10"
    assert health["capabilities"] == PINNED_CAPABILITIES


def test_health_rejects_wrong_or_missing_version(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        request = json.loads(sys.stdin.readline())
        print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":{"version":"2.0.9"}}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(command=command)) as client:
        with pytest.raises(MemOSVersionError):
            client.health()


def test_timeout_is_typed_and_ambiguous_after_write(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import sys, time
        sys.stdin.readline()
        time.sleep(2)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(command=command, call_timeout=0.05)) as client:
        with pytest.raises(MemOSTimeoutError) as caught:
            client.call("feedback.submit", retryable=False)
    assert caught.value.ambiguous is True


def test_write_timeout_is_bounded_when_bridge_never_reads(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import time
        time.sleep(10)
        """,
    )
    client = MemOSBridgeClient(BridgeConfig(
        command=command, call_timeout=0.08, shutdown_timeout=0.1,
        max_line_bytes=1024 * 1024,
    ))
    started = time.monotonic()
    try:
        with pytest.raises(MemOSTimeoutError, match="writing request") as caught:
            client.call("memory.search", {"query": "x" * 900_000})
    finally:
        client.close()
    assert time.monotonic() - started < 0.75
    assert caught.value.ambiguous is True


def test_upstream_and_malformed_responses_are_distinct(tmp_path: Path) -> None:
    upstream = _script(
        tmp_path,
        """
        import json, sys
        request = json.loads(sys.stdin.readline())
        print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "error":{"code":123, "message":"nope", "data":{"why":"test"}}}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(command=upstream)) as client:
        with pytest.raises(MemOSUpstreamError) as caught:
            client.call("x")
    assert caught.value.code == 123
    assert caught.value.data == {"why": "test"}

    malformed = _script(tmp_path, "print('{not-json', flush=True)")
    with MemOSBridgeClient(BridgeConfig(command=malformed)) as client:
        with pytest.raises(MemOSProtocolError) as caught:
            client.call("x")
    assert caught.value.ambiguous is True


def test_bounded_line_size_rejects_oversized_output(tmp_path: Path) -> None:
    command = _script(tmp_path, "print('x' * 500, flush=True)")
    with MemOSBridgeClient(BridgeConfig(command=command, max_line_bytes=128)) as client:
        with pytest.raises(MemOSProtocolError, match="maximum line size") as caught:
            client.call("x")
    assert caught.value.ambiguous is True


def test_bounded_line_size_rejects_oversized_request_before_write(tmp_path: Path) -> None:
    command = _script(tmp_path, "import time; time.sleep(1)")
    with MemOSBridgeClient(BridgeConfig(command=command, max_line_bytes=128)) as client:
        with pytest.raises(MemOSProtocolError, match="request exceeds") as caught:
            client.call("x", {"value": "x" * 500})
    assert caught.value.ambiguous is False


def test_stderr_is_drained_so_large_diagnostics_do_not_deadlock(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        request = json.loads(sys.stdin.readline())
        sys.stderr.write("diagnostic" * 20000)
        sys.stderr.flush()
        print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":True}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(command=command, call_timeout=1)) as client:
        assert client.call("x") is True
        assert client.recent_stderr


def test_retryable_call_restarts_once_but_nonretryable_feedback_does_not(tmp_path: Path) -> None:
    marker = tmp_path / "starts"
    command = _script(
        tmp_path,
        f"""
        import json, pathlib, sys
        marker = pathlib.Path({str(marker)!r})
        count = int(marker.read_text()) + 1 if marker.exists() else 1
        marker.write_text(str(count))
        request = json.loads(sys.stdin.readline())
        if count == 1:
            sys.exit(3)
        print(json.dumps({{"jsonrpc":"2.0", "id":request["id"], "result":"recovered"}}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(command=command, call_timeout=0.5)) as client:
        assert client.call("episode.upsert", retryable=True) == "recovered"
    assert marker.read_text() == "2"

    marker.unlink()
    with MemOSBridgeClient(BridgeConfig(command=command, call_timeout=0.5)) as client:
        with pytest.raises(MemOSTransportError) as caught:
            client.call("feedback.submit", retryable=False)
    assert caught.value.ambiguous is True
    assert marker.read_text() == "1"


def test_retry_attempts_share_one_total_call_deadline(tmp_path: Path) -> None:
    marker = tmp_path / "starts"
    command = _script(
        tmp_path,
        f"""
        import json, pathlib, sys, time
        marker = pathlib.Path({str(marker)!r})
        count = int(marker.read_text()) + 1 if marker.exists() else 1
        marker.write_text(str(count))
        json.loads(sys.stdin.readline())
        if count == 1:
            time.sleep(0.3)
            sys.exit(3)
        time.sleep(1)
        """,
    )
    client = MemOSBridgeClient(BridgeConfig(
        command=command, call_timeout=0.6, shutdown_timeout=0.03,
    ))
    started = time.monotonic()
    try:
        with pytest.raises(MemOSTimeoutError):
            client.call("memory.search", retryable=True)
        elapsed = time.monotonic() - started
    finally:
        client.close()
    assert elapsed < 0.85
    assert marker.read_text() == "2"


def test_write_timeout_can_close_and_restart_without_live_writer(tmp_path: Path) -> None:
    marker = tmp_path / "starts"
    command = _script(
        tmp_path,
        f"""
        import json, pathlib, sys, time
        marker = pathlib.Path({str(marker)!r})
        count = int(marker.read_text()) + 1 if marker.exists() else 1
        marker.write_text(str(count))
        if count == 1:
            time.sleep(10)
        else:
            request = json.loads(sys.stdin.readline())
            print(json.dumps({{"jsonrpc":"2.0", "id":request["id"], "result":True}}), flush=True)
        """,
    )
    client = MemOSBridgeClient(BridgeConfig(
        command=command, call_timeout=0.08, shutdown_timeout=0.1,
        circuit_cooldown=0.02, max_line_bytes=1024 * 1024,
    ))
    with pytest.raises(MemOSTimeoutError):
        client.call("memory.search", {"query": "x" * 900_000})
    time.sleep(0.03)
    assert client.call("core.health", timeout=1.0) is True
    client.close()
    assert not client._writer_threads
    assert not any(t.name == "memos-stdin" and t.is_alive() for t in threading.enumerate())


def test_exhausted_retry_opens_circuit_then_cooldown_allows_restart(tmp_path: Path) -> None:
    marker = tmp_path / "starts"
    command = _script(
        tmp_path,
        f"""
        import pathlib, sys
        marker = pathlib.Path({str(marker)!r})
        marker.write_text(str(int(marker.read_text()) + 1 if marker.exists() else 1))
        sys.stdin.readline()
        sys.exit(2)
        """,
    )
    config = BridgeConfig(command=command, call_timeout=0.2, circuit_cooldown=0.08)
    with MemOSBridgeClient(config) as client:
        with pytest.raises(MemOSTransportError):
            client.call("event", retryable=True)
        assert marker.read_text() == "2"
        with pytest.raises(MemOSUnavailableError, match="circuit"):
            client.call("event", retryable=True)
        time.sleep(0.1)
        with pytest.raises(MemOSTransportError):
            client.call("event", retryable=False)
    assert marker.read_text() == "3"


def test_missing_executable_is_typed_unavailable() -> None:
    client = MemOSBridgeClient(BridgeConfig(command=("/definitely/missing/memos",)))
    with pytest.raises(MemOSUnavailableError):
        client.call("x")
    client.close()


def test_close_requests_shutdown_and_reaps_process(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown"
    command = _script(
        tmp_path,
        f"""
        import json, pathlib, sys
        for line in sys.stdin:
            request = json.loads(line)
            print(json.dumps({{"jsonrpc":"2.0", "id":request["id"], "result":True}}), flush=True)
            if request["method"] == "core.shutdown":
                pathlib.Path({str(marker)!r}).write_text("yes")
                break
        """,
    )
    client = MemOSBridgeClient(BridgeConfig(command=command, shutdown_timeout=0.5))
    assert client.call("x") is True
    client.close()
    assert marker.read_text() == "yes"
    assert client.closed


def test_close_honors_absolute_deadline_on_slow_shutdown(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys, time
        for line in sys.stdin:
            request = json.loads(line)
            if request["method"] == "core.shutdown":
                time.sleep(2)
                continue
            print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":True}), flush=True)
        """,
    )
    client = MemOSBridgeClient(BridgeConfig(command=command, shutdown_timeout=0.5))
    assert client.call("x") is True
    started = time.monotonic()
    client.close(deadline=started + 0.1)
    assert time.monotonic() - started < 0.2
    assert client.closed
