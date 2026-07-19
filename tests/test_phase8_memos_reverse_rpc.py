from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.memos_bridge import (  # noqa: E402
    BridgeConfig,
    MemOSBridgeClient,
    MemOSProtocolError,
)


def _script(tmp_path: Path, body: str) -> tuple[str, ...]:
    path = tmp_path / "fake_reverse_bridge.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return (sys.executable, "-u", str(path))


def test_reverse_host_completion_runs_during_forward_call(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        forward = json.loads(sys.stdin.readline())
        print(json.dumps({"jsonrpc":"2.0", "id":"srv-1", "method":"host.llm.complete", "params":{"value":"safe"}}), flush=True)
        reverse = json.loads(sys.stdin.readline())
        assert reverse == {"jsonrpc":"2.0", "id":"srv-1", "result":{"text":"SAFE"}}
        print(json.dumps({"jsonrpc":"2.0", "id":forward["id"], "result":"forward-ok"}), flush=True)
        """,
    )
    seen: list[Any] = []

    def complete(params: Any) -> Any:
        seen.append(params)
        return {"text": params["value"].upper()}

    with MemOSBridgeClient(BridgeConfig(
        command=command, request_handlers={"host.llm.complete": complete},
    )) as client:
        assert client.call("turn.end") == "forward-ok"
    assert seen == [{"value": "safe"}]


def test_unknown_reverse_method_returns_sanitized_jsonrpc_error(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        forward = json.loads(sys.stdin.readline())
        print(json.dumps({"jsonrpc":"2.0", "id":"srv-7", "method":"host.secret.dump", "params":{"secret":"do-not-echo"}}), flush=True)
        reverse = json.loads(sys.stdin.readline())
        assert reverse["id"] == "srv-7"
        assert reverse["error"] == {"code":-32601, "message":"reverse method is not allowed"}
        assert "do-not-echo" not in json.dumps(reverse)
        print(json.dumps({"jsonrpc":"2.0", "id":forward["id"], "result":True}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(command=command)) as client:
        assert client.call("turn.end") is True


def test_reverse_handler_failure_does_not_echo_exception_or_params(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        forward = json.loads(sys.stdin.readline())
        print(json.dumps({"jsonrpc":"2.0", "id":"srv-8", "method":"host.llm.complete", "params":{"prompt":"private-prompt"}}), flush=True)
        reverse = json.loads(sys.stdin.readline())
        assert reverse == {"jsonrpc":"2.0", "id":"srv-8", "error":{"code":-32000, "message":"reverse handler failed"}}
        assert "private-prompt" not in json.dumps(reverse)
        assert "private-exception" not in json.dumps(reverse)
        print(json.dumps({"jsonrpc":"2.0", "id":forward["id"], "result":True}), flush=True)
        """,
    )

    def fail(_params: Any) -> Any:
        raise RuntimeError("private-exception")

    with MemOSBridgeClient(BridgeConfig(
        command=command, request_handlers={"host.llm.complete": fail},
    )) as client:
        assert client.call("turn.end") is True


def test_reverse_handler_timeout_is_sanitized_and_forward_call_continues(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        forward = json.loads(sys.stdin.readline())
        print(json.dumps({"jsonrpc":"2.0", "id":"srv-2", "method":"host.llm.complete", "params":{}}), flush=True)
        reverse = json.loads(sys.stdin.readline())
        assert reverse == {"jsonrpc":"2.0", "id":"srv-2", "error":{"code":-32001, "message":"reverse handler timed out"}}
        print(json.dumps({"jsonrpc":"2.0", "id":forward["id"], "result":"ok"}), flush=True)
        """,
    )
    release = threading.Event()

    def hang(_params: Any) -> Any:
        release.wait(1)
        return {"late": True}

    try:
        with MemOSBridgeClient(BridgeConfig(
            command=command,
            request_handlers={"host.llm.complete": hang},
            request_timeout=0.05,
            call_timeout=0.5,
        )) as client:
            assert client.call("turn.end") == "ok"
    finally:
        release.set()


def test_malformed_reverse_id_is_protocol_failure(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        json.loads(sys.stdin.readline())
        print(json.dumps({"jsonrpc":"2.0", "id":"srv-0", "method":"host.llm.complete", "params":{}}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(
        command=command, request_handlers={"host.llm.complete": lambda params: {}},
    )) as client:
        with pytest.raises(MemOSProtocolError, match="reverse request id"):
            client.call("turn.end")


def test_oversized_reverse_result_becomes_sanitized_error(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        forward = json.loads(sys.stdin.readline())
        print(json.dumps({"jsonrpc":"2.0", "id":"srv-3", "method":"host.llm.complete", "params":{}}), flush=True)
        reverse = json.loads(sys.stdin.readline())
        assert reverse == {"jsonrpc":"2.0", "id":"srv-3", "error":{"code":-32002, "message":"reverse result exceeds maximum line size"}}
        print(json.dumps({"jsonrpc":"2.0", "id":forward["id"], "result":True}), flush=True)
        """,
    )
    with MemOSBridgeClient(BridgeConfig(
        command=command,
        request_handlers={"host.llm.complete": lambda params: {"text": "x" * 1000}},
        max_line_bytes=256,
    )) as client:
        assert client.call("turn.end") is True


def test_reverse_handler_concurrency_is_bounded(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        forward = json.loads(sys.stdin.readline())
        for n in (1, 2):
            print(json.dumps({"jsonrpc":"2.0", "id":f"srv-{n}", "method":"host.llm.complete", "params":{"n":n}}), flush=True)
        replies = [json.loads(sys.stdin.readline()), json.loads(sys.stdin.readline())]
        by_id = {item["id"]: item for item in replies}
        assert by_id["srv-2"]["error"] == {"code":-32003, "message":"reverse handler is busy"}
        assert by_id["srv-1"]["result"] == {"ok":1}
        print(json.dumps({"jsonrpc":"2.0", "id":forward["id"], "result":True}), flush=True)
        """,
    )
    active = 0
    peak = 0
    lock = threading.Lock()

    def complete(params: Any) -> Any:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return {"ok": params["n"]}

    with MemOSBridgeClient(BridgeConfig(
        command=command,
        request_handlers={"host.llm.complete": complete},
        max_request_handlers=1,
        call_timeout=0.5,
    )) as client:
        assert client.call("turn.end") is True
    assert peak == 1


def test_immediate_reverse_error_workers_are_bounded(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        """
        import json, sys
        json.loads(sys.stdin.readline())
        for n in range(1, 3):
            print(json.dumps({"jsonrpc":"2.0", "id":f"srv-{n}", "method":"not.allowed"}), flush=True)
        # A bounded client either returns one error or terminates this noisy
        # peer; it must never create one worker per frame.
        sys.stdin.readline()
        """,
    )
    with MemOSBridgeClient(BridgeConfig(
        command=command, max_request_handlers=1, call_timeout=0.5,
    )) as client:
        original = client._send_reverse_response

        def slow_response(*args, **kwargs):
            time.sleep(0.1)
            return original(*args, **kwargs)

        client._send_reverse_response = slow_response
        with pytest.raises(MemOSProtocolError, match="capacity"):
            client.call("turn.end")
