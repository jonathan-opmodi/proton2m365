from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from proton2m365 import graph_store as gs

WELL_KNOWN_IDS = {"inbox": "F-INBOX", "sentitems": "F-SENT", "archive": "F-ARCHIVE"}


class FakeGraph:
    """Just enough of Graph: folders, messages, $batch GET/PATCH, master categories, throttling."""

    def __init__(self):
        self.children = {"msgfolderroot": [], "F-AUTO": []}
        self.messages: dict[str, list[dict]] = {}
        self.master = [{"displayName": "important", "color": "preset0"}]
        self.throttle_once = 0          # top-level 429s to return before succeeding
        self.batch_throttle: set[str] = set()  # message ids whose PATCH gets one 429 inside $batch
        self.batch_calls = 0
        self.log: list[tuple[str, str]] = []
        self.patched: dict[str, dict] = {}
        self.created_categories: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1.0")
        self.log.append((request.method, path))
        if self.throttle_once:
            self.throttle_once -= 1
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"code": "TooManyRequests"}})
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        parts = path.strip("/").split("/")

        if path == "/$batch":
            return self._batch(json.loads(request.content))
        if path == "/me/outlook/masterCategories":
            if request.method == "POST":
                body = json.loads(request.content)
                self.created_categories.append(body)
                self.master.append(body)
                return httpx.Response(201, json=body)
            return httpx.Response(200, json={"value": self.master})
        if parts[:2] == ["me", "mailFolders"] and len(parts) == 3:
            if parts[2] in WELL_KNOWN_IDS:
                return httpx.Response(200, json={"id": WELL_KNOWN_IDS[parts[2]]})
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound", "message": "nope"}})
        if parts[:2] == ["me", "mailFolders"] and parts[3] == "childFolders":
            return self._page(request, self.children.get(parts[2], []), params, page=2)
        if parts[:2] == ["me", "mailFolders"] and parts[3] == "messages":
            return self._page(request, self.messages.get(parts[2], []), params, page=int(params.get("$top", 10)))
        return httpx.Response(400, json={"error": {"code": "BadRoute", "message": path}})

    def _page(self, request, items, params, page):
        skip = int(params.get("$skip", 0))
        body = {"value": [{k: v for k, v in i.items() if k != "headers"} for i in items[skip:skip + page]]}
        if skip + page < len(items):
            body["@odata.nextLink"] = str(request.url.copy_merge_params({"$skip": str(skip + page)}))
        return httpx.Response(200, json=body)

    def _batch(self, payload):
        self.batch_calls += 1
        assert len(payload["requests"]) <= gs.BATCH_SIZE
        out = []
        for sub in payload["requests"]:
            gid = sub["url"].split("/")[3].split("?")[0]
            msg = next((m for ms in self.messages.values() for m in ms if m["id"] == gid), None)
            if msg is None:
                out.append({"id": sub["id"], "status": 404, "body": {"error": {"code": "ErrorItemNotFound", "message": "gone"}}})
            elif sub["method"] == "GET":
                headers = [{"name": n, "value": v} for n, v in msg.get("headers", {}).items()]
                out.append({"id": sub["id"], "status": 200, "body": {"internetMessageHeaders": headers}})
            elif gid in self.batch_throttle:
                self.batch_throttle.discard(gid)
                out.append({"id": sub["id"], "status": 429, "headers": {"Retry-After": "7"}, "body": {}})
            else:
                self.patched[gid] = sub["body"]
                out.append({"id": sub["id"], "status": 200, "body": {"id": gid}})
        return httpx.Response(200, json={"responses": out})


@pytest.fixture
def fake():
    return FakeGraph()


@pytest.fixture
def slept():
    return []


@pytest.fixture
def graph(fake, slept):
    client = httpx.Client(base_url=gs.GRAPH, transport=httpx.MockTransport(fake.handler))
    return gs.GraphStore(lambda: "tok", client=client, sleep=slept.append)


def message(gid, mid, received="2019-06-08T13:20:00Z", proton_id=None):
    m = {"id": gid, "internetMessageId": mid, "receivedDateTime": received}
    if proton_id:
        m["headers"] = {"X-Proton-Id": proton_id}
    return m


# --- small helpers ------------------------------------------------------------------------------

@pytest.mark.parametrize(("color", "preset"), [("#E74856", "preset0"), ("#000000", "preset14"), ("#8080FF", "preset10"),
                                              (None, "preset12"), ("#zzzzzz", "preset12"), ("#fff", "preset12")])
def test_nearest_preset(color, preset):
    assert gs.nearest_preset(color) == preset


