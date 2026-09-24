# SPDX-License-Identifier: MIT
"""Concurrent reads from the shared vault connection. No Docker required."""
from concurrent.futures import ThreadPoolExecutor
import shutil
import threading
import unittest.mock as mock

import _helpers

_HOME = _helpers.isolated_home()
from store import store  # noqa: E402


def test_concurrent_reads_keep_their_rows_intact():
    for i in range(2):
        cid = f"c_read{i}"
        store.insert_computer(cid, cid, "test", 1, 2048, "test-volume", "test-token")
        store.insert_auth_attempt(f"a_read{i}", cid, "test", "https://example.com")
    expected = {f"c_read{i}": dict(store.get_computer(f"c_read{i}")) for i in range(2)}
    ready = threading.Barrier(16)

    def read(worker):
        ready.wait()
        cid = f"c_read{worker % 2}"
        for _ in range(250):
            assert dict(store.get_computer(cid)) == expected[cid]
            rows = store.all("SELECT * FROM computers ORDER BY id")
            assert [dict(r) for r in rows] == list(expected.values())
            attempts = store.stale_active_auth_attempts("9999-01-01T00:00:00Z")
            assert sorted(a["id"] for a in attempts) == ["a_read0", "a_read1"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(read, range(16)))


def _plan(query):
    """The query plan of the one statement `query()` runs."""
    seen = []
    with mock.patch.object(store, "one", side_effect=lambda sql, args=(): seen.append((sql, args))), \
         mock.patch.object(store, "all", side_effect=lambda sql, args=(): seen.append((sql, args))):
        query()
    (sql, args), = seen
    return " ".join(r["detail"] for r in store.db.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_active_attempt_queries_use_an_index():
    plan = _plan(lambda: store.get_active_auth_attempt("c_plan"))
    assert "idx_auth_attempts_one_active" in plan, plan
    plan = _plan(lambda: store.stale_active_auth_attempts("2000-01-01T00:00:00Z"))
    assert "SEARCH" in plan, plan


def test_prune_terminal_auth_attempts_keeps_active_and_recent_rows():
    old = "2000-01-01T00:00:00Z"
    for aid, status in (("a_old_done", "failed"), ("a_old_live", "awaiting_human"),
                        ("a_new_done", "authenticated")):
        store.insert_auth_attempt(aid, "c_prune", "test", "https://example.com", status=status)
    store.q("UPDATE auth_attempts SET updated_at=? WHERE id IN ('a_old_done','a_old_live')",
            (old,))
    assert store.prune_terminal_auth_attempts("2001-01-01T00:00:00Z") == 1
    left = {r["id"] for r in store.all("SELECT id FROM auth_attempts WHERE computer_id='c_prune'")}
    assert left == {"a_old_live", "a_new_done"}, left


if __name__ == "__main__":
    try:
        test_concurrent_reads_keep_their_rows_intact()
        test_active_attempt_queries_use_an_index()
        test_prune_terminal_auth_attempts_keeps_active_and_recent_rows()
        print("test_store: ok")
    finally:
        store.db.close()
        shutil.rmtree(_HOME)
