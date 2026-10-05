#!/usr/bin/env bash
# Run from a checkout or stream with curl ... | bash.
set -euo pipefail

fail() {
    printf 'Installation error: %s\n' "$1" >&2
    exit 1
}

command -v python3 >/dev/null 2>&1 || fail 'Python 3.11+ is required.'

source_path=${BASH_SOURCE[0]:-}
if [[ -n "$source_path" && -f "$source_path" ]]; then
    checkout=$(cd -- "$(dirname -- "$source_path")" && pwd -P)
    [[ -f "$checkout/install.py" ]] || fail 'Run install.sh from a complete repository checkout.'
else
    command -v git >/dev/null 2>&1 || fail 'Git is required.'
    checkout=${MIMO_GROK_REPO_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/mimo-grok-adapter}
    [[ "$checkout" == /* ]] || fail 'MIMO_GROK_REPO_DIR must be an absolute path.'
    [[ ! -L "$checkout" ]] || fail 'The checkout directory must be a regular directory.'

    if [[ -e "$checkout" ]]; then
        [[ -d "$checkout/.git" || -f "$checkout/.git" ]] || fail 'The existing directory is unrelated to this Git checkout; choose another path.'
        checkout=$(cd -- "$checkout" && pwd -P)
        top=$(git -C "$checkout" rev-parse --show-toplevel < /dev/null)
        [[ "$top" == "$checkout" ]] || fail 'The selected directory must be the checkout root.'
        origin=$(git -C "$checkout" remote get-url origin < /dev/null)
        case "$origin" in
            https://github.com/zinin/mimo-grok-adapter|https://github.com/zinin/mimo-grok-adapter.git|git@github.com:zinin/mimo-grok-adapter.git|ssh://git@github.com/zinin/mimo-grok-adapter.git) ;;
            *) fail 'The checkout origin must point to zinin/mimo-grok-adapter.' ;;
        esac
        branch=$(git -C "$checkout" symbolic-ref --quiet --short HEAD < /dev/null)
        [[ "$branch" == master ]] || fail 'The checkout must be on the master branch.'
        dirty=$(git -C "$checkout" status --porcelain < /dev/null)
        [[ -z "$dirty" ]] || fail 'Commit or stash local changes before updating the checkout.'
        GIT_TERMINAL_PROMPT=0 git -C "$checkout" pull --ff-only origin master < /dev/null
    else
        mkdir -p -- "$(dirname -- "$checkout")"
        GIT_TERMINAL_PROMPT=0 git clone --branch master --single-branch \
            https://github.com/zinin/mimo-grok-adapter.git "$checkout" < /dev/null
    fi
fi

# Keep the checkout for subsequent updates; the Python installer owns rollback.
exec python3 "$checkout/install.py" "$@" < /dev/null
