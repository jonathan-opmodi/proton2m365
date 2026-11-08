"""Microsoft Graph: find folders, match uploaded mail to Graph IDs, set categories/flags, master categories."""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .proton_export import ARCHIVE_FOLDER, INBOX_FOLDER, SENT_FOLDER, STARRED_CATEGORY

GRAPH = "https://graph.microsoft.com/v1.0"
BATCH_SIZE = 20                # Graph $batch limit
PAGE_SIZE = 1000               # largest $top Graph allows for messages
MAX_RETRIES = 8
DATE_WINDOW_SECONDS = 120      # fallback match: receivedDateTime within ±2 minutes of the planned date
WELL_KNOWN = {INBOX_FOLDER: "inbox", SENT_FOLDER: "sentitems", ARCHIVE_FOLDER: "archive"}

# Outlook's 25 category presets (approximate RGB), for mapping Proton label colors.
PRESET_RGB = {
    "preset0": (0xE7, 0x48, 0x56), "preset1": (0xFF, 0x8C, 0x00), "preset2": (0xAB, 0x7B, 0x05),
    "preset3": (0xFF, 0xF1, 0x00), "preset4": (0x4C, 0xB1, 0x2D), "preset5": (0x00, 0xB2, 0x94),
    "preset6": (0x89, 0x9B, 0x22), "preset7": (0x00, 0x78, 0xD7), "preset8": (0x88, 0x17, 0x98),
    "preset9": (0xE3, 0x00, 0x8C), "preset10": (0x7A, 0x8C, 0xA1), "preset11": (0x4A, 0x5B, 0x6D),
    "preset12": (0xA0, 0xA0, 0xA0), "preset13": (0x60, 0x60, 0x60), "preset14": (0x00, 0x00, 0x00),
    "preset15": (0x75, 0x0B, 0x1C), "preset16": (0xCA, 0x50, 0x10), "preset17": (0x6E, 0x4B, 0x13),
    "preset18": (0xC1, 0x9C, 0x00), "preset19": (0x10, 0x7C, 0x10), "preset20": (0x03, 0x83, 0x87),
    "preset21": (0x5D, 0x6B, 0x13), "preset22": (0x00, 0x2E, 0x8A), "preset23": (0x4E, 0x25, 0x7F),
    "preset24": (0x8D, 0x00, 0x56),
}


class GraphError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Graph {status}: {message}")
        self.status = status


class FolderNotFound(GraphError):
    def __init__(self, path: tuple[str, ...]):
        super().__init__(404, f"folder {'/'.join(path)!r} not found in the mailbox (has upload created it?)")
        self.path = path


