# SPDX-License-Identifier: AGPL-3.0-only
"""Environment-derived configuration + the shared logger.

A leaf module: anything may import it, it imports nothing of ours.
"""
import logging
import os
from datetime import timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("cased")

IMAGE = os.environ.get("CASE_IMAGE", "case-desk:0.1")
MAX_RUNNING = int(os.environ.get("CASE_MAX_RUNNING", "8"))


def _host_ram_budget_mb():
    """75% of the host's RAM, or 0 (no budget) where we cannot read it.

    MAX_RUNNING caps how *many* desktops are awake, which is only a stand-in for
    memory while every desktop is the default 2 GB. Once a caller can ask for
    8 GB, a count means nothing and the box OOMs instead of returning 409. So the
    real guard is a memory budget, and it has to default to something sane
    because nobody sets an env var they have never heard of.

    /proc/meminfo is the container's view of the host (or, on a Mac, of the
    Docker VM), which is exactly the ceiling we care about. macOS has no /proc,
    so cased running natively there gets 0 and behaves as it always has.
    """
    try:
        with open("/proc/meminfo") as f:
            total_kb = int(f.readline().split()[1])
        return int(total_kb / 1024 * 0.75)
    except Exception:
        return 0


MAX_RAM_MB = int(os.environ.get("CASE_MAX_RAM_MB") or _host_ram_budget_mb())
# Sanity rails for caller-supplied sizing (the create form is a trust boundary).
MIN_RAM_MB, MAX_COMPUTER_RAM_MB = 512, 65536
MIN_CPUS, MAX_CPUS = 0.25, 32.0
# Default: loopback. Compose sets CASE_BIND=0.0.0.0 and publishes
# 127.0.0.1:8787 on the host so the API is laptop-only unless they change ports.
BIND_HOST = os.environ.get("CASE_BIND", "127.0.0.1") or "127.0.0.1"
BIND_PORT = int(os.environ.get("CASE_PORT") or 8787)
# CASE_VNC_PORT pins the desktop's noVNC to a fixed host port so a reverse proxy
# can serve /desk/* from it. 0/unset = ephemeral port per container (local dev).
# `or 0` first: an env file line `CASE_VNC_PORT=` (empty) would otherwise ValueError at
# import and restart-loop cased with only a traceback to explain it.
VNC_PORT = int(os.environ.get("CASE_VNC_PORT") or 0) or None
API_BASE = os.environ.get("CASE_API_BASE", f"http://127.0.0.1:{BIND_PORT}/v1")
HANDOFF_TTL = timedelta(minutes=15)

# Brain = headless Claude Code, invoked by the scheduler. BYOK stays with the caller
# (Case never owns LLM cost): an ANTHROPIC_API_KEY exported for cased is honoured
# (their key, their bill); with none set the scheduler blanks it so the box's
# logged-in/subscription auth is used. CASE_BRAIN_BIN must point at the real binary, a
# shell alias (e.g. `ANTHROPIC_API_KEY= claude`) is invisible to subprocess.
BRAIN_BIN = os.environ.get("CASE_BRAIN_BIN", "")
# Optional full-command template for the schedule brain (any headless harness,
# e.g. codex). Split with shlex, then per token {mcp}→config, {prompt}→prompt, so a
# prompt with spaces stays one argv element and its text is never re-scanned. Must
# contain {prompt}. Unset = stock claude. The template carries no --allowedTools clamp:
# a template harness runs with the box's full privileges, only use one you trust.
BRAIN_CMD = os.environ.get("CASE_BRAIN_CMD", "")
MCP_CONFIG = os.environ.get(
    "CASE_MCP_CONFIG",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "case-mcp.json"))
BRAIN_TIMEOUT = int(os.environ.get("CASE_BRAIN_TIMEOUT", "1800"))

CASE_HOME = os.environ.get("CASE_HOME", os.path.expanduser("~/.case"))
RUNS_DIR = os.path.join(CASE_HOME, "runs")   # per-run screenshot artifacts (host side)
AUDIT_DIR = os.path.join(CASE_HOME, "audit")  # per-day JSONL of every API call
