# Upgrading

`pipx upgrade proton2m365` (or `uvx proton2m365@latest`). A migration can continue across versions: a newer
proton2m365 upgrades an older state file the first time a writing command opens it, after saving a copy as
`<state>.bak-v<old version>` (made with SQLite's backup API, so nothing is lost). Read-only commands such as
`status` leave an old state file as it is.

An older proton2m365 refuses a state file written by a newer one (`P2M-STATE-NEWER`) and changes nothing.

### Going back to an older version

Prefer going forward: run the newer version again. If you must go back, the `.bak-v<old version>` copy is
the state as it was before the upgrade, so anything done after it (uploads, tags, checks) is not in it. Copy
it over the state file, install the older version, then run `plan` with your export and `verify --all`:
mail already uploaded is found again by its `X-Proton-Id` header, so nothing is uploaded twice.

### Versions

| proton2m365 | state schema | what changed |
|---|---|---|
| 0.1.x | 2 | |
| 0.2.0 | 3 | records which messages a newer export no longer has (`plan --accept-missing`) |

## Pinning a version

```sh
pipx install proton2m365==0.1.0
uvx proton2m365@0.1.0 status
```

## Deprecations

Anything that changes or goes away first prints a warning on stderr for at least one minor release.
The receipt's `proton2m365_receipt` field is its schema version.
