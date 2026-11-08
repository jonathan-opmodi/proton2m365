from __future__ import annotations

from pathlib import Path

import pytest

from proton2m365.sample_export import ExportBuilder as _SampleBuilder
from proton2m365.sample_export import eml_bytes  # noqa: F401 - shared with sample-export

# The tests' original system labels (no Archive entry: the code must know label 6 without labels.json).
SYSTEM_LABELS = [
    {"ID": "0", "Name": "Inbox", "Path": "Inbox", "Type": 3},
    {"ID": "3", "Name": "Trash", "Path": "Trash", "Type": 3},
    {"ID": "4", "Name": "Spam", "Path": "Spam", "Type": 3},
    {"ID": "7", "Name": "Sent", "Path": "Sent", "Type": 3},
    {"ID": "8", "Name": "Drafts", "Path": "Drafts", "Type": 3},
    {"ID": "10", "Name": "Starred", "Path": "Starred", "Type": 3},
]


class ExportBuilder(_SampleBuilder):
    def __init__(self, root, labels=None, wrapped=True):
        super().__init__(root, labels=labels, wrapped=wrapped, system_labels=SYSTEM_LABELS)


def owner_only(path) -> bool:
    """Readable and writable by the owner only. Windows ignores these mode bits (files there get the user
    profile's permissions, as SECURITY.md says), so the check only applies elsewhere."""
    import os

    return os.name == "nt" or (Path(path).stat().st_mode & 0o777) == 0o600


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No test may touch a real state file, config or token cache: run each in its own folder."""
    monkeypatch.chdir(tmp_path)
    for name in ("PROTON2M365_STATE", "PROTON2M365_CLIENT_ID", "PROTON2M365_TENANT"):
        monkeypatch.delenv(name, raising=False)
    # Tests never hold off sleep or pop up desktop notifications (tests/test_awake.py turns these back on).
    monkeypatch.setenv("PROTON2M365_ALLOW_SLEEP", "1")
    monkeypatch.setenv("PROTON2M365_NO_NOTIFY", "1")
    from proton2m365 import auth

    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path / ".config-proton2m365")
    monkeypatch.setattr(auth, "DEFAULT_CONFIG", tmp_path / ".config-proton2m365" / "config.toml")
    monkeypatch.setattr(auth, "DEFAULT_CACHE", tmp_path / ".config-proton2m365" / "token_cache.bin")


@pytest.fixture
def export(tmp_path: Path) -> ExportBuilder:
    return ExportBuilder(tmp_path)
