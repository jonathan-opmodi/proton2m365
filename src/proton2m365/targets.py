"""The seam between the migration (export, plan, state, verify, receipt) and the mailbox it writes to.

Everything Microsoft-specific (MSAL sign-in, Exchange's IMAP quirks, Graph categories and flags) sits behind
two interfaces. The rest of proton2m365 is written against these, so another destination (Fastmail, iCloud,
Gmail: IMAP for the mail, their own API or IMAP keywords for labels) means writing one new module that
provides them, plus an entry in TARGETS. Only `microsoft365` exists today.

    MailStore   puts messages into folders and finds them again by X-Proton-Id (IMAP)
    Organizer   applies categories and flags, and reads back what verify checks (Microsoft Graph)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MailStore(Protocol):
    """Writes and finds mail. Messages are identified only by their X-Proton-Id header."""

    delimiter: str

    def ensure_folders(self, paths: Iterable[tuple[str, ...]]) -> list[str]: ...
    def has_folder(self, path: tuple[str, ...]) -> bool: ...
    def delete_folder(self, path: tuple[str, ...]) -> None: ...
    def upload(self, path: tuple[str, ...], proton_id: str, message: bytes, *, seen: bool,
               internal_date: int | None) -> Any: ...
    def present_ids(self, path: tuple[str, ...]) -> dict[str, int | None]: ...
    def fresh_ids(self, path: tuple[str, ...]) -> dict[str, int | None]: ...
    def duplicates(self, path: tuple[str, ...]) -> dict[str, list[int]]: ...
    def remove(self, path: tuple[str, ...], proton_ids: Iterable[str]) -> set[str]: ...
    def purge(self, path: tuple[str, ...], proton_ids: Iterable[str]) -> set[str]: ...
    def close(self) -> None: ...


@runtime_checkable
class Organizer(Protocol):
    """Applies categories and flags, and reads messages back for verify."""

    def resolve_folder(self, path: tuple[str, ...]) -> str: ...
    def match(self, folder_id: str, targets: list) -> Any: ...
    def patch_messages(self, changes: dict[str, dict]) -> dict[str, str]: ...
    def ensure_categories(self, wanted: dict[str, str | None]) -> list[str]: ...
    def delete_category(self, name: str) -> bool: ...
    def get_messages(self, graph_ids: Iterable[str], select: str) -> dict[str, dict | None]: ...
    def folder_total(self, folder_id: str) -> int: ...
    def mime(self, graph_id: str) -> bytes: ...


@dataclass(frozen=True)
class Target:
    name: str
    description: str
    connect: Callable[..., Any]   # (args) -> an object with .auth, .imap_store(args) -> MailStore, .graph() -> Organizer


def _microsoft365(args, **kwargs):
    from . import cli  # looked up at call time

    return cli._Connections(args, **kwargs)


TARGETS: dict[str, Target] = {
    "microsoft365": Target("microsoft365", "Microsoft 365 and Outlook.com (IMAP + Microsoft Graph)", _microsoft365),
}
DEFAULT_TARGET = "microsoft365"


def get(name: str) -> Target:
    try:
        return TARGETS[name]
    except KeyError:
        raise ValueError(f"unknown target {name!r}; available: {', '.join(sorted(TARGETS))}") from None
