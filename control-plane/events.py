# SPDX-License-Identifier: AGPL-3.0-only
"""Server-Sent Events fan-out.

emit() is safe to call from any thread (the sweeper/blocker/login-resume threads all
do): it hops onto the asyncio loop (set once at startup via set_loop()) to feed
each subscriber queue. sse_gen() is the per-connection generator.
subscribe()/unsubscribe() are the shared primitives for SSE and auth-attempt long-poll.
"""
import asyncio
import json

LOOP = None      # main asyncio loop, set at startup
SUBS = []        # live subscriber queues
QUEUE_MAX = 256  # a subscriber this far behind has stalled; it is dropped, not buffered


def set_loop(loop):
    global LOOP
    LOOP = loop


def emit(type_, data):
    if LOOP:
        for q in list(SUBS):
            try:
                LOOP.call_soon_threadsafe(_offer, q, (type_, data))
            except RuntimeError:     # loop closed (shutdown): nobody is listening
                return


def _offer(q, item):
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        unsubscribe(q)               # sse_gen hangs up on it at its next heartbeat


def subscribe():
    """Register a subscriber queue. Caller must unsubscribe() in finally."""
    q = asyncio.Queue(maxsize=QUEUE_MAX)
    SUBS.append(q)
    return q


def unsubscribe(q):
    try:
        SUBS.remove(q)
    except ValueError:
        pass


async def sse_gen(computer_id=None):
    q = subscribe()
    try:
        yield ": connected\n\n"
        while True:
            try:
                type_, data = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                if q not in SUBS:
                    return           # dropped as stalled; the client reconnects
                yield ": hb\n\n"
                continue
            if computer_id and data.get("computer_id") != computer_id:
                continue
            yield f"event: {type_}\ndata: {json.dumps(data)}\n\n"
    finally:
        unsubscribe(q)
