"""Regression tests for the SketchUp JSON-RPC transport.

Each test pins down one of the defects that made the upstream client unreliable:
framing, stale sockets, id correlation, error surfacing, timeouts, and above all
the rule that a modelling command is never sent to SketchUp twice.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from sketchup_mcp.transport import (
    SketchupConnection,
    SketchupConnectionError,
    SketchupProtocolError,
    SketchupTimeoutError,
    SketchupToolError,
    SketchupTransportError,
    result_text,
)

from .fake_sketchup import FakeSketchup


def connect_to(server: FakeSketchup, **kwargs) -> SketchupConnection:
    kwargs.setdefault("timeout", 2.0)
    kwargs.setdefault("probe_timeout", 1.0)
    kwargs.setdefault("probe_idle_after", 1e9)  # no probe unless a test asks
    return SketchupConnection(host=server.host, port=server.port, **kwargs)


def payload(result) -> dict:
    return json.loads(result_text(result))


# -- happy path ------------------------------------------------------------


def test_single_call_returns_result():
    with FakeSketchup() as server:
        conn = connect_to(server)
        try:
            result = conn.call("get_selection", {}, retry_safe=True)
        finally:
            conn.close()
        assert payload(result) == {"tool": "get_selection", "arguments": {}}
        assert server.connection_count == 1


def test_many_sequential_calls_reuse_one_connection():
    with FakeSketchup() as server:
        conn = connect_to(server)
        try:
            for index in range(10):
                mutation = conn.call("create_component", {"i": index})
                assert payload(mutation)["arguments"] == {"i": index}
                read = conn.call("get_selection", {}, retry_safe=True)
                assert payload(read)["tool"] == "get_selection"
        finally:
            conn.close()

        assert server.connection_count == 1, "the connection should have been reused"
        assert len(server.tool_calls("create_component")) == 10
        assert len(server.tool_calls("get_selection")) == 10


def test_request_ids_are_unique_and_owned_by_the_connection():
    with FakeSketchup() as server:
        conn = connect_to(server)
        try:
            for _ in range(5):
                conn.call("get_selection", {}, retry_safe=True)
        finally:
            conn.close()
        ids = [request["id"] for request in server.tool_calls()]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)


# -- framing ---------------------------------------------------------------


def test_reply_split_across_many_tcp_segments():
    def responder(request, client, server):
        blob = json.dumps(
            {
                "jsonrpc": "2.0",
                "result": {"content": [{"type": "text", "text": "x" * 500}]},
                "id": request.get("id"),
            }
        ).encode("utf-8") + b"\n"
        for start in range(0, len(blob), 7):
            client.send_raw(blob[start : start + 7])
            time.sleep(0.001)

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            result = conn.call("get_selection", {}, retry_safe=True)
        finally:
            conn.close()
        assert result_text(result) == "x" * 500


def test_two_replies_in_one_segment_do_not_corrupt_the_next_call():
    """A stale reply glued to the real one must be discarded, not returned."""

    state = {"calls": 0}

    def responder(request, client, server):
        if request.get("method") == "ping":
            client.reply_result(request, {"ok": True})
            return
        state["calls"] += 1
        stale = json.dumps(
            {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "STALE"}]}, "id": 99999}
        ).encode("utf-8")
        good = json.dumps(
            {
                "jsonrpc": "2.0",
                "result": {"content": [{"type": "text", "text": f"call-{state['calls']}"}]},
                "id": request.get("id"),
            }
        ).encode("utf-8")
        client.send_raw(stale + b"\n" + good + b"\n")

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            assert result_text(conn.call("get_selection", {}, retry_safe=True)) == "call-1"
            assert result_text(conn.call("get_selection", {}, retry_safe=True)) == "call-2"
        finally:
            conn.close()


def test_reply_for_another_id_is_discarded():
    def responder(request, client, server):
        client.send_json(
            {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "wrong"}]}, "id": 4242}
        )
        client.reply_text(request, "right")

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            assert result_text(conn.call("get_selection", {}, retry_safe=True)) == "right"
        finally:
            conn.close()


def test_unparsable_frame_is_skipped():
    def responder(request, client, server):
        client.send_raw(b"this is not json\n")
        client.reply_text(request, "recovered")

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            assert result_text(conn.call("get_selection", {}, retry_safe=True)) == "recovered"
        finally:
            conn.close()


def test_reply_without_trailing_newline_before_close_is_accepted():
    def responder(request, client, server):
        client.send_json({"jsonrpc": "2.0", "result": "bare", "id": request.get("id")}, newline=False)
        client.close()

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            assert conn.call("get_selection", {}, retry_safe=True) == "bare"
        finally:
            conn.close()


def test_endless_garbage_eventually_fails_rather_than_hanging():
    def responder(request, client, server):
        client.send_raw(b"\n".join([b"{}"] * 100) + b"\n")

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            with pytest.raises(SketchupProtocolError):
                conn.call("get_selection", {}, retry_safe=True)
        finally:
            conn.close()


# -- stale sockets ---------------------------------------------------------


def test_reconnects_when_the_peer_closed_between_calls():
    """The exact upstream failure: a reused socket the peer already dropped."""

    def responder(request, client, server):
        params = request.get("params") or {}
        client.reply_text(request, json.dumps({"tool": params.get("name")}))
        if client.index == 1:
            client.close()  # emulate a server that hangs up after one request

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            assert payload(conn.call("create_component", {"a": 1}))["tool"] == "create_component"
            # Give the FIN time to arrive so the next call sees a closed peer.
            time.sleep(0.2)
            assert payload(conn.call("create_component", {"a": 2}))["tool"] == "create_component"
        finally:
            conn.close()

        assert server.connection_count == 2, "should have reconnected instead of reusing"
        calls = server.tool_calls("create_component")
        assert [call["params"]["arguments"] for call in calls] == [{"a": 1}, {"a": 2}], (
            "each mutation must arrive exactly once"
        )


def test_idle_socket_is_ping_probed_before_a_mutation():
    with FakeSketchup() as server:
        conn = connect_to(server, probe_idle_after=0.0)
        try:
            conn.call("create_component", {"n": 1})
            conn.call("create_component", {"n": 2})
        finally:
            conn.close()
        methods = server.methods()
        assert methods.count("ping") >= 1, "an idle reused socket should be probed"
        assert len(server.tool_calls("create_component")) == 2


def test_probe_failure_reconnects_before_the_mutation_is_sent():
    """A dead peer must be discovered by the probe, not by the mutation."""

    def responder(request, client, server):
        if request.get("method") == "ping" and client.index == 1:
            client.close()  # first connection dies during the probe
            return
        default_reply(request, client)

    def default_reply(request, client):
        params = request.get("params") or {}
        client.reply_text(request, json.dumps({"tool": params.get("name"), "conn": client.index}))

    with FakeSketchup(responder) as server:
        conn = connect_to(server, probe_idle_after=0.0)
        try:
            first = payload(conn.call("create_component", {"n": 1}))
            assert first["conn"] == 1
            second = payload(conn.call("create_component", {"n": 2}))
            assert second["conn"] == 2, "should have moved to a fresh connection"
        finally:
            conn.close()
        assert len(server.tool_calls("create_component")) == 2, "no duplicate mutation"


def test_unsolicited_bytes_are_discarded_before_the_next_request():
    """Upstream wrote an unread ping; make sure leftovers cannot be mistaken for a reply."""

    def responder(request, client, server):
        params = request.get("params") or {}
        client.reply_text(request, json.dumps({"tool": params.get("name")}))
        if client.index == 1 and params.get("name") == "get_selection":
            # Push a second, unsolicited reply nobody asked for.
            client.send_json(
                {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "ghost"}]}, "id": 0}
            )

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            conn.call("get_selection", {}, retry_safe=True)
            time.sleep(0.2)
            result = payload(conn.call("create_component", {"n": 1}))
            assert result["tool"] == "create_component"
        finally:
            conn.close()
        assert server.connection_count == 1


# -- errors, timeouts, retries --------------------------------------------


def test_jsonrpc_error_is_raised_not_returned_as_success():
    def responder(request, client, server):
        client.reply_error(request, "Unknown tool: nope", code=-32601)

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            with pytest.raises(SketchupToolError) as excinfo:
                conn.call("nope", {})
        finally:
            conn.close()
        assert "Unknown tool: nope" in str(excinfo.value)
        assert excinfo.value.code == -32601


def test_timeout_does_not_resend_a_mutation():
    def responder(request, client, server):
        if request.get("method") == "ping":
            client.reply_result(request, {"ok": True})
        # tools/call: stay silent

    with FakeSketchup(responder) as server:
        conn = connect_to(server, timeout=0.4)
        try:
            with pytest.raises(SketchupTimeoutError) as excinfo:
                conn.call("create_component", {"n": 1})
        finally:
            conn.close()
        assert excinfo.value.request_sent is True
        assert "not retried" in str(excinfo.value)
        assert len(server.tool_calls("create_component")) == 1, "mutation must be sent once"


def test_timeout_retries_a_read_only_call():
    state = {"seen": 0}

    def responder(request, client, server):
        if request.get("method") == "ping":
            client.reply_result(request, {"ok": True})
            return
        state["seen"] += 1
        if state["seen"] == 1:
            return  # silence, force a timeout
        client.reply_text(request, "second attempt")

    with FakeSketchup(responder) as server:
        conn = connect_to(server, timeout=0.4)
        try:
            assert result_text(conn.call("get_selection", {}, retry_safe=True)) == "second attempt"
        finally:
            conn.close()
        assert len(server.tool_calls("get_selection")) == 2


def test_peer_closing_mid_request_does_not_resend_a_mutation():
    def responder(request, client, server):
        if request.get("method") == "ping":
            client.reply_result(request, {"ok": True})
            return
        client.close()  # hang up without replying

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            with pytest.raises(SketchupConnectionError) as excinfo:
                conn.call("create_component", {"n": 1})
        finally:
            conn.close()
        assert excinfo.value.request_sent is True
        assert "not retried" in str(excinfo.value)
        assert len(server.tool_calls("create_component")) == 1


def test_connection_refused_is_actionable_and_sends_nothing():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    host, port = probe.getsockname()
    probe.close()  # nothing is listening on this port now

    conn = SketchupConnection(host=host, port=port, timeout=0.5)
    with pytest.raises(SketchupConnectionError) as excinfo:
        conn.call("create_component", {"n": 1})
    message = str(excinfo.value)
    assert f"{host}:{port}" in message
    assert "Start SketchUp" in message
    assert excinfo.value.request_sent is False


# -- ping ------------------------------------------------------------------


def test_ping_reports_supported_extension():
    with FakeSketchup() as server:
        conn = connect_to(server)
        try:
            status = conn.ping()
        finally:
            conn.close()
        assert status["alive"] is True
        assert status["supported"] is True


def test_ping_treats_method_not_found_as_alive_but_unsupported():
    """An extension older than the ping handler must not look dead."""

    def responder(request, client, server):
        if request.get("method") == "ping":
            client.reply_error(request, "Method not found", code=-32601)
            return
        client.reply_text(request, "ok")

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            status = conn.ping()
            assert status == {"alive": True, "supported": False}
            # and the connection is still usable straight afterwards
            assert result_text(conn.call("get_selection", {}, retry_safe=True)) == "ok"
        finally:
            conn.close()


def test_is_connected_detects_a_closed_peer():
    def responder(request, client, server):
        client.reply_text(request, "bye")
        client.close()

    with FakeSketchup(responder) as server:
        conn = connect_to(server)
        try:
            conn.call("get_selection", {}, retry_safe=True)
            deadline = time.monotonic() + 2.0
            while conn.is_connected() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not conn.is_connected()
        finally:
            conn.close()


# -- concurrency -----------------------------------------------------------


def test_concurrent_callers_do_not_interleave_frames():
    with FakeSketchup() as server:
        conn = connect_to(server)
        results: dict = {}
        errors: list = []

        def worker(index: int) -> None:
            try:
                results[index] = payload(conn.call("create_component", {"i": index}))
            except SketchupTransportError as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        conn.close()

        assert not errors
        assert len(results) == 8
        for index, result in results.items():
            assert result["arguments"] == {"i": index}, "a caller got someone else's reply"
        assert len(server.tool_calls("create_component")) == 8


# -- helpers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"content": [{"type": "text", "text": "hello"}]}, "hello"),
        ({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}, "a\nb"),
        ({"content": []}, ""),
        ({"result": "plain"}, "plain"),
        ("raw", "raw"),
        ({}, ""),
        (None, ""),
    ],
)
def test_result_text(value, expected):
    assert result_text(value) == expected
