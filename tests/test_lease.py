"""The mailbox lease: two computers never upload into one mailbox at once."""

from __future__ import annotations

import pytest

from proton2m365 import imap_store as im
from proton2m365.lease import LEASE_FOLDER, LeaseHeld, MailboxLease

from .test_imap_store import FakeClient, FakeClock, FakeServer


def computer(server, clock, name):
    c = FakeClock()
    store = im.ImapStore("me@x", lambda: "tok", client_factory=lambda: FakeClient(server), sleep=c.sleep,
                         pacer=im.Pacer(10**6, 10**12, clock=c.clock, sleep=c.sleep))
    store.connect()
    return MailboxLease(store, clock=clock, holder=name)


class WallClock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def world():
    return FakeServer(), WallClock()


def leases(server):
    return [im.parse_proton_id(m) for _, m, _, _ in server.folders.get("_proton2m365_lease", [])]


def test_second_computer_is_refused_until_the_first_releases(world):
    server, clock = world
    a, b = computer(server, clock, "aaa"), computer(server, clock, "bbb")
    a.acquire()
    with pytest.raises(LeaseHeld, match="P2M-MAILBOX-BUSY"):
        b.acquire()
    a.release()
    b.acquire()
    assert [pid.split(".")[1] for pid in leases(server)] == ["bbb"]


def test_a_crashed_runs_lease_expires(world):
    server, clock = world
    computer(server, clock, "aaa").acquire()           # never released
    b = computer(server, clock, "bbb")
    with pytest.raises(LeaseHeld):
        b.acquire()
    clock.t += 16 * 60
    b.acquire()                                        # expired: taken over, the old message removed
    assert [pid.split(".")[1] for pid in leases(server)] == ["bbb"]


def test_break_lease_takes_over(world):
    server, clock = world
    computer(server, clock, "aaa").acquire()
    computer(server, clock, "bbb").acquire(break_lease=True)
    assert [pid.split(".")[1] for pid in leases(server)] == ["bbb"]


def test_renew_replaces_the_lease_message_and_extends_it(world):
    server, clock = world
    a = computer(server, clock, "aaa")
    a.acquire()
    first = leases(server)
    clock.t += 60
    a.renew()                                          # too soon: unchanged
    assert leases(server) == first
    clock.t += 5 * 60
    a.renew()
    assert len(leases(server)) == 1 and leases(server) != first
    assert int(leases(server)[0].rsplit(".", 1)[1]) == int(clock.t) + 15 * 60


def test_simultaneous_takers_the_earlier_message_wins(world):
    server, clock = world
    a, b = computer(server, clock, "aaa"), computer(server, clock, "bbb")
    a.acquire()
    # b checked at the same instant as a, so its first look saw no lease; after appending it looks again.
    real = b._leases
    looks = []

    def first_look_empty():
        looks.append(1)
        return [] if len(looks) == 1 else real()

    b._leases = first_look_empty
    with pytest.raises(LeaseHeld):
        b.acquire()
    assert [pid.split(".")[1] for pid in leases(server)] == ["aaa"]   # a keeps it; b removed its own message
