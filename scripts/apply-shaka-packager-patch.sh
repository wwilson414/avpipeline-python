#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PATCH_FILE="${PROJECT_DIR}/patches/shaka-v3.9.3-hls-discontinuity.patch"

usage() {
    cat <<EOF
Usage: $(basename "$0") SHAKA_SOURCE_DIR

Apply the Python project's HLS discontinuity patch to a Shaka Packager
v3.9.3 or v3.9.3-avpipeline.1 Git checkout. If the patch is already applied,
the command succeeds without changing files.

This script applies the patch only; build Shaka using its upstream build
instructions, then put the resulting packager executable on PATH.
EOF
}

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

if (($# == 1)) && [[ "$1" == "--help" || "$1" == "-h" ]]; then
    usage
    exit 0
fi

(($# == 1)) || {
    usage >&2
    exit 2
}

command -v git >/dev/null 2>&1 || fail "git is required"
[[ -f "${PATCH_FILE}" ]] || fail "project patch is missing: ${PATCH_FILE}"
[[ -d "$1" ]] || fail "Shaka source directory does not exist: $1"

SHAKA_SOURCE_DIR="$(cd "$1" && pwd -P)" ||
    fail "could not access Shaka source directory: $1"
GIT_ROOT="$(git -C "${SHAKA_SOURCE_DIR}" rev-parse --show-toplevel 2>/dev/null)" ||
    fail "source directory is not inside a Git checkout: ${SHAKA_SOURCE_DIR}"
GIT_ROOT="$(cd "${GIT_ROOT}" && pwd -P)"
[[ "${GIT_ROOT}" == "${SHAKA_SOURCE_DIR}" ]] ||
    fail "pass the Shaka repository root, not a subdirectory: ${SHAKA_SOURCE_DIR}"

SHAKA_VERSION="$(git -C "${SHAKA_SOURCE_DIR}" describe --tags --exact-match HEAD 2>/dev/null)" ||
    fail "Shaka HEAD must be tagged v3.9.3 or v3.9.3-avpipeline.1"
case "${SHAKA_VERSION}" in
    v3.9.3|v3.9.3-avpipeline.1) ;;
    *) fail "unsupported Shaka tag ${SHAKA_VERSION}; expected v3.9.3 or v3.9.3-avpipeline.1" ;;
esac

if git -C "${SHAKA_SOURCE_DIR}" apply --reverse --check \
    "${PATCH_FILE}" >/dev/null 2>&1; then
    printf 'Shaka HLS discontinuity patch is already applied to %s\n' "${SHAKA_VERSION}"
    exit 0
fi

if ! git -C "${SHAKA_SOURCE_DIR}" apply --check "${PATCH_FILE}"; then
    fail "patch does not apply cleanly to ${SHAKA_VERSION} at ${SHAKA_SOURCE_DIR}"
fi

git -C "${SHAKA_SOURCE_DIR}" apply "${PATCH_FILE}" ||
    fail "could not apply the Shaka HLS discontinuity patch"

printf 'Applied Shaka HLS discontinuity patch to %s at %s\n' \
    "${SHAKA_VERSION}" "${SHAKA_SOURCE_DIR}"
