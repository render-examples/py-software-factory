#!/usr/bin/env bash
# Verifies a factory sandbox: the builder before it is snapshotted, and every sandbox restored from the snapshot.
# Fails unless the layout, Python runtime, credential hygiene, checkout, and permissions are as built.
# Usage: verify.sh
set -euo pipefail

app=$(dirname "${BASH_SOURCE[0]}")
# shellcheck source=/dev/null
. "$app/env"
fail() {
	printf 'verify: %s\n' "$*" >&2
	exit 1
}
as_runner() { bash "$app/as-runner.sh" "$@"; }
[ $# -eq 0 ] || fail "unexpected arguments: $*"

# ── Layout and tools ─────────────────────────────────────────────────────

for d in "$FACTORY_APP_DIR" "$FACTORY_REPO_DIR" "$FACTORY_SCRATCH_DIR"; do [ -d "$d" ] || fail "missing $d"; done
for f in as-runner.sh fileops.py detach.sh lockfile-hash.sh build/git-credential-staged.sh build/scrub-credentials.sh manifest.json; do
	[ -f "$FACTORY_APP_DIR/$f" ] || fail "missing $FACTORY_APP_DIR/$f"
done
for tool in git rg timeout sha256sum; do PATH="$FACTORY_PATH" command -v "$tool" >/dev/null || fail "missing $tool"; done
python_version=$("$FACTORY_PYTHON" -c 'import platform; print(platform.python_version())') || fail "python does not run"
case "$python_version" in 3.12.*) ;; *) fail "python $python_version is not 3.12" ;; esac

# ── Credential hygiene ───────────────────────────────────────────────────
# No stored credentials or credential helpers may survive into the snapshot.

pattern='^(credential\.|url\..*\.insteadof|http\..*\.extraheader)'
for f in "$HOME/.git-credentials" "$FACTORY_SCRATCH_DIR/home/.git-credentials"; do [ ! -e "$f" ] || fail "$f exists"; done
for scope in --system --global; do
	if git config "$scope" --get-regexp "$pattern" >/dev/null 2>&1; then fail "credential settings in git $scope config"; fi
done
if git config --file "$FACTORY_APP_DIR/gitconfig" --get-regexp "$pattern" >/dev/null 2>&1; then fail "credential settings in $FACTORY_APP_DIR/gitconfig"; fi
g() { GIT_OPTIONAL_LOCKS=0 git -c safe.directory='*' -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$FACTORY_REPO_DIR" "$@"; }
[ -d "$FACTORY_REPO_DIR/.git" ] || fail "$FACTORY_REPO_DIR is not a git checkout"
if g config --local --get-regexp "$pattern" >/dev/null 2>&1; then fail "credential settings in $FACTORY_REPO_DIR/.git/config"; fi
url=$(g config --local --get remote.origin.url || true)
case "$url" in *@*) fail "remote URL carries credentials" ;; esac

# ── Manifest and checkout ────────────────────────────────────────────────

commit=$("$FACTORY_PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["sha"])' "$FACTORY_APP_DIR/manifest.json") ||
	fail "cannot read manifest"
head=$(g rev-parse HEAD 2>&1) || fail "$head"
[ "$head" = "$commit" ] || fail "HEAD $head != manifest $commit"
dirty=$(g status --porcelain --untracked-files=no 2>&1) || fail "git status failed: $dirty"
[ -z "$dirty" ] || fail "tracked files modified in $FACTORY_REPO_DIR: $(printf '%s' "$dirty" | head -n 5 | tr '\n' ' ')"

# ── Permissions ──────────────────────────────────────────────────────────

probe=".factory-write-probe-$$"
as_runner touch "$FACTORY_REPO_DIR/$probe" 2>/dev/null || fail "$FACTORY_REPO_DIR is not writable by $FACTORY_RUN_AS"
rm -f "$FACTORY_REPO_DIR/$probe"
as_runner touch "$FACTORY_SCRATCH_DIR/tmp/$probe" || fail "scratch is not writable by $FACTORY_RUN_AS"
rm -f "$FACTORY_SCRATCH_DIR/tmp/$probe"
if [ "$(id -u)" = 0 ] && [ "$FACTORY_RUN_AS" != root ]; then
	if as_runner touch "$FACTORY_APP_DIR/$probe" 2>/dev/null; then
		rm -f "$FACTORY_APP_DIR/$probe"
		fail "$FACTORY_APP_DIR is writable by $FACTORY_RUN_AS"
	fi
fi

# ── Cleanup and report ───────────────────────────────────────────────────
# Only tmp is cleared: HOME may hold dependency caches an install relies on (Go modules, Poetry venvs).

find "$FACTORY_SCRATCH_DIR/tmp" -mindepth 1 -delete

"$FACTORY_PYTHON" -c '
import json, os, platform
print(json.dumps({
    "runAs": os.environ["FACTORY_RUN_AS"],
    "uid": os.getuid(),
    "python": platform.python_version(),
}))
'
