"""--map-folder and --map-label (and [map] in config.toml)."""

from __future__ import annotations

import json

import pytest

from proton2m365 import cli, pipeline
from proton2m365 import proton_export as pe
from proton2m365.sample_export import write_sample_export
from proton2m365.state import open_state


@pytest.mark.parametrize(("folder", "maps", "expected"), [
    (("Projects", "Alpha"), (("Projects", "Clients"),), ("Clients", "Alpha")),          # subfolders follow
    (("Projects", "Alpha"), (("projects/alpha", "Archive"),), ("Archive",)),           # case-insensitive
    (("Projects", "Alpha"), (("Projects", "A"), ("Projects/Alpha", "B/C")), ("B", "C")),  # longest prefix wins
    (("Inbox",), (("Projects", "Clients"),), ("Inbox",)),
])
def test_map_folder(folder, maps, expected):
    assert pipeline.map_folder(folder, maps)[0] == expected


def test_map_categories_renames_drops_and_dedupes():
    used = set()
    out = pipeline.map_categories(("Work", "Starred", "Office"), {"work": "Office", "starred": ""}, used)
    assert out == ("Office",) and used == {"work", "starred"}


@pytest.fixture
def sample(tmp_path):
    return write_sample_export(tmp_path / "sample")


def test_plan_applies_mappings_and_saves_them(sample, tmp_path, capsys):
    code = cli.main(["plan", str(sample), "--map-folder", "Projects=Clients", "--map-folder", "Sent Items=Archive",
                     "--map-label", "Work=Office", "--map-label", "Starred="])
    out = capsys.readouterr().out
    assert code == 0 and "Mapped: Projects -> Clients; Sent Items -> Archive; Work -> Office; Starred -> (no category)" in out
    with open_state(tmp_path / "state.sqlite", write=False) as st:
        rows = {r["proton_id"]: r for r in st.rows()}
        saved = json.loads(st.get_meta("plan_settings"))
    alpha = next(r for pid, r in rows.items() if "alpha" in pid and r["skip_reason"] is None)
    assert json.loads(alpha["target_folder"]) == ["Clients", "Alpha"]
    sent = next(r for pid, r in rows.items() if "sent" in pid)
    assert json.loads(sent["target_folder"]) == ["Archive"]
    cats = {c for r in rows.values() for c in json.loads(r["categories"])}
    assert cats == {"Office"}
    assert saved["map_folders"] == [["Projects", "Clients"], ["Sent Items", "Archive"]]
    code = cli.main(["plan", str(sample)])                              # a re-plan keeps them
    assert code == 0 and "--map-folder Projects=Clients" in capsys.readouterr().out


def test_a_mapping_that_matches_nothing_is_refused(sample, capsys):
    code = cli.main(["plan", str(sample), "--dry-run", "--map-folder", "Nope=Clients"])
    err = capsys.readouterr().err
    assert code == 2 and "P2M-MAP-NO-MATCH" in err and "Projects/Alpha" in err


def test_bad_mapping_syntax(sample, capsys):
    assert cli.main(["plan", str(sample), "--dry-run", "--map-label", "Work"]) == 2
    assert "FROM=TO" in capsys.readouterr().err


def test_mappings_from_config(sample, capsys):
    from proton2m365 import auth

    auth.DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    auth.DEFAULT_CONFIG.write_text('[map.folders]\n"Projects" = "Clients"\n[map.labels]\n"Work" = "Office"\n')
    code = cli.main(["plan", str(sample), "--dry-run", "--map-label", "Work=Team"])   # the flag wins
    out = capsys.readouterr().out
    assert code == 0 and "Projects -> Clients" in out and "Work -> Team" in out and "Clients/Alpha" in out
