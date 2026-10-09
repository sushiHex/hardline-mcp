"""Wake a running agent session with one constant notice at a time.

The core of Codex queue-wake (``codex_queue``, docs/codex-queue-wake.md) and
Claude inbox-wake (``claude_inbox``, docs/claude-inbox-wake.md), beside the
channel pusher in the same serving loop. A subclass says which client it
serves, how a notice is worded, how it is sent, and - when it is not known at
startup - how the address is found.

The notice is an idempotent pointer: constant text that tells the session to
read its hardline inbox, with a receipt nonce as its only variable. A stale or
duplicated notice only points at an inbox, so the transport never has to be
tracked exactly: one notice is outstanding at a time, released only by its own
receipt. New mail is announced once; deferred mail is not re-announced.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

import anyio

from . import adapters, channel, delivery, procid, sessions


class NotSent(Exception):
    """The transport delivered nothing - the address could not be reached.

    Any other failure might have delivered the notice, so it stays outstanding.
    """


class Announcer:
    """Wake state for this process's one connection of a ``client``."""

    client = ""  # the clientInfo.name served
    transport = ""  # reported beside the delivery state

    def __init__(
        self,
        *,
        address: object = None,
        fulfil: Optional[Callable[[], list[str]]] = None,
        clock: Callable[[], datetime] = channel._now,
        poll_s: float = channel.POLL_S,
    ):
        self.address = address  # given at startup, or pinned by a subclass
        self.fulfil = fulfil
        self.clock = clock
        self.poll_s = poll_s
        self.stopped = False  # set once, never cleared: no further notices
        self.outstanding: Optional[tuple[str, datetime]] = None  # (nonce, sent at)
        self.announced: set[int] = set()
        self.facts: dict = {}
        self._previous_push: Optional[datetime] = None  # restored by take_back
        self._dirty = False
        self._pruned = False
        self._cursor = 0
        self._last_fulfil: Optional[datetime] = None
        self._notes: list[str] = []  # logged by the poll thread, never the loop
        # Evidence arrives on the event loop and receipts on tool threads, so
        # both only touch memory, under this lock; the poll thread alone does
        # I/O - facts and logging - so writes need no ordering.
        self._lock = threading.Lock()

    def notice(self, nonce: str) -> str:
        raise NotImplementedError

    def send(self, address, text: str) -> None:
        """Deliver ``text``. Raises ``NotSent`` if nothing was delivered; any
        other failure may have delivered it."""
        raise NotImplementedError

    def arm(self, client: dict) -> None:
        """Called once the client is known to be this one's. Memory only."""

    # ── evidence ─────────────────────────────────────────────────────────────

    def declare(self) -> None:
        """The address is known: from here the session reads ``declared``.
        Caller holds ``_lock``."""
        self.facts = {"declared_at": self.clock()}
        self._dirty = True

    def accept_receipt(self, nonce: str) -> bool:
        """A receipt proves the notice that carried it - that one only.

        The only thing that releases a notice: a call made by the turn a
        notice started is no proof, since one turn makes many calls, and a
        call made after the next notice went out would release that one.
        """
        with self._lock:
            if self.outstanding is None or self.outstanding[0] != nonce:
                return False
            self.facts["last_receipted_push_at"] = self.outstanding[1]
            self.facts["oldest_unreceipted_at"] = None
            self.outstanding = None
            self._dirty = True
            return True

    def state(self) -> Optional[str]:
        with self._lock:
            facts = dict(self.facts)
        return delivery.derive(facts, self.clock()) if facts else None

    # ── one poll ─────────────────────────────────────────────────────────────

    def _flush(self) -> None:
        """Write facts if they changed, and pending notes. Best effort: a report,
        never a failure - but a failed write is retried on the next poll."""
        with self._lock:
            notes, self._notes = self._notes, []
            facts, dirty, self._dirty = dict(self.facts), self._dirty, False
        for note in notes:
            channel._log(note)
        if not dirty:
            return
        try:
            pid, key = procid.current_identity()
            if key is None:
                raise RuntimeError("no process identity to record facts under")
            if not self._pruned:
                delivery.prune()
                self._pruned = True
            delivery.record(pid, key, **facts)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._dirty = True  # retried on the next poll
            channel._log(f"{self.transport} delivery facts not recorded: {type(exc).__name__}: {exc}")

    def poll(self) -> Optional[tuple[object, str, set[int]]]:
        """``(address, nonce, ids)`` to send now, if anything. Blocking; off the loop.

        Recorded as outstanding, and its mail as announced, BEFORE it is sent:
        an attempt counts whatever its outcome - a timeout may still have
        delivered it, and a retry would then send it twice - unless the
        transport says it delivered nothing (``take_back``).
        """
        now = self.clock()
        if self.fulfil and (
            self._last_fulfil is None or now - self._last_fulfil >= timedelta(seconds=channel.FULFIL_S)
        ):
            self._last_fulfil = now
            self.fulfil()
        self._flush()
        address = self.address
        if address is None:
            return None  # no address yet; set once, never unset
        owned = adapters.owned_recipients()
        start = self._cursor
        rows, self._cursor = channel.unread(owned, after=start)
        if start == 0 and self._cursor == 0:
            self.announced &= {r["id"] for r in rows}  # a full sweep: forget read mail
        new = [r for r in rows if r["id"] not in self.announced]
        if new:
            held = set(sessions.granted(owned))
            new = [r for r in new if r["recipient"] in held]
        if not new:
            return None
        nonce = secrets.token_hex(8)
        ids = {r["id"] for r in new}
        with self._lock:
            # Decided here, under the lock that reserves, and nowhere else: a
            # stop may have been decided while the store was being read.
            if self.stopped or self.outstanding is not None:
                return None
            self.announced |= ids
            self.outstanding = (nonce, now)
            self._previous_push = self.facts.get("last_push_at")
            self.facts["last_push_at"] = now
            self.facts["oldest_unreceipted_at"] = now
            self._dirty = True
        self._flush()
        return address, nonce, ids

    def take_back(self, nonce: str, ids: set[int]) -> None:
        """Undo a reservation whose notice was never delivered: its mail is
        announced again, and the facts - written now, not after the backoff -
        say no push happened. Blocking; off the loop."""
        with self._lock:
            if self.outstanding is None or self.outstanding[0] != nonce:
                return  # already receipted: it was delivered after all
            self.outstanding = None
            self.announced -= ids
            self.facts["last_push_at"] = self._previous_push
            self.facts["oldest_unreceipted_at"] = None
            self._dirty = True
        self._flush()

    # ── the loop ─────────────────────────────────────────────────────────────

    async def run(self, initialized: anyio.Event, client: dict) -> None:
        """Wake until cancelled, for this one's client only."""
        await initialized.wait()
        if client.get("name") != self.client:
            return
        with self._lock:
            self.arm(client)
        delay = self.poll_s
        while True:
            # Isolated like the channel pusher: a fault is logged and retried
            # with backoff, never allowed to cancel the task group serving tools.
            try:
                due = await anyio.to_thread.run_sync(self.poll)
                if due:
                    address, nonce, ids = due
                    try:
                        await anyio.to_thread.run_sync(self.send, address, self.notice(nonce))
                    except NotSent:
                        await anyio.to_thread.run_sync(self.take_back, nonce, ids)
                        raise
                    except Exception as exc:  # noqa: BLE001 - may have delivered
                        with self._lock:
                            self._notes.append(
                                f"{self.transport} notice failed; it stays outstanding: "
                                f"{type(exc).__name__}: {exc}"
                            )
                delay = self.poll_s
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                with self._lock:
                    self._notes.append(f"{self.transport} wake {type(exc).__name__}: {exc}")
                delay = min(delay * 2, channel.MAX_BACKOFF_S)
            await anyio.sleep(delay)


# One server per process, so one connection and at most one of each wake.
_installed: list[Announcer] = []


def install(*wakes: Announcer) -> None:
    """Make ``wakes`` the ones receipts and state are answered from."""
    _installed[:] = wakes


def add(wake: Announcer) -> Announcer:
    """Answer from ``wake`` too: one chosen after the client connected."""
    _installed.append(wake)
    return wake


def accept_receipt(nonce: str) -> bool:
    return any(wake.accept_receipt(nonce) for wake in _installed)


def status() -> tuple[Optional[str], Optional[str]]:
    """``(delivery state, transport)`` of the wake serving this connection;
    ``(None, None)`` until one has an address."""
    for wake in _installed:
        state = wake.state()
        if state:
            return state, wake.transport
    return None, None
