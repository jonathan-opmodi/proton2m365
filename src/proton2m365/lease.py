"""A lease in the mailbox itself, so two computers never upload into one mailbox at the same time.

The local mailbox lock (cli._mailbox_lock) only sees runs on this computer. The lease is one small message
in the folder `_proton2m365_lease`, whose X-Proton-Id says who holds it and until when:

    X-Proton-Id: proton2m365-lease.<holder>.<expires, unix seconds>

That reuses the X-Proton-Id lookup upload already trusts. A run takes the lease before it uploads or prunes,
renews it while it works, and deletes it at the end. A crashed run's lease expires by itself (15 minutes).
If two computers take it at the same moment, the message with the lower UID wins and the other backs off.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime

from .targets import MailStore

LEASE_FOLDER = ("_proton2m365_lease",)
PREFIX = "proton2m365-lease."
TTL_SECONDS = 15 * 60


class LeaseHeld(Exception):
    def __init__(self, expires: int):
        until = datetime.fromtimestamp(expires, UTC).strftime("%H:%M UTC")
        super().__init__(
            "P2M-MAILBOX-BUSY: another computer is uploading into this mailbox (its lease runs until "
            f"{until}). Wait for it to finish. If no other computer is running proton2m365 (for example one "
            "crashed), wait until then, or run again with --break-lease")
        self.expires = expires


def _parse(pid: str) -> tuple[str, int] | None:
    if not pid.startswith(PREFIX):
        return None
    try:
        holder, expires = pid[len(PREFIX):].rsplit(".", 1)
        return holder, int(expires)
    except ValueError:
        return None


class MailboxLease:
    def __init__(self, imap: MailStore, *, ttl: int = TTL_SECONDS, clock: Callable[[], float] = time.time,
                 holder: str | None = None):
        self.imap = imap
        self.ttl = ttl
        self.clock = clock
        self.holder = holder or secrets.token_hex(6)
        self.mine: str | None = None   # the X-Proton-Id of our current lease message
        self.renewed = 0.0

    def _leases(self) -> list[tuple[int, str, str, int]]:
        """(uid, pid, holder, expires) for every lease message, read fresh."""
        out = []
        for pid, uid in self.imap.fresh_ids(LEASE_FOLDER).items():
            parsed = _parse(pid)
            if parsed:
                out.append((uid or 0, pid, *parsed))
        return sorted(out)

    def _append(self) -> str:
        now = int(self.clock())
        pid = f"{PREFIX}{self.holder}.{now + self.ttl}"
        when = datetime.fromtimestamp(now, UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
        raw = (f"From: proton2m365 <lease@proton2m365.invalid>\r\nSubject: proton2m365 is working in this mailbox\r\n"
               f"Date: {when}\r\nX-Proton-Id: {pid}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
               "proton2m365 keeps this message here while it uploads, so that a second computer does not upload "
               "the same mail again. It is removed when the run ends, and expires by itself after 15 minutes.\r\n"
               ).encode()
        self.imap.upload(LEASE_FOLDER, pid, raw, seen=True, internal_date=now)
        return pid

    def acquire(self, *, break_lease: bool = False) -> None:
        self.imap.ensure_folders([LEASE_FOLDER])
        now = self.clock()
        others = [(uid, pid, h, exp) for uid, pid, h, exp in self._leases() if h != self.holder]
        live = [lease for lease in others if lease[3] > now]
        if live and not break_lease:
            raise LeaseHeld(max(exp for _, _, _, exp in live))
        stale = [pid for _, pid, _, exp in others if exp <= now or break_lease]
        if stale:
            self.imap.purge(LEASE_FOLDER, stale)
        self.mine = self._append()
        self.renewed = now
        # Two computers may have appended at the same moment: the earlier message (lower UID) wins.
        leases = self._leases()
        ours = next((uid for uid, pid, _, _ in leases if pid == self.mine), None)
        rival = [(uid, exp) for uid, pid, h, exp in leases if h != self.holder and exp > now]
        if ours is not None and any(uid < ours for uid, _ in rival):
            self.release()
            raise LeaseHeld(max(exp for _, exp in rival))

    def renew(self) -> None:
        """Called while working; replaces the lease message once a third of its time has passed."""
        if self.mine is None or self.clock() - self.renewed < self.ttl / 3:
            return
        old = self.mine
        self.mine = self._append()
        self.renewed = self.clock()
        self.imap.purge(LEASE_FOLDER, [old])

    def release(self) -> None:
        mine = [pid for _, pid, h, _ in self._leases() if h == self.holder]
        if mine:
            self.imap.purge(LEASE_FOLDER, mine)
        self.mine = None
