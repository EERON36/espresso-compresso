#!/usr/bin/env sh
# Run the source version on Linux or another POSIX system with Python and Tk.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$script_dir/espresso_compresso.py" "$@"