def nearest_preset(hex_color: str | None) -> str:
    """Closest Outlook category preset to a '#RRGGBB' color; gray when unknown."""
    if not hex_color or len(hex_color.lstrip("#")) != 6:
        return "preset12"
    try:
        rgb = tuple(int(hex_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "preset12"
    return min(PRESET_RGB, key=lambda name: sum((a - b) ** 2 for a, b in zip(PRESET_RGB[name], rgb)))


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _norm_message_id(value: str | None) -> str:
    return (value or "").strip()


def _parse_graph_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class MatchTarget:
    """What tag knows about one uploaded message."""

    proton_id: str
    message_id: str
    internal_date: int | None


@dataclass
class MatchResult:
    matched: dict[str, str] = field(default_factory=dict)  # proton_id -> graph message id
    unmatched: list[str] = field(default_factory=list)


class GraphStore:
    def __init__(
        self,
        token: Callable[[], str],
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        say: Callable[[str], None] = lambda _msg: None,
    ):
        self.token = token
        self.http = client or httpx.Client(base_url=GRAPH, timeout=60)
        self.sleep = sleep
        self.say = say
        self._folder_cache: dict[tuple[str, ...], str] = {}

    # -- HTTP -------------------------------------------------------------------------------------

    def mime(self, graph_id: str) -> bytes:
        """The message as Exchange stores it (MIME), for verify --content."""
        return self.request("GET", f"/me/messages/{graph_id}/$value", raw=True)

    def request(self, method: str, url: str, *, raw: bool = False, idempotent: bool | None = None, **kwargs):
        """One Graph call, retrying after Retry-After. raw=True returns the body bytes.

        429 is always retried (Graph refused the call before running it). 503/504 are retried only for
        idempotent calls: a POST that creates something may have run before the error, so it is never
        repeated blindly; its caller looks the item up first (see create_once)."""
        if idempotent is None:
            idempotent = method.upper() != "POST"
        retry_on = (429, 503, 504) if idempotent else (429,)
        extra = kwargs.pop("headers", {})
        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self.token()}", **extra}
            response = self.http.request(method, url, headers=headers, **kwargs)
            if response.status_code in retry_on and attempt < MAX_RETRIES:
                wait = float(response.headers.get("Retry-After", 2 ** attempt))
                self.say(f"Graph asked us to wait {wait:.0f}s")
                self.sleep(wait)
                continue
            if response.status_code >= 400:
                raise GraphError(response.status_code, _error_text(response))
            if raw:
                return response.content
            return response.json() if response.content else {}
        raise GraphError(429, "still throttled after retries")

    def create_once(self, create: Callable[[], dict], exists: Callable[[], bool]) -> dict | None:
        """Create something exactly once. After an ambiguous failure (503/504, a timeout, a lost connection) the
        create may already have happened, so look it up before trying again. Returns the create's response, or
        None when the lookup found it already there."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                return create()
            except GraphError as exc:
                if exc.status == 409:  # Graph says it exists
                    return None
                if exc.status not in (503, 504) or attempt == MAX_RETRIES:
                    raise
            except httpx.TransportError:
                if attempt == MAX_RETRIES:
                    raise
            if exists():
                return None
            self.say("Graph did not confirm a create; it is not there, so trying again")
            self.sleep(2 ** attempt)
        return None

    def paged(self, url: str, params: dict | None = None) -> Iterator[dict]:
        page = self.request("GET", url, params=params)
        while True:
            yield from page.get("value", [])
            next_link = page.get("@odata.nextLink")
            if not next_link:
                return
            page = self.request("GET", next_link)

    def batch(self, requests: list[dict]) -> dict[str, dict]:
        """Run sub-requests through $batch, 20 at a time, retrying throttled ones. Returns id -> response."""
        results: dict[str, dict] = {}
        pending = list(requests)
        for _ in range(MAX_RETRIES + 1):
            if not pending:
                return results
            retry: list[dict] = []
            wait = 0.0
            for start in range(0, len(pending), BATCH_SIZE):
                chunk = pending[start:start + BATCH_SIZE]
                safe = all(r.get("method", "GET").upper() != "POST" for r in chunk)
                body = self.request("POST", "/$batch", json={"requests": chunk}, idempotent=safe)
                by_id = {r["id"]: r for r in chunk}
                for item in body.get("responses", []):
                    sub = by_id[item["id"]]
                    again = (429, 503, 504) if sub.get("method", "GET").upper() != "POST" else (429,)
                    if item.get("status") in again:
                        retry.append(by_id[item["id"]])
                        wait = max(wait, float((item.get("headers") or {}).get("Retry-After", 5)))
                    else:
                        results[item["id"]] = item
            pending = retry
            if pending:
                self.say(f"Graph throttled {len(pending)} requests; waiting {wait:.0f}s")
                self.sleep(wait)
        if pending:
            raise GraphError(429, f"{len(pending)} batch requests still throttled after retries")
        return results

    # -- folders ----------------------------------------------------------------------------------

    def resolve_folder(self, path: tuple[str, ...]) -> str:
        """Graph folder id for a planned folder path: well-known names, else walk from the root."""
        if path in self._folder_cache:
            return self._folder_cache[path]
        if path in WELL_KNOWN:
            folder_id = self.request("GET", f"/me/mailFolders/{WELL_KNOWN[path]}", params={"$select": "id"})["id"]
        else:
            parent = "msgfolderroot"
            for depth, segment in enumerate(path):
                children = list(self.paged(f"/me/mailFolders/{parent}/childFolders",
                                           {"$select": "id,displayName", "$top": 100}))
                wanted = _nfc(segment)
                exact = [c for c in children if _nfc(c.get("displayName", "")) == wanted]
                loose = [c for c in children if _nfc(c.get("displayName", "")).casefold() == wanted.casefold()]
                found = (exact or loose or [None])[0]
                if found is None:
                    raise FolderNotFound(path[: depth + 1])
                parent = found["id"]
            folder_id = parent
        self._folder_cache[path] = folder_id
        return folder_id

    # -- matching ---------------------------------------------------------------------------------

    def folder_messages(self, folder_id: str) -> list[dict]:
        return list(self.paged(f"/me/mailFolders/{folder_id}/messages", {
            "$select": "id,internetMessageId,receivedDateTime", "$top": PAGE_SIZE,
        }))

    def proton_ids_for(self, graph_ids: Iterable[str]) -> dict[str, str]:
        """graph id -> X-Proton-Id, read from internetMessageHeaders."""
        ids = {str(i): gid for i, gid in enumerate(graph_ids)}
        requests = [{"id": rid, "method": "GET", "url": f"/me/messages/{gid}?$select=internetMessageHeaders"}
                    for rid, gid in ids.items()]
        found: dict[str, str] = {}
        for rid, response in self.batch(requests).items():
            if response.get("status") != 200:
                continue
            for header in (response.get("body") or {}).get("internetMessageHeaders") or []:
                if header.get("name", "").lower() == "x-proton-id":
                    found[ids[rid]] = header.get("value", "").strip()
                    break
        return found

    def match(self, folder_id: str, targets: list[MatchTarget]) -> MatchResult:
        """Map uploaded messages to Graph ids. Every match is confirmed by the X-Proton-Id header.

        Candidates come from the Message-ID first, then (when none of those carries the header) from
        messages received within DATE_WINDOW_SECONDS. A Message-ID alone is never enough: mail that was
        already in the folder can share it. Two copies carrying the same
        id (a duplicate upload) are ambiguous and stay unmatched.
        """
        result = MatchResult()
        if not targets:
            return result
        messages = self.folder_messages(folder_id)
        by_mid: dict[str, list[str]] = {}
        for m in messages:
            by_mid.setdefault(_norm_message_id(m.get("internetMessageId")), []).append(m["id"])
        headers: dict[str, str] = {}
        fetched: set[str] = set()

        def fetch_headers(gids: Iterable[str]) -> None:
            todo = sorted(set(gids) - fetched)
            if todo:
                headers.update(self.proton_ids_for(todo))
                fetched.update(todo)

        def confirmed(pid: str, gids: list[str]) -> list[str]:
            fetch_headers(gids)
            return [gid for gid in gids if headers.get(gid) == pid]

        def near(t: MatchTarget) -> list[str]:
            if t.internal_date is None:
                return []
            return [m["id"] for m in messages
                    if (ts := _parse_graph_time(m.get("receivedDateTime"))) is not None
                    and abs(ts - t.internal_date) <= DATE_WINDOW_SECONDS]

        # Header lookups are batched: first every Message-ID candidate, then the date-window fallbacks.
        by_target = {t.proton_id: by_mid.get(_norm_message_id(t.message_id), []) for t in targets}
        fetch_headers(g for gids in by_target.values() for g in gids)
        hits: dict[str, list[str]] = {t.proton_id: confirmed(t.proton_id, by_target[t.proton_id]) for t in targets}
        misses = [t for t in targets if not hits[t.proton_id]]
        if misses:
            fetch_headers(g for t in misses for g in near(t))
            for t in misses:
                hits[t.proton_id] = confirmed(t.proton_id, near(t))

        for t in targets:  # a message carries one X-Proton-Id, so a confirmed match is one-to-one
            gids = hits[t.proton_id]
            if len(gids) == 1:
                result.matched[t.proton_id] = gids[0]
            else:
                result.unmatched.append(t.proton_id)
        return result

    def get_messages(self, graph_ids: Iterable[str], select: str) -> dict[str, dict | None]:
        """graph id -> message fields (None when Graph says it doesn't exist)."""
        ids = {str(i): gid for i, gid in enumerate(graph_ids)}
        requests = [{"id": rid, "method": "GET", "url": f"/me/messages/{gid}?$select={select}"}
                    for rid, gid in ids.items()]
        out: dict[str, dict | None] = {}
        for rid, response in self.batch(requests).items():
            status = int(response.get("status", 0))
            if status == 404:
                out[ids[rid]] = None
            elif 200 <= status < 300:
                out[ids[rid]] = response.get("body") or {}
            else:
                raise GraphError(status, _batch_error(response))
        return out

    def folder_total(self, folder_id: str) -> int:
        return int(self.request("GET", f"/me/mailFolders/{folder_id}", params={"$select": "totalItemCount"})
                   .get("totalItemCount", 0))

    # -- tagging ----------------------------------------------------------------------------------

    def patch_messages(self, changes: dict[str, dict]) -> dict[str, str]:
        """PATCH many messages (graph id -> body) via $batch. Returns graph id -> error for failures."""
        requests = []
        ids: dict[str, str] = {}
        for i, (gid, body) in enumerate(changes.items()):
            rid = str(i)
            ids[rid] = gid
            requests.append({"id": rid, "method": "PATCH", "url": f"/me/messages/{gid}",
                             "headers": {"Content-Type": "application/json"}, "body": body})
        errors = {}
        for rid, response in self.batch(requests).items():
            if not 200 <= int(response.get("status", 0)) < 300:
                errors[ids[rid]] = f"{response.get('status')}: {_batch_error(response)}"
        return errors

    def ensure_categories(self, wanted: dict[str, str | None]) -> list[str]:
        """Create master categories that don't exist yet (name -> Proton color). Returns names created."""
        existing = {c["displayName"].casefold() for c in self.paged("/me/outlook/masterCategories")}
        created = []
        for name, color in sorted(wanted.items(), key=lambda kv: kv[0].casefold()):
            if name.casefold() in existing:
                continue
            preset = "preset3" if name == STARRED_CATEGORY and not color else nearest_preset(color)
            self.create_once(
                lambda name=name, preset=preset: self.request(
                    "POST", "/me/outlook/masterCategories", json={"displayName": name, "color": preset}),
                lambda name=name: any(c["displayName"].casefold() == name.casefold()
                                      for c in self.paged("/me/outlook/masterCategories")))
            existing.add(name.casefold())
            created.append(name)
        return created


    def delete_category(self, name: str) -> bool:
        """Delete one master category by name. Returns whether it existed."""
        for c in self.paged("/me/outlook/masterCategories"):
            if c["displayName"].casefold() == name.casefold():
                self.request("DELETE", f"/me/outlook/masterCategories/{c['id']}")
                return True
        return False


def tag_body(categories: Iterable[str], flagged: bool) -> dict:
    # Always explicit: Exchange turns an X-Message-Flag header into a flag on its own, so a message
    # that wasn't starred in Proton must be set to notFlagged, not left alone.
    return {"categories": list(categories), "flag": {"flagStatus": "flagged" if flagged else "notFlagged"}}


def _error_text(response: httpx.Response) -> str:
    try:
        err = response.json().get("error", {})
        return f"{err.get('code', '')} {err.get('message', '')}".strip() or response.text[:300]
    except ValueError:
        return response.text[:300]


def _batch_error(item: dict) -> str:
    err = (item.get("body") or {}).get("error") or {}
    return f"{err.get('code', '')} {err.get('message', '')}".strip() or "error"
