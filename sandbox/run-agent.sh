#!/usr/bin/env bash
# Runs protocol-v3 once as the runner user, bounded by coreutils timeout.
# Usage: run-agent.sh <run_dir> <timeout_seconds>
# <run_dir>/input.json is fed directly on stdin. No model key enters the sandbox: Render's sandbox-group
# credential broker injects it on the allowed provider request. Output files sit beside <run_dir>, root-owned,
# so repository code cannot rewrite what the host reads.
set -euo pipefail

run_dir=${1:?usage: run-agent.sh <run_dir> <timeout_seconds>}
seconds=${2:?usage: run-agent.sh <run_dir> <timeout_seconds>}
run_dir=${run_dir%/}
app=$(dirname "${BASH_SOURCE[0]}")
# shellcheck source=/dev/null
. "$app/env"

if [ ! -f "$run_dir/input.json" ]; then
	echo "run-agent: $run_dir/input.json is missing" >&2
	exit 1
fi
if [ "$(id -u)" = 0 ] && [ "$FACTORY_RUN_AS" != root ]; then chown -R "$FACTORY_RUN_AS": "$run_dir"; fi

set +e
cat "$run_dir/input.json" |
	timeout --kill-after=10s "$seconds" bash "$app/as-runner.sh" \
		env PYTHONPATH="$FACTORY_APP_DIR/runner.zip" "$FACTORY_PYTHON" -m factory.runner >"$run_dir.jsonl" 2>"$run_dir.stderr"
status=${PIPESTATUS[1]}
set -e
echo "$status" >"$run_dir.exit"
exit "$status"