def test_tag_body():
    assert gs.tag_body(["A"], False) == {"categories": ["A"], "flag": {"flagStatus": "notFlagged"}}
    assert gs.tag_body([], True) == {"categories": [], "flag": {"flagStatus": "flagged"}}


# --- HTTP behavior ------------------------------------------------------------------------------

def test_retries_after_429(graph, fake, slept):
    fake.throttle_once = 2
    assert graph.resolve_folder(("Inbox",)) == "F-INBOX"
    assert slept == [3.0, 3.0]


def test_errors_carry_status(graph, fake):
    with pytest.raises(gs.GraphError, match="ErrorItemNotFound") as exc:
        graph.request("GET", "/me/mailFolders/nope")
    assert exc.value.status == 404


# --- folders -----------------------------------------------------------------------------------

@pytest.mark.parametrize(("path", "folder_id"), [(("Inbox",), "F-INBOX"), (("Sent Items",), "F-SENT"), (("Archive",), "F-ARCHIVE")])
def test_well_known_folders(graph, path, folder_id):
    assert graph.resolve_folder(path) == folder_id


def test_nested_folder_walk_with_paging_nfc_and_case(graph, fake):
    fake.children["msgfolderroot"] = [{"id": "F-X", "displayName": "X"}, {"id": "F-Y", "displayName": "Y"},
                                      {"id": "F-AUTO", "displayName": "alerts"}]
    fake.children["F-AUTO"] = [{"id": "F-CAFE", "displayName": "Café ∕ x"}]
    assert graph.resolve_folder(("Alerts", "Café ∕ x")) == "F-CAFE"
    calls = len(fake.log)
    assert graph.resolve_folder(("Alerts", "Café ∕ x")) == "F-CAFE"  # cached
    assert len(fake.log) == calls


def test_exact_name_beats_case_insensitive(graph, fake):
    fake.children["msgfolderroot"] = [{"id": "lower", "displayName": "work"}, {"id": "exact", "displayName": "Work"}]
    assert graph.resolve_folder(("Work",)) == "exact"


def test_missing_folder(graph, fake):
    fake.children["msgfolderroot"] = [{"id": "F-AUTO", "displayName": "Alerts"}]
    with pytest.raises(gs.FolderNotFound) as exc:
        graph.resolve_folder(("Alerts", "Amazon"))
    assert exc.value.path == ("Alerts", "Amazon")


# --- matching ----------------------------------------------------------------------------------

T = gs.MatchTarget
JUNE_8 = 1560000000  # 2019-06-08T13:20:00Z


def test_match_paths(graph, fake):
    fake.messages["F-INBOX"] = [
        message("g1", "<a@x>", proton_id="pA"),
        message("g2", "<dup@x>", proton_id="pB"),
        message("g3", "<dup@x>", proton_id="pC"),
        message("g4", "<rewritten-by-exchange@x>", proton_id="pD"),
        message("g5", "<other@x>", received="2020-01-01T00:00:00Z", proton_id="pE-not-near"),
    ]
    result = graph.match("F-INBOX", [
        T("pA", "<a@x>", JUNE_8),
        T("pB", "<dup@x>", JUNE_8),
        T("pC", " <dup@x> ", JUNE_8),
        T("pD", "<original@x>", JUNE_8 + 90),        # Message-ID changed; found by date window + header
        T("pE", "<gone@x>", JUNE_8),                  # nothing near carries pE
        T("pF", "<also-gone@x>", None),               # no date: can't fall back
    ])
    assert result.matched == {"pA": "g1", "pB": "g2", "pC": "g3", "pD": "g4"}
    assert sorted(result.unmatched) == ["pE", "pF"]


def test_match_pages_through_large_folders(graph, fake, monkeypatch):
    monkeypatch.setattr(gs, "PAGE_SIZE", 3)
    fake.messages["F-ARCHIVE"] = [message(f"g{i}", f"<m{i}@x>", proton_id=f"p{i}") for i in range(8)]
    result = graph.match("F-ARCHIVE", [T(f"p{i}", f"<m{i}@x>", JUNE_8) for i in range(8)])
    assert len(result.matched) == 8 and not result.unmatched


def test_match_never_takes_existing_mail_that_shares_the_message_id(graph, fake):
    # Mail already in Inbox (no X-Proton-Id) has the same Message-ID as the uploaded copy,
    # which Graph does not show yet: nothing is matched, so tag cannot touch the existing message.
    fake.messages["F-INBOX"] = [message("old", "<same@x>")]
    result = graph.match("F-INBOX", [T("pA", "<same@x>", JUNE_8)])
    assert result.matched == {} and result.unmatched == ["pA"]
    # Once the uploaded copy is visible, it is the one matched.
    fake.messages["F-INBOX"].append(message("new", "<same@x>", proton_id="pA"))
    assert graph.match("F-INBOX", [T("pA", "<same@x>", JUNE_8)]).matched == {"pA": "new"}


