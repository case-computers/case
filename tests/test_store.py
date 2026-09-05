# SPDX-License-Identifier: MIT
"""Concurrent reads from the shared vault connection. No Docker required."""
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import sys
import tempfile
import threading

_HOME = tempfile.mkdtemp(prefix="case-store-test-")
os.environ["CASE_HOME"] = _HOME
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control-plane"))
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


if __name__ == "__main__":
    try:
        test_concurrent_reads_keep_their_rows_intact()
        print("test_store: ok")
    finally:
        store.db.close()
        shutil.rmtree(_HOME)
