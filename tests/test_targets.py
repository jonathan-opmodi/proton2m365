"""The destination seam: the Microsoft classes provide what the rest of proton2m365 is written against."""

from __future__ import annotations

import httpx
import pytest

from proton2m365 import cli, targets
from proton2m365 import graph_store as gs
from proton2m365 import imap_store as im

from .test_imap_store import FakeClient, FakeServer


def test_microsoft_classes_satisfy_the_interfaces():
    store = im.ImapStore("me@x", lambda: "tok", client_factory=lambda: FakeClient(FakeServer()))
    graph = gs.GraphStore(lambda: "tok", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    assert isinstance(store, targets.MailStore)
    assert isinstance(graph, targets.Organizer)


def test_registry_and_target_flag():
    assert targets.get("microsoft365").name == "microsoft365"
    with pytest.raises(ValueError, match="available: microsoft365"):
        targets.get("fastmail")
    args = cli.build_parser().parse_args(["upload"])
    assert args.target == "microsoft365"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["upload", "--target", "fastmail"])


def test_online_commands_connect_through_the_registry(monkeypatch, tmp_path):
    seen = []

    def fake_connect(args, **kwargs):
        seen.append((args.command, kwargs))
        raise cli.auth_mod.AuthError("stop here")

    monkeypatch.setitem(targets.TARGETS, "microsoft365",
                        targets.Target("microsoft365", "test", fake_connect))
    from proton2m365 import pipeline
    from proton2m365.sample_export import write_sample_export

    pipeline.plan_export(write_sample_export(tmp_path / "s"), pipeline.PlanSettings(), state_path=tmp_path / "s.db")
    assert cli.main(["tag", "--state", str(tmp_path / "s.db")]) == 2
    assert seen == [("tag", {"recorded": None})]
