# SPDX-License-Identifier: MIT
"""dockerd network vs loopback reachability. Pure — no Docker daemon.
Run: .venv/bin/python tests/test_dockerd.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
os.environ["CASE_HOME"] = "/tmp/case-dockerd-test"
os.environ.pop("CASE_DOCKER_NETWORK", None)
import dockerd  # noqa: E402


def _net(val):
    if val is None:
        os.environ.pop("CASE_DOCKER_NETWORK", None)
    else:
        os.environ["CASE_DOCKER_NETWORK"] = val


def test_host_mode_dials_loopback_and_publishes_ports():
    _net(None)
    assert dockerd.desk_host("c_ab") == "127.0.0.1"
    assert dockerd.desk_base("c_ab", 32771) == "http://127.0.0.1:32771"
    kw = dockerd.container_run_kwargs("c_ab", 1, 2048, "vol", "tok")
    assert "network" not in kw
    assert kw["ports"]["8000/tcp"][0] == "127.0.0.1"
    assert kw["name"] == "case-c_ab"


def test_compose_mode_uses_container_dns_and_no_host_ports():
    _net("case")
    try:
        assert dockerd.desk_host("c_ab") == "case-c_ab"
        assert dockerd.desk_base("c_ab", 8000) == "http://case-c_ab:8000"
        kw = dockerd.container_run_kwargs("c_ab", 1, 2048, "vol", "tok")
        assert kw["network"] == "case"
        assert "ports" not in kw
        assert dockerd.container_ports(None) == (8000, 6080)
    finally:
        _net(None)


def test_deskclient_accepts_sqlite_row():
    # store hands sqlite3.Row to every deskd call (snapshot, screenshot, eval).
    # Row has no .get — desk() used to AttributeError on GET /page.
    import sqlite3
    import deskclient
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE c (id TEXT, desk_port INT, desk_token TEXT)")
    db.execute("INSERT INTO c VALUES ('c_ab', 32771, 'tok')")
    row = db.execute("SELECT * FROM c").fetchone()
    assert not hasattr(row, "get")
    seen = {}

    class R:
        status_code = 200
        def json(self):
            return {"ok": True}

    orig = deskclient.requests.request
    deskclient.requests.request = lambda method, url, headers=None, timeout=None, **kw: (
        seen.update(url=url, headers=headers) or R())
    try:
        deskclient.desk(row, "GET", "/health")
        assert seen["url"] == "http://127.0.0.1:32771/health"
        assert seen["headers"]["Authorization"] == "Bearer tok"
    finally:
        deskclient.requests.request = orig


def test_deskclient_url_follows_the_network():
    # desk() builds the URL before the HTTP call — capture it via a stub.
    seen = {}

    class R:
        status_code = 200
        content = b"{}"
        def json(self):
            return {}

    def fake_request(method, url, headers=None, timeout=None, **kw):
        seen["url"] = url
        seen["headers"] = headers
        return R()

    import deskclient
    orig = deskclient.requests.request
    deskclient.requests.request = fake_request
    _net("case")
    try:
        deskclient.desk({"id": "c_ab", "desk_port": 8000, "desk_token": "secret"},
                        "GET", "/health")
        assert seen["url"] == "http://case-c_ab:8000/health"
        assert seen["headers"]["Authorization"] == "Bearer secret"
    finally:
        deskclient.requests.request = orig
        _net(None)


def test_vnc_url_hidden_when_computers_are_on_the_compose_network():
    from lifecycle import _vnc_url
    row = {"state": "running", "vnc_port": 6080}
    _net(None)
    assert _vnc_url(row) == "http://127.0.0.1:6080/vnc.html"
    _net("case")
    try:
        assert _vnc_url(row) is None
    finally:
        _net(None)


if __name__ == "__main__":
    test_host_mode_dials_loopback_and_publishes_ports()
    test_compose_mode_uses_container_dns_and_no_host_ports()
    test_deskclient_accepts_sqlite_row()
    test_deskclient_url_follows_the_network()
    test_vnc_url_hidden_when_computers_are_on_the_compose_network()
    print("test_dockerd: ok")
