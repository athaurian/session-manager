#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/../.." && pwd)

IMAGE_I3=${IMAGE_I3:-session-manager-i3:test}
IMAGE_SWAY=${IMAGE_SWAY:-session-manager-sway:test}

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required for the integration tests" >&2
    exit 1
fi

build_if_missing() {
    image=$1
    dockerfile=$2
    if docker image inspect "$image" >/dev/null 2>&1; then
        return
    fi
    echo "building $image from $dockerfile"
    docker build -f "$script_dir/$dockerfile" -t "$image" "$script_dir"
}

run_i3() {
    build_if_missing "$IMAGE_I3" Dockerfile.i3
    docker run --rm --network none \
        -v "$repo_root:/src:ro" \
        -v "$script_dir/i3-roundtrip.sh:/test.sh:ro" \
        "$IMAGE_I3" bash /test.sh
}

run_sway() {
    build_if_missing "$IMAGE_SWAY" Dockerfile.sway
    docker run --rm --network none \
        -v "$repo_root:/src:ro" \
        -v "$script_dir/sway-roundtrip.sh:/test.sh:ro" \
        "$IMAGE_SWAY" dbus-run-session -- bash /test.sh
}

case "${1:-all}" in
    all)
        run_i3
        run_sway
        ;;
    i3)
        run_i3
        ;;
    sway)
        run_sway
        ;;
    *)
        echo "usage: $0 [all|i3|sway]" >&2
        exit 2
        ;;
esac

echo "integration tests passed"
