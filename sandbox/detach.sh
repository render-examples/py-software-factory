#!/usr/bin/env bash
# Starts one bounded command independently of the short Sandbox exec stream.
# Usage: detach.sh /tmp/factory-detached-<hex> <timeout_seconds> <command> [args...]
set -euo pipefail

run_dir=${1:?usage: detach.sh <run_dir> <timeout_seconds> <command> [args...]}
seconds=${2:?usage: detach.sh <run_dir> <timeout_seconds> <command> [args...]}
shift 2
[ "$#" -gt 0 ] || {
	echo "detach: command is required" >&2
	exit 2
}
case "$run_dir" in /tmp/factory-detached-[0-9a-f]*) ;; *) echo "detach: unsafe run directory" >&2; exit 2 ;; esac
[[ $seconds =~ ^[0-9]+([.][0-9]+)?$ ]] || {
	echo "detach: timeout must be a positive number" >&2
	exit 2
}
[ "$seconds" != 0 ] || {
	echo "detach: timeout must be positive" >&2
	exit 2
}
if [ -e "$run_dir" ] || [ -L "$run_dir" ]; then
	echo "detach: run directory already exists" >&2
	exit 2
fi
mkdir -m 0700 "$run_dir"

worker="$run_dir/worker.sh"
cat >"$worker" <<'WORKER'
#!/usr/bin/env bash
set -u
run_dir=$1
seconds=$2
shift 2
set +e
timeout --kill-after=10s "$seconds" "$@" >"$run_dir/stdout" 2>"$run_dir/stderr"
status=$?
set -e
printf '%s\n' "$status" >"$run_dir/exit.next"
mv "$run_dir/exit.next" "$run_dir/exit"
WORKER
chmod 0700 "$worker"

nohup setsid -f bash "$worker" "$run_dir" "$seconds" "$@" </dev/null >/dev/null 2>&1 &
printf '%s\n' "$!" >"$run_dir/launcher.pid"
