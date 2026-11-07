# Upgrading

`pipx upgrade proton2m365` (or `uvx proton2m365@latest`). A migration can continue across versions: a newer
proton2m365 upgrades an older state file the first time a writing command opens it, after saving a copy as
`<state>.bak-v<old version>` (made with SQLite's backup API, so nothing is lost). Read-only commands such as
`status` leave an old state file as it is.

An older proton2m365 refuses a state file written by a newer one (`P2M-STATE-NEWER`) and changes nothing.

## Pinning a version

```sh
pipx install proton2m365==0.1.0
uvx proton2m365@0.1.0 status
```

## Deprecations

Anything that changes or goes away first prints a warning on stderr for at least one minor release.
The receipt's `proton2m365_receipt` field is its schema version.
