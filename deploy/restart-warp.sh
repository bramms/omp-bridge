#!/bin/bash
set -euo pipefail

usage() {
    printf 'Usage: %s NUMBER [NUMBER ...]\n       %s --429 [--model MODEL]\nNumbers: 1–10. Example: %s 3 7\n' \
        "${0##*/}" "${0##*/}" "${0##*/}"
}

if [[ $# == 0 ]]; then
    usage >&2
    exit 2
fi
if [[ $# == 1 && ( $1 == -h || $1 == --help ) ]]; then
    usage
    exit 0
fi

deploy_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $1 == --429 ]]; then
    shift
    if [[ $# == 0 ]]; then
        set -- --model deepseek-v4.1-flash-free
    elif [[ $# != 2 || $1 != --model || -z $2 ]]; then
        usage >&2
        exit 2
    fi
    exec "${OMP_FREE_PYTHON:-python3}" "$deploy_dir/../bridge.py" probe "$@" --route all --restart-429
fi

letters=(a b c d e f g h i j)
services=()
for number in "$@"; do
    case "$number" in
        [1-9]|10)
            services+=("warp-${letters[number - 1]}")
            ;;
        *)
            printf 'Invalid WARP number: %s\n' "$number" >&2
            usage >&2
            exit 2
            ;;
    esac
done

docker_cli=(docker)
if [[ "$(uname -s)" == Darwin ]]; then
    docker_cli+=(--context colima)
fi
exec "${docker_cli[@]}" compose -f "$deploy_dir/warp/compose.yaml" restart "${services[@]}"
