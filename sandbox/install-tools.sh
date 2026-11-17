#!/usr/bin/env bash
# Installs the small OS tool set plus pinned uv, uv-managed CPython 3.12, and the runner virtualenv.
# Usage: install-tools.sh
set -euo pipefail

log() { printf 'install-tools: %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
UV_VERSION=0.11.18
PYTHON_VERSION=3.12.13
root=/opt/factory

[ "$(id -u)" = 0 ] || {
	log "must run as root"
	exit 1
}
pkgs=""
have git || pkgs="$pkgs git"
have rg || pkgs="$pkgs ripgrep"
have timeout || pkgs="$pkgs coreutils"
have setpriv || pkgs="$pkgs util-linux"
have useradd || pkgs="$pkgs passwd"
have curl || pkgs="$pkgs curl"
if [ -n "$pkgs" ]; then
	log "apt-get install$pkgs"
	export DEBIAN_FRONTEND=noninteractive
	# The image's Debian sources use plain HTTP, and the sandbox egress allow-list admits only HTTPS.
	for src in /etc/apt/sources.list /etc/apt/sources.list.d/*; do
		if [ -f "$src" ]; then sed -i 's|http://deb\.debian\.org/|https://deb.debian.org/|g' "$src"; fi
	done
	apt-get update -qq >&2
	# shellcheck disable=SC2086 # intentional word splitting of the package list
	apt-get install -y -qq --no-install-recommends ca-certificates $pkgs >&2
	rm -rf /var/lib/apt/lists/*
fi

missing=""
for tool in git rg timeout setpriv setsid useradd curl tar sha256sum; do have "$tool" || missing="$missing $tool"; done
if [ -n "$missing" ]; then
	log "missing tools:$missing"
	exit 1
fi

mkdir -p "$root/bin" "$root/python"
uv="$root/bin/uv"
if [ ! -x "$uv" ] || [ "$("$uv" --version | awk '{print $2}')" != "$UV_VERSION" ]; then
	archive=$(mktemp)
	trap 'rm -f "$archive"' EXIT
	name="uv-x86_64-unknown-linux-gnu"
	curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${name}.tar.gz" -o "$archive"
	tar -xzf "$archive" --strip-components=1 -C "$root/bin" "$name/uv"
	chmod 0755 "$uv"
	rm -f "$archive"
	trap - EXIT
fi

export UV_PYTHON_INSTALL_DIR="$root/python"
export UV_PYTHON_PREFERENCE=only-managed
"$uv" python install "$PYTHON_VERSION"
"$uv" venv --clear --python "$PYTHON_VERSION" --python-preference only-managed "$root/venv"
"$uv" pip install --python "$root/venv/bin/python" --require-hashes -r "$root/app/runner-requirements.txt"

version=$("$root/venv/bin/python" -c 'import platform; print(platform.python_version())')
case "$version" in 3.12.*) ;; *) log "expected Python 3.12, got $version"; exit 1 ;; esac
log "git $(git --version | cut -d' ' -f3), rg $(rg --version | awk 'NR == 1 {print $2}'), uv $UV_VERSION, python $version"
