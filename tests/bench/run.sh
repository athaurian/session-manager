#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/../.." && pwd)
image=${IMAGE_SWAY:-session-manager-sway:test}
dockerfile="$repo_root/tests/integration/Dockerfile.sway"

if ! command -v docker >/dev/null 2>&1; then
	echo "docker is required for the benchmark" >&2
	exit 1
fi

container_args=()
mounts=(-v "$repo_root:/src:ro")
while [ "$#" -gt 0 ]; do
	case "$1" in
	--baseline | --baseline=*)
		if [ "$1" = "--baseline" ]; then
			if [ "$#" -lt 2 ]; then
				echo "--baseline needs a path" >&2
				exit 2
			fi
			baseline_path=$2
			shift 2
		else
			baseline_path=${1#--baseline=}
			shift
		fi
		baseline=$(cd "$baseline_path" && pwd)
		mounts+=(-v "$baseline:/baseline:ro")
		container_args+=(--baseline /baseline)
		;;
	*)
		container_args+=("$1")
		shift
		;;
	esac
done

if ! docker image inspect "$image" >/dev/null 2>&1; then
	echo "building $image from tests/integration/Dockerfile.sway"
	docker build -f "$dockerfile" -t "$image" "$repo_root/tests/integration"
fi

docker run --rm --network none \
	"${mounts[@]}" \
	-e PYTHONDONTWRITEBYTECODE=1 \
	"$image" bash -c '
		mkdir -p /tmp/xdg && chmod 700 /tmp/xdg
		if ! dbus-run-session -- python3 /src/tests/bench/run.py --repo /src "$@" 2>/tmp/bench-dbus.log; then
			cat /tmp/bench-dbus.log >&2
			exit 1
		fi
		grep -v "ignoring unknown child process" /tmp/bench-dbus.log >&2 || true
	' bench "${container_args[@]}"
