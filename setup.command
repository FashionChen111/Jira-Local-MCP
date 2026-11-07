#!/bin/sh
ROOT=$(CDPATH= cd -P "$(dirname "$0")" && pwd) || exit 1
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    printf 'Python 3.10 or newer is required. Install it and run setup again.\n'
    printf 'Press Enter to close: '
    IFS= read -r _
    exit 1
fi
python3 "$ROOT/scripts/install.py"
status=$?
printf '\nPress E to update configuration, or Enter to exit: '
IFS= read -r choice || choice=
case "$choice" in
    e|E) python3 "$ROOT/scripts/install.py" --edit
         printf 'Press Enter to close: '
         IFS= read -r _ ;;
esac
exit "$status"
