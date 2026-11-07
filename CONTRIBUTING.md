# Contributing

## Running the tests

```sh
uv sync
uv run pytest
```

The tests never touch a real mailbox: IMAP and Graph are fakes (`tests/test_imap_store.py`,
`tests/test_pipeline.py`). `proton2m365 selftest` is the live check against a real (test) mailbox.

## Reporting a problem

Open an issue and paste the output of `proton2m365 doctor --issue`. It replaces addresses, folder and label
names with hashes. Please do not attach your state file, receipt or crash log unless asked.

## Before pushing

`scripts/check-publish.sh` must pass, and the `scripts/pre-push` hook runs it on every push. It refuses
real email addresses, databases and receipts. Tests use `example.com` / `example.org` addresses and synthetic
`.eml` files only (list new ones in `tests/fixtures/SYNTHETIC.txt`).
