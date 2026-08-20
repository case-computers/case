# SPDX-License-Identifier: AGPL-3.0-only
"""Persistence + the Fernet credential vault.

This module is the single owner of the schema: every SQL string and every
column-order dependency lives here, behind domain methods. Callers pass and
receive rows/dicts, never SQL. Secrets are encrypted/decrypted only inside this
module — the vault boundary never leaks plaintext to callers except through
credential_material(), which login uses.
"""
import json
import os
import sqlite3
import threading

from cryptography.fernet import Fernet

from config import CASE_HOME
from util import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS computers (
  id TEXT PRIMARY KEY, name TEXT, state TEXT, image TEXT,
  created_at TEXT, last_active_at TEXT,
  cpus REAL, ram_mb INTEGER, volume TEXT,
  desk_port INTEGER, vnc_port INTEGER, desk_token TEXT
);
CREATE TABLE IF NOT EXISTS credentials (
  computer_id TEXT, name TEXT, username TEXT,
  secret BLOB, totp_seed BLOB, otp_phone TEXT,
  domains TEXT, created_at TEXT,
  last_verified_at TEXT, last_status TEXT,
  probe_url TEXT, proof_spec TEXT, verification_hosts TEXT,
  PRIMARY KEY (computer_id, name)
);
CREATE TABLE IF NOT EXISTS handoffs (
  id TEXT PRIMARY KEY, computer_id TEXT, kind TEXT, prompt TEXT,
  screenshot TEXT, status TEXT, answer TEXT, created_at TEXT,
  login_credential TEXT, domain TEXT,
  continuation TEXT, challenge_fingerprint TEXT,
  attempt_id TEXT, sequence INTEGER, revision INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_attempts (
  id TEXT PRIMARY KEY,
  computer_id TEXT NOT NULL,
  credential TEXT NOT NULL,
  status TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  target_url TEXT,
  proof_spec TEXT,
  idempotency_key TEXT,
  current_handoff_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_attempts_idempotency
  ON auth_attempts(computer_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_attempts_one_active
  ON auth_attempts(computer_id)
  WHERE status IN ('created','advancing','awaiting_human','proving');
CREATE INDEX IF NOT EXISTS idx_handoffs_computer_status ON handoffs(computer_id, status);
CREATE INDEX IF NOT EXISTS idx_handoffs_status_created ON handoffs(status, created_at);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, computer_id TEXT, name TEXT, prompt TEXT,
  kind TEXT, spec TEXT, jitter_s INTEGER, enabled INTEGER,
  next_run_at TEXT, last_run_at TEXT, last_status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, schedule_id TEXT, computer_id TEXT,
  started_at TEXT, ended_at TEXT, exit_code INTEGER, summary TEXT, artifact_path TEXT,
  status TEXT
);
CREATE TABLE IF NOT EXISTS links (
  token TEXT PRIMARY KEY, computer_id TEXT, kind TEXT,
  created_at TEXT, expires_at TEXT, used_at TEXT
);
CREATE TABLE IF NOT EXISTS assist_tokens (
  handoff_id TEXT PRIMARY KEY,
  token_hash TEXT UNIQUE NOT NULL,
  expires_at TEXT NOT NULL,
  burned_at TEXT,
  session_hash TEXT,
  session_expires_at TEXT
);
"""

ACTIVE_STATES = ("creating", "waking", "running")   # states that count against MAX_RUNNING


class Store:
    def __init__(self, home=None):
        home = home or CASE_HOME
        os.makedirs(home, exist_ok=True)
        key_path = os.path.join(home, "key")
        if not os.path.exists(key_path):
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, Fernet.generate_key())
            os.close(fd)
        with open(key_path, "rb") as f:
            self.fernet = Fernet(f.read())
        self.db = sqlite3.connect(os.path.join(home, "case.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.lock = threading.Lock()

    # (table, column, type), CREATE TABLE IF NOT EXISTS won't add columns to an
    # existing table, and a live box can carry a case.db older than every one of these.
    MIGRATIONS = [
        ("handoffs", "login_credential", "TEXT"),
        ("runs", "status", "TEXT"),
        ("handoffs", "domain", "TEXT"),
        ("credentials", "last_verified_at", "TEXT"),
        ("credentials", "last_status", "TEXT"),
        ("handoffs", "continuation", "TEXT"),
        ("handoffs", "challenge_fingerprint", "TEXT"),
        ("handoffs", "attempt_id", "TEXT"),
        ("handoffs", "sequence", "INTEGER"),
        ("handoffs", "revision", "INTEGER DEFAULT 0"),
        ("credentials", "probe_url", "TEXT"),
        ("credentials", "proof_spec", "TEXT"),
        ("credentials", "verification_hosts", "TEXT"),
    ]

    # Active (non-terminal) auth-attempt statuses, kept here so the partial unique
    # index and get_active_* share one definition with auth_attempts.ACTIVE_STATUSES.
    AUTH_ATTEMPT_ACTIVE = ("created", "advancing", "awaiting_human", "proving")

    def _migrate(self):
        for table, col, typ in self.MIGRATIONS:
            cols = [r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # Indexes are also in SCHEMA; re-assert for DBs created before they existed.
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_attempts_idempotency "
            "ON auth_attempts(computer_id, idempotency_key) "
            "WHERE idempotency_key IS NOT NULL")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_attempts_one_active "
            "ON auth_attempts(computer_id) "
            "WHERE status IN ('created','advancing','awaiting_human','proving')")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_handoffs_computer_status "
            "ON handoffs(computer_id, status)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_handoffs_status_created "
            "ON handoffs(status, created_at)")
        # Pre-migration handoff rows: treat missing revision as 0.
        self.db.execute("UPDATE handoffs SET revision=0 WHERE revision IS NULL")
        self.db.commit()

    # ---- low-level (module-internal) ----
    def q(self, sql, args=()):
        """Run one statement under the store lock; non-SELECTs auto-commit."""
        with self.lock:
            cur = self.db.execute(sql, args)
            if not sql.lstrip()[:6].upper().startswith("SELECT"):
                self.db.commit()
            return cur

    def one(self, sql, args=()):
        return self.q(sql, args).fetchone()

    def all(self, sql, args=()):
        return self.q(sql, args).fetchall()

    # ---- vault ----
    def enc(self, s):
        """Fernet-encrypt a secret for storage. None stays None."""
        return self.fernet.encrypt(s.encode()) if s is not None else None

    def dec(self, b):
        """Decrypt a stored secret. Callers outside this module should not need
        this — credential_material() is the sanctioned plaintext exit."""
        return self.fernet.decrypt(b).decode() if b is not None else None

    # ---- computers ----
    def get_computer(self, cid):
        return self.one("SELECT * FROM computers WHERE id=?", (cid,))

    def list_computers(self):
        return self.all("SELECT * FROM computers WHERE state != 'deleted' ORDER BY created_at")

    def all_non_deleted(self):
        return self.all("SELECT * FROM computers WHERE state != 'deleted'")

    def running_rows(self):
        return self.all("SELECT * FROM computers WHERE state='running'")

    def active_count(self):
        qs = ",".join("?" * len(ACTIVE_STATES))
        return self.one(f"SELECT COUNT(*) c FROM computers WHERE state IN ({qs})", ACTIVE_STATES)["c"]

    def active_ram_mb(self):
        """RAM committed to awake desktops. The count cap stops being a memory
        guard the moment computers are not all the same size."""
        qs = ",".join("?" * len(ACTIVE_STATES))
        row = self.one(f"SELECT COALESCE(SUM(ram_mb),0) r FROM computers WHERE state IN ({qs})",
                       ACTIVE_STATES)
        return int(row["r"] or 0)

    def computer_name(self, cid):
        """Display name for a foreign key, falling back to the id. Runs and credentials
        outlive the computer they belong to, so this must never raise."""
        row = self.one("SELECT name FROM computers WHERE id=?", (cid,))
        return row["name"] if row else cid

    def computer_names(self):
        """{id: name} for every computer, so a list route resolves its owners in one
        query instead of one per row."""
        return {r["id"]: r["name"] for r in self.all("SELECT id, name FROM computers")}

    def insert_computer(self, cid, name, image, cpus, ram_mb, volume, token):
        ts = now()
        self.q("INSERT INTO computers (id,name,state,image,created_at,last_active_at,cpus,ram_mb,"
               "volume,desk_port,vnc_port,desk_token) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (cid, name, "creating", image, ts, ts, cpus, ram_mb, volume, 0, 0, token))

    def set_ports(self, cid, desk_port, vnc_port):
        self.q("UPDATE computers SET desk_port=?, vnc_port=? WHERE id=?", (desk_port, vnc_port, cid))

    def set_state(self, cid, to, expect=None):
        """Write the state. If `expect` is given, the write is conditional on the row
        still being in that state (compare-and-set), returns rows affected, so callers
        can detect a concurrent change instead of silently clobbering it."""
        if expect is None:
            return self.q("UPDATE computers SET state=? WHERE id=?", (to, cid)).rowcount
        return self.q("UPDATE computers SET state=? WHERE id=? AND state=?",
                      (to, cid, expect)).rowcount

    def touch(self, cid):
        self.q("UPDATE computers SET last_active_at=? WHERE id=?", (now(), cid))

    def delete_computer(self, cid):
        self.q("DELETE FROM computers WHERE id=?", (cid,))

    # ---- credentials ----
    _PROFILE_UNSET = object()

    def upsert_credential(self, cid, name, username, secret, totp_seed, otp_phone, domains,
                          probe_url=_PROFILE_UNSET, proof_spec=_PROFILE_UNSET,
                          verification_hosts=_PROFILE_UNSET):
        """Write vault material. Auth-profile columns are preserved unless explicitly passed.

        INSERT OR REPLACE used to wipe probe_url/proof_spec/verification_hosts on every
        password update, callers that omit those fields keep the prior profile.
        """
        existing = self.get_credential(cid, name)
        if probe_url is self._PROFILE_UNSET:
            probe_url = existing["probe_url"] if existing and "probe_url" in existing.keys() else None
        if proof_spec is self._PROFILE_UNSET:
            proof_spec = existing["proof_spec"] if existing and "proof_spec" in existing.keys() else None
        else:
            if isinstance(proof_spec, dict):
                proof_spec = json.dumps(proof_spec)
        if verification_hosts is self._PROFILE_UNSET:
            verification_hosts = (existing["verification_hosts"]
                                  if existing and "verification_hosts" in existing.keys() else None)
        else:
            if isinstance(verification_hosts, list):
                verification_hosts = json.dumps(verification_hosts)
        created = existing["created_at"] if existing else now()
        last_verified = existing["last_verified_at"] if existing else None
        last_status = existing["last_status"] if existing else None
        self.q("INSERT OR REPLACE INTO credentials "
               "(computer_id,name,username,secret,totp_seed,otp_phone,domains,created_at,"
               "last_verified_at,last_status,probe_url,proof_spec,verification_hosts) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (cid, name, username, self.enc(secret), self.enc(totp_seed), otp_phone,
                json.dumps(domains), created, last_verified, last_status,
                probe_url, proof_spec, verification_hosts))

    def record_credential_result(self, cid, name, status):
        """Credential health without a nightly checker: every login already
        produces a definitive answer, so keep the last one instead of discarding it."""
        self.q("UPDATE credentials SET last_verified_at=?, last_status=? "
               "WHERE computer_id=? AND name=?", (now(), status, cid, name))

    def get_credential(self, cid, name):
        return self.one("SELECT * FROM credentials WHERE computer_id=? AND name=?", (cid, name))

    def list_credentials(self, cid):
        return self.all("SELECT * FROM credentials WHERE computer_id=? ORDER BY name", (cid,))

    def list_all_credentials(self):
        return self.all("SELECT * FROM credentials ORDER BY computer_id, name")

    def credential_names(self, cid):
        return [r["name"] for r in
                self.all("SELECT name FROM credentials WHERE computer_id=? ORDER BY name", (cid,))]

    def delete_credential(self, cid, name):
        return self.q("DELETE FROM credentials WHERE computer_id=? AND name=?", (cid, name)).rowcount

    def delete_credentials(self, cid):
        self.q("DELETE FROM credentials WHERE computer_id=?", (cid,))

    def credential_material(self, cid, name):
        """Decrypted secret bundle for login. The only path plaintext leaves the vault."""
        row = self.get_credential(cid, name)
        if not row:
            return None
        return {"name": row["name"], "username": row["username"],
                "secret": self.dec(row["secret"]), "totp_seed": self.dec(row["totp_seed"]),
                "otp_phone": row["otp_phone"], "domains": json.loads(row["domains"])}

    # ---- handoffs ----
    def insert_handoff(self, hid, cid, kind, prompt, screenshot, login_credential, domain=None,
                       continuation=None, challenge_fingerprint=None, attempt_id=None,
                       sequence=None, revision=0):
        self.q("INSERT INTO handoffs (id,computer_id,kind,prompt,screenshot,status,answer,"
               "created_at,login_credential,domain,continuation,challenge_fingerprint,"
               "attempt_id,sequence,revision) "
               "VALUES (?,?,?,?,?,'pending',NULL,?,?,?,?,?,?,?,?)",
               (hid, cid, kind, prompt, screenshot, now(),
                login_credential, domain, continuation, challenge_fingerprint,
                attempt_id, sequence, revision))

    def pending_login_handoffs(self):
        # pending + validating: a restart mid-verify must still recover LOGIN_CTX
        return self.all("SELECT * FROM handoffs WHERE status IN ('pending','validating') "
                        "AND login_credential IS NOT NULL")

    def get_handoff(self, hid):
        return self.one("SELECT * FROM handoffs WHERE id=?", (hid,))

    def get_open_handoff_by_fingerprint(self, cid, fingerprint):
        return self.one(
            "SELECT * FROM handoffs WHERE computer_id=? AND challenge_fingerprint=? "
            "AND status IN ('pending','validating') ORDER BY created_at DESC LIMIT 1",
            (cid, fingerprint))

    def list_handoffs(self, status=None):
        if status == "completed":
            # one-release compat: legacy rows wrote 'answered'
            return self.all("SELECT * FROM handoffs WHERE status IN ('completed','answered') "
                            "ORDER BY created_at DESC")
        if status:
            return self.all("SELECT * FROM handoffs WHERE status=? ORDER BY created_at DESC", (status,))
        return self.all("SELECT * FROM handoffs ORDER BY created_at DESC")

    def list_handoffs_for(self, cid):
        return self.all("SELECT * FROM handoffs WHERE computer_id=? ORDER BY created_at DESC", (cid,))

    def pending_handoff_count(self, cid):
        return self.one("SELECT COUNT(*) c FROM handoffs WHERE computer_id=? AND status='pending'",
                        (cid,))["c"]

    def pending_handoff_ids(self):
        return [r["id"] for r in self.all("SELECT id FROM handoffs WHERE status='pending'")]

    def stale_pending_handoffs(self, cutoff):
        """Open handoffs older than `cutoff`, for the TTL sweeper.
        validating that never finished (restart mid-verify) also ages out."""
        return self.all("SELECT * FROM handoffs WHERE status IN ('pending','validating') "
                        "AND created_at < ?", (cutoff,))

    def delete_handoff(self, hid):
        self.q("DELETE FROM handoffs WHERE id=?", (hid,))

    _ANSWER_UNCHANGED = object()

    def set_handoff_status(self, hid, status, answer=_ANSWER_UNCHANGED):
        """Update status. answer omitted → leave unchanged; answer=None → clear; else set."""
        if answer is self._ANSWER_UNCHANGED:
            self.q("UPDATE handoffs SET status=? WHERE id=?", (status, hid))
        elif answer is None:
            self.q("UPDATE handoffs SET status=?, answer=NULL WHERE id=?", (status, hid))
        else:
            self.q("UPDATE handoffs SET status=?, answer=? WHERE id=?", (status, answer, hid))

    def cas_handoff_status(self, hid, from_status, to_status, revision_expect, answer=_ANSWER_UNCHANGED):
        """Compare-and-set handoff status + bump revision. rowcount 1 = won the race."""
        if answer is self._ANSWER_UNCHANGED:
            return self.q(
                "UPDATE handoffs SET status=?, revision=COALESCE(revision,0)+1 "
                "WHERE id=? AND status=? AND COALESCE(revision,0)=?",
                (to_status, hid, from_status, revision_expect)).rowcount
        if answer is None:
            return self.q(
                "UPDATE handoffs SET status=?, answer=NULL, revision=COALESCE(revision,0)+1 "
                "WHERE id=? AND status=? AND COALESCE(revision,0)=?",
                (to_status, hid, from_status, revision_expect)).rowcount
        return self.q(
            "UPDATE handoffs SET status=?, answer=?, revision=COALESCE(revision,0)+1 "
            "WHERE id=? AND status=? AND COALESCE(revision,0)=?",
            (to_status, answer, hid, from_status, revision_expect)).rowcount

    # ---- auth attempts (durable login workflow; MCP carries no session state) ----
    def insert_auth_attempt(self, aid, computer_id, credential, target_url,
                            proof_spec=None, idempotency_key=None, status="created"):
        ts = now()
        spec = json.dumps(proof_spec) if proof_spec is not None and not isinstance(proof_spec, str) \
            else proof_spec
        self.q("INSERT INTO auth_attempts (id,computer_id,credential,status,revision,target_url,"
               "proof_spec,idempotency_key,current_handoff_id,created_at,updated_at) "
               "VALUES (?,?,?,?,0,?,?,?,NULL,?,?)",
               (aid, computer_id, credential, status, target_url, spec, idempotency_key, ts, ts))

    def get_auth_attempt(self, aid):
        return self.one("SELECT * FROM auth_attempts WHERE id=?", (aid,))

    def get_active_auth_attempt(self, computer_id):
        """The newest non-terminal attempt on this computer, or None. At most one
        should exist — login 409s while one is active."""
        qs = ",".join("?" * len(self.AUTH_ATTEMPT_ACTIVE))
        return self.one(
            f"SELECT * FROM auth_attempts WHERE computer_id=? AND status IN ({qs}) "
            "ORDER BY created_at DESC LIMIT 1",
            (computer_id, *self.AUTH_ATTEMPT_ACTIVE))

    def get_auth_attempt_by_idempotency(self, computer_id, idempotency_key):
        if not idempotency_key:
            return None
        return self.one(
            "SELECT * FROM auth_attempts WHERE computer_id=? AND idempotency_key=?",
            (computer_id, idempotency_key))

    def cas_auth_attempt_status(self, aid, from_status, to_status, revision_expect):
        """Compare-and-set status + bump revision. rowcount 1 = this caller won."""
        return self.q(
            "UPDATE auth_attempts SET status=?, revision=revision+1, updated_at=? "
            "WHERE id=? AND status=? AND revision=?",
            (to_status, now(), aid, from_status, revision_expect)).rowcount

    def set_attempt_handoff(self, aid, handoff_id):
        return self.q(
            "UPDATE auth_attempts SET current_handoff_id=?, updated_at=? WHERE id=?",
            (handoff_id, now(), aid)).rowcount

    def next_handoff_sequence(self, attempt_id):
        row = self.one(
            "SELECT COALESCE(MAX(sequence), 0) AS m FROM handoffs WHERE attempt_id=?",
            (attempt_id,))
        return int(row["m"] if row else 0) + 1

    def list_auth_attempts_for(self, computer_id, limit=50):
        limit = max(1, min(int(limit), 200))
        return self.all(
            "SELECT * FROM auth_attempts WHERE computer_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (computer_id, limit))

    def active_attempt_exists(self, computer_id):
        return self.get_active_auth_attempt(computer_id) is not None

    # ---- links (human one-time / short-TTL URLs: /fill, /desk) ----
    def insert_link(self, token, cid, kind, expires_at):
        self.q("INSERT INTO links (token,computer_id,kind,created_at,expires_at,used_at) "
               "VALUES (?,?,?,?,?,NULL)", (token, cid, kind, now(), expires_at))

    def get_link(self, token):
        return self.one("SELECT * FROM links WHERE token=?", (token,))

    def burn_link(self, token):
        """Compare-and-set: rowcount 1 means *this* call burned it, 0 means someone
        else already did. Single-use has to be decided by the write, not by a read."""
        return self.q("UPDATE links SET used_at=? WHERE token=? AND used_at IS NULL",
                      (now(), token)).rowcount

    def prune_expired_links(self):
        """Drop tokens that valid() would already reject. `<=` matches links.valid."""
        return self.q("DELETE FROM links WHERE expires_at <= ?", (now(),)).rowcount

    # ---- assist tokens (hashed exchange → session cookie; scoped to one handoff) ----
    def insert_assist_token(self, handoff_id, token_hash, expires_at):
        self.q("INSERT INTO assist_tokens (handoff_id,token_hash,expires_at,burned_at,"
               "session_hash,session_expires_at) VALUES (?,?,?,NULL,NULL,NULL)",
               (handoff_id, token_hash, expires_at))

    def get_assist_by_token_hash(self, token_hash):
        return self.one("SELECT * FROM assist_tokens WHERE token_hash=?", (token_hash,))

    def get_assist_by_session_hash(self, session_hash):
        return self.one("SELECT * FROM assist_tokens WHERE session_hash=?", (session_hash,))

    def get_assist_by_handoff(self, handoff_id):
        return self.one("SELECT * FROM assist_tokens WHERE handoff_id=?", (handoff_id,))

    def burn_assist_exchange(self, token_hash, session_hash, session_expires_at):
        """Compare-and-set: burn the emailed exchange token and attach a session hash.
        rowcount 1 = this call won; 0 = already burned or unknown."""
        return self.q("UPDATE assist_tokens SET burned_at=?, session_hash=?, "
                      "session_expires_at=? WHERE token_hash=? AND burned_at IS NULL",
                      (now(), session_hash, session_expires_at, token_hash)).rowcount

    def prune_expired_assist_tokens(self):
        """Drop exchange rows only when the emailed link and the session are both dead.
        A burned 15-minute link with a live 30-minute session must survive."""
        ts = now()
        return self.q(
            "DELETE FROM assist_tokens WHERE expires_at <= ? "
            "AND (session_expires_at IS NULL OR session_expires_at <= ?)",
            (ts, ts)).rowcount

    # ---- schedules ----
    def insert_schedule(self, sid, cid, name, prompt, kind, spec, jitter_s, next_run_at):
        self.q("INSERT INTO schedules (id,computer_id,name,prompt,kind,spec,jitter_s,enabled,"
               "next_run_at,last_run_at,last_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (sid, cid, name, prompt, kind, spec, jitter_s, 1, next_run_at, None, None, now()))

    def get_schedule(self, sid, enabled_only=False):
        if enabled_only:
            return self.one("SELECT * FROM schedules WHERE id=? AND enabled=1", (sid,))
        return self.one("SELECT * FROM schedules WHERE id=?", (sid,))

    def list_schedules(self, cid):
        return self.all("SELECT * FROM schedules WHERE computer_id=? ORDER BY created_at", (cid,))

    def delete_schedule(self, sid):
        return self.q("DELETE FROM schedules WHERE id=?", (sid,)).rowcount

    def set_schedule_next(self, sid, next_run_at):
        self.q("UPDATE schedules SET next_run_at=? WHERE id=?", (next_run_at, sid))

    def set_schedule_result(self, sid, last_run_at, last_status):
        self.q("UPDATE schedules SET last_run_at=?, last_status=? WHERE id=?",
               (last_run_at, last_status, sid))

    def schedule_summary(self, cid):
        """(task count, soonest next_run_at) for the console's COMPUTERS row. MIN over
        the stored strings is chronological: compute_next writes zero-padded UTC-Z."""
        row = self.one("SELECT COUNT(*) AS n, MIN(next_run_at) AS nxt FROM schedules "
                       "WHERE computer_id=? AND enabled=1", (cid,))
        return (row["n"] or 0), row["nxt"]

    def schedule_summaries(self):
        return {r["computer_id"]: ((r["n"] or 0), r["nxt"]) for r in self.all(
            "SELECT computer_id, COUNT(*) n, MIN(next_run_at) nxt FROM schedules "
            "WHERE enabled=1 GROUP BY computer_id")}

    def credential_names_by_computer(self):
        out = {}
        for r in self.all("SELECT computer_id, name FROM credentials ORDER BY name"):
            out.setdefault(r["computer_id"], []).append(r["name"])
        return out

    def pending_handoff_counts(self):
        return {r["computer_id"]: r["c"] for r in self.all(
            "SELECT computer_id, COUNT(*) c FROM handoffs WHERE status='pending' "
            "GROUP BY computer_id")}

    def due_schedules(self, at):
        return self.all("SELECT id FROM schedules WHERE enabled=1 AND next_run_at IS NOT NULL "
                        "AND next_run_at <= ?", (at,))

    # ---- runs ----
    def insert_run(self, rid, sid, cid, started_at, ended_at, exit_code, summary,
                   artifact_path, status):
        self.q("INSERT INTO runs (id,schedule_id,computer_id,started_at,ended_at,exit_code,"
               "summary,artifact_path,status) VALUES (?,?,?,?,?,?,?,?,?)",
               (rid, sid, cid, started_at, ended_at, exit_code, summary, artifact_path, status))

    def list_runs(self, sid, limit=50):
        return self.all("SELECT * FROM runs WHERE schedule_id=? ORDER BY started_at DESC LIMIT ?",
                        (sid, limit))

    def list_all_runs(self, limit=50):
        return self.all("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))

    def get_run(self, rid):
        return self.one("SELECT * FROM runs WHERE id=?", (rid,))

    def prune_terminal_handoffs(self, cutoff):
        self.q("UPDATE handoffs SET screenshot=NULL WHERE screenshot IS NOT NULL "
               "AND status IN ('completed','answered','failed','expired')")
        return self.q("DELETE FROM handoffs WHERE status IN "
                      "('completed','answered','failed','expired') AND created_at < ?",
                      (cutoff,)).rowcount

    def prune_old_runs(self, keep=1000):
        """Cap run history. Returns artifact_path values of deleted rows."""
        keep = max(1, int(keep))
        rows = self.all("SELECT id, artifact_path FROM runs ORDER BY started_at DESC")
        drop = rows[keep:]
        if not drop:
            return []
        ids = [r["id"] for r in drop]
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            self.q(f"DELETE FROM runs WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        return [r["artifact_path"] for r in drop if r["artifact_path"]]


store = Store()