def test_match_falls_back_to_the_date_window_when_message_id_hits_only_other_mail(graph, fake):
    fake.messages["F-INBOX"] = [message("old", "<same@x>"), message("ours", "<rewritten@x>", proton_id="pA")]
    assert graph.match("F-INBOX", [T("pA", "<same@x>", JUNE_8)]).matched == {"pA": "ours"}


def test_match_never_takes_a_message_carrying_another_id(graph, fake):
    fake.messages["F-INBOX"] = [message("g1", "<a@x>", proton_id="pA"), message("g2", "<b@x>", proton_id="pA")]
    result = graph.match("F-INBOX", [T("pA", "<a@x>", JUNE_8), T("pA2", "<a@x>", JUNE_8)])
    assert result.matched == {"pA": "g1"}
    assert result.unmatched == ["pA2"]


def test_match_duplicate_upload_is_ambiguous(graph, fake):
    # Two copies carry the same X-Proton-Id (a duplicate upload): report it, never pick one.
    fake.messages["F-INBOX"] = [message("g1", "<a@x>", proton_id="pA"), message("g2", "<a@x>", proton_id="pA")]
    assert graph.match("F-INBOX", [T("pA", "<a@x>", JUNE_8)]).unmatched == ["pA"]


def test_match_nothing_to_do_makes_no_requests(graph, fake):
    assert graph.match("F-INBOX", []).matched == {}
    assert fake.log == []


# --- tagging -----------------------------------------------------------------------------------

def test_patch_messages_batches_retries_and_reports(graph, fake, slept):
    fake.messages["F-ARCHIVE"] = [message(f"g{i}", f"<m{i}@x>") for i in range(45)]
    fake.batch_throttle = {"g7"}
    changes = {f"g{i}": gs.tag_body(["Important"], i == 3) for i in range(45)}
    changes["missing"] = gs.tag_body(["X"], False)
    errors = graph.patch_messages(changes)
    assert errors == {"missing": "404: ErrorItemNotFound gone"}
    assert len(fake.patched) == 45
    assert fake.patched["g3"] == {"categories": ["Important"], "flag": {"flagStatus": "flagged"}}
    assert fake.patched["g7"] == {"categories": ["Important"], "flag": {"flagStatus": "notFlagged"}}
    assert slept == [7.0]
    assert fake.batch_calls == 4  # 46 requests in 3 batches, then 1 retry batch


def test_ensure_categories_creates_only_missing(graph, fake):
    created = graph.ensure_categories({"Important": "#FF0000", "Travel": "#00B294", "Starred": None, "receipts": None})
    assert created == ["receipts", "Starred", "Travel"]
    assert {c["displayName"]: c["color"] for c in fake.created_categories} == {
        "receipts": "preset12", "Starred": "preset3", "Travel": "preset5"}
    assert graph.ensure_categories({"Travel": None}) == []     # second run creates nothing


def test_a_post_is_not_repeated_after_503(graph, fake):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(503, json={"error": {"code": "ServiceUnavailable", "message": "busy"}})

    graph.http = httpx.Client(base_url=gs.GRAPH, transport=httpx.MockTransport(handler))
    with pytest.raises(gs.GraphError) as exc:
        graph.request("POST", "/me/outlook/masterCategories", json={"displayName": "X"})
    assert exc.value.status == 503 and calls == ["POST"]


def test_a_category_created_before_a_lost_reply_is_not_created_twice(graph, fake, slept):
    real = fake.handler
    lost = {"n": 1}

    def handler(request):
        response = real(request)
        if request.method == "POST" and lost["n"]:
            lost["n"] -= 1
            return httpx.Response(504, json={"error": {"code": "GatewayTimeout", "message": "lost"}})
        return response

    graph.http = httpx.Client(base_url=gs.GRAPH, transport=httpx.MockTransport(handler))
    assert graph.ensure_categories({"Travel": None}) == ["Travel"]
    assert [c["displayName"] for c in fake.created_categories] == ["Travel"]   # created once, reply lost


def test_a_category_create_that_did_not_happen_is_retried(graph, fake, slept):
    real = fake.handler
    failed = {"n": 1}

    def handler(request):
        if request.method == "POST" and failed["n"]:
            failed["n"] -= 1
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable", "message": "busy"}})
        return real(request)

    graph.http = httpx.Client(base_url=gs.GRAPH, transport=httpx.MockTransport(handler))
    assert graph.ensure_categories({"Travel": None}) == ["Travel"]
    assert [c["displayName"] for c in fake.created_categories] == ["Travel"]
