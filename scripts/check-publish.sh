#!/bin/sh
# Refuse to publish personal data. See scripts/check_publish.py for the checks and options.
# Run before the fresh public commit:  scripts/check-publish.sh --stage ../proton2m365-public
exec python3 "$(dirname "$0")/check_publish.py" "$@"
