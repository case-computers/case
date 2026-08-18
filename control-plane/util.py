# SPDX-License-Identifier: AGPL-3.0-only
"""Tiny shared primitives with no dependencies of their own."""
import secrets
from datetime import datetime, timedelta, timezone


def now():
    """UTC ISO-8601, second precision, always zero-padded + 'Z' so lexicographic
    string order equals chronological order (relied on by the scheduler)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix, nbytes=5):
    return f"{prefix}_{secrets.token_hex(nbytes)}"


def row_get(row, key, default=None):
    if row is None:
        return default
    if hasattr(row, "keys"):
        return row[key] if key in row.keys() else default
    return row.get(key, default)


def iso_in(ttl_s):
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
