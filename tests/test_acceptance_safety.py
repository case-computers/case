# SPDX-License-Identifier: MIT
"""No-Docker checks for the acceptance harness.

Run: .venv/bin/python tests/test_acceptance_safety.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_acceptance as acceptance  # noqa: E402


def test_import_does_not_start_a_server():
    assert acceptance.BASE is None
    assert acceptance.ROOT is None


def test_child_env_removes_case_and_provider_settings():
    parent = {
        "CASE_HOME": "/real/vault",
        "CASE_TOKEN": "real-token",
        "CASE_DOCKER_NETWORK": "case-desks",
        "DESK_DEBUG": "1",
        "OPENAI_API_KEY": "provider-key",
        "ANTHROPIC_API_KEY": "provider-key",
        "CASE_NTFY_TOKEN": "notify-token",
        "DOCKER_HOST": "unix:///tmp/docker.sock",
        "DOCKER_CERT_PATH": "/tmp/docker-certs",
        "PATH": "/bin",
    }
    env = acceptance.child_env(parent, "/tmp/test-vault", 43123, "test-token", "test-image")
    assert env["CASE_HOME"] == "/tmp/test-vault"
    assert env["CASE_PORT"] == "43123"
    assert env["CASE_TOKEN"] == "test-token"
    assert env["CASE_IMAGE"] == "test-image"
    assert env["CASE_BIND"] == "127.0.0.1"
    assert "CASE_DOCKER_NETWORK" not in env
    assert "DESK_DEBUG" not in env
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CASE_NTFY_TOKEN" not in env
    assert env["DOCKER_HOST"] == "unix:///tmp/docker.sock"
    assert env["DOCKER_CERT_PATH"] == "/tmp/docker-certs"


def test_event_stream_is_not_consumed_by_response_capture():
    class Response:
        headers = {"content-type": "text/event-stream"}

        @property
        def text(self):
            raise AssertionError("stream consumed before the listener could read it")

    response = Response()
    with mock.patch.object(acceptance, "BASE", "http://127.0.0.1:43123/v1"), \
         mock.patch.object(acceptance, "CASE_TOKEN", "test-token"), \
         mock.patch.object(acceptance.requests, "request", return_value=response) as request:
        assert acceptance.api("GET", "/events", stream=True) is response
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert request.call_args.kwargs["stream"] is True


def test_cleanup_only_deletes_created_ids():
    calls = []

    class Response:
        status_code = 204
        text = ""

    def fake_api(method, path):
        calls.append((method, path))
        return Response()

    acceptance.cleanup_owned(fake_api, ("c_owned_a", "c_owned_b"), keep_id="c_owned_b")
    assert calls == [("DELETE", "/computers/c_owned_a")]


def test_cleanup_continues_after_a_delete_failure():
    calls = []

    class Response:
        status_code = 204
        text = ""

    def fake_api(method, path):
        calls.append((method, path))
        if path.endswith("c_owned_a"):
            raise RuntimeError("connection lost")
        return Response()

    failures = acceptance.cleanup_owned(fake_api, ("c_owned_a", "c_owned_b"))
    assert calls == [
        ("DELETE", "/computers/c_owned_a"),
        ("DELETE", "/computers/c_owned_b"),
    ]
    assert failures == ["c_owned_a: connection lost"]


def test_stop_owned_process_terminates_and_waits():
    class Process:
        def __init__(self):
            self.calls = []

        def poll(self):
            self.calls.append("poll")
            return None

        def terminate(self):
            self.calls.append("terminate")

        def wait(self, timeout):
            self.calls.append(("wait", timeout))

    proc = Process()
    assert acceptance.stop_owned_process(proc) == []
    assert proc.calls == ["poll", "terminate", ("wait", 20)]


def test_failures_retain_the_scratch_directory():
    assert acceptance.retain_scratch(None, True, False, [], []) is True
    assert acceptance.retain_scratch(None, False, True, [], []) is True
    assert acceptance.retain_scratch(None, False, False, ["delete failed"], []) is True
    assert acceptance.retain_scratch(None, False, False, [], ["stop failed"]) is True
    assert acceptance.retain_scratch(None, False, False, [], []) is False


if __name__ == "__main__":
    test_import_does_not_start_a_server()
    test_child_env_removes_case_and_provider_settings()
    test_event_stream_is_not_consumed_by_response_capture()
    test_cleanup_only_deletes_created_ids()
    test_cleanup_continues_after_a_delete_failure()
    test_stop_owned_process_terminates_and_waits()
    test_failures_retain_the_scratch_directory()
    print("test_acceptance_safety: ok")
