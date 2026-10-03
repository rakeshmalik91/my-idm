#!/bin/sh
# POSIX launcher for My-IDM, mirroring run.bat.
#
# `pyproject.toml` already declares `my-idm` / `my-idm-gui` entry points that work on all three
# platforms, so an installed copy does not need this script. It exists for the common case of
# running from a checkout, where the package is not pip-installed and `python -m my_idm.main` is
# the only reliable invocation.
#
# Differences from run.bat that are deliberate, not oversights:
#   * `.venv/bin/activate` rather than `.venv\Scripts\activate.bat` - the POSIX layout.
#   * `PYTHONPATH` is not set: the script `cd`s to its own directory, and `python -m` puts the
#     current directory first on sys.path, so `my_idm` resolves. Setting it as well would risk
#     shadowing an installed copy of the package with the checkout, which is the opposite of what
#     someone running from a checkout usually wants on a machine that also has it installed.
#   * No `start` / windowless invocation. A packaged Linux build uses the .desktop file's
#     `Terminal=false`; this script is for a terminal.

set -e

# Navigate to the directory of this script, following symlinks, so the launcher works from
# anywhere - including a symlink in ~/bin or /usr/local/bin.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$script_dir"

# Activate a virtual environment if one is present. Sourced rather than executed: `activate` is a
# shell script that exports PATH, so running it in a child process would discard the change.
for venv in .venv venv; do
    if [ -f "$venv/bin/activate" ]; then
        # shellcheck disable=SC1091
        . "$venv/bin/activate"
        break
    fi
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "run.sh: python3 not found on PATH." >&2
    echo "Install Python 3.10 or newer, or activate a virtual environment first." >&2
    exit 1
fi

# exec so the Python process replaces this shell: it then receives SIGINT directly and Ctrl-C
# stops My-IDM instead of being swallowed by an intervening process.
exec python3 -m my_idm.main "$@"
