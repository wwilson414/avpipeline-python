#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SHAKA_REPOSITORY="${SHAKA_REPOSITORY:-https://github.com/shaka-project/shaka-packager.git}"
SHAKA_VERSION="${SHAKA_VERSION:-v3.9.3}"
SHAKA_SOURCE_DIR="${SHAKA_SOURCE_DIR:-${PROJECT_DIR}/build/shaka-packager/${SHAKA_VERSION}/source}"
BUILD_DIR="${BUILD_DIR:-${PROJECT_DIR}/build/shaka-packager/${SHAKA_VERSION}/build}"
BUILD_TYPE="${BUILD_TYPE:-}"
PARALLEL="${PARALLEL:-}"
PACKAGER_VERSION="${PACKAGER_VERSION:-}"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Clone Shaka Packager from the project repository, apply this project's
HLS discontinuity patch, and build the standalone packager executable.

Options:
  --repository URL       Shaka Git repository
                         (default: ${SHAKA_REPOSITORY})
  --version TAG          Supported tag: v3.9.3
                         (default: ${SHAKA_VERSION})
  --source-dir DIR       Source checkout path (default: ${SHAKA_SOURCE_DIR})
  --build-dir DIR        CMake build directory (default: ${BUILD_DIR})
  --build-type TYPE      CMake build type, such as Release
  --parallel N           Maximum parallel build jobs
  --packager-version V   Version label embedded in the Packager binary
  -h, --help             Show this help

Environment defaults: SHAKA_REPOSITORY, SHAKA_VERSION, SHAKA_SOURCE_DIR,
BUILD_DIR, BUILD_TYPE, PARALLEL, and PACKAGER_VERSION.

This script builds Shaka Packager only; it does not build the Python
application or install the resulting executable.
EOF
}

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

while (($# > 0)); do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --repository|--version|--source-dir|--build-dir|--build-type|--parallel|--packager-version)
            (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] ||
                fail "$1 requires a value"
            case "$1" in
                --repository) SHAKA_REPOSITORY="$2" ;;
                --version) SHAKA_VERSION="$2" ;;
                --source-dir) SHAKA_SOURCE_DIR="$2" ;;
                --build-dir) BUILD_DIR="$2" ;;
                --build-type) BUILD_TYPE="$2" ;;
                --parallel) PARALLEL="$2" ;;
                --packager-version) PACKAGER_VERSION="$2" ;;
            esac
            shift 2
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

case "${SHAKA_VERSION}" in
    v3.9.3) ;;
    *) fail "unsupported Shaka version ${SHAKA_VERSION}; use v3.9.3" ;;
esac
[[ -n "${SHAKA_REPOSITORY}" ]] || fail "Shaka repository URL must not be empty"
if [[ -n "${PARALLEL}" ]]; then
    [[ "${PARALLEL}" =~ ^[1-9][0-9]*$ ]] || fail "--parallel requires a positive integer"
fi
if [[ -n "${PACKAGER_VERSION}" ]]; then
    [[ "${PACKAGER_VERSION}" =~ ^[A-Za-z0-9][A-Za-z0-9.+_-]*$ ]] ||
        fail "--packager-version must contain only letters, digits, '.', '+', '_' or '-'"
fi

for tool in git cmake ninja python3; do
    command -v "${tool}" >/dev/null 2>&1 || fail "${tool} is required but was not found in PATH"
done

if [[ -e "${SHAKA_SOURCE_DIR}" ]]; then
    [[ -d "${SHAKA_SOURCE_DIR}" ]] || fail "Shaka source path is not a directory: ${SHAKA_SOURCE_DIR}"
    SHAKA_SOURCE_DIR="$(cd "${SHAKA_SOURCE_DIR}" && pwd -P)" ||
        fail "could not access Shaka source directory"
    GIT_ROOT="$(git -C "${SHAKA_SOURCE_DIR}" rev-parse --show-toplevel 2>/dev/null)" ||
        fail "existing source path is not a Git checkout: ${SHAKA_SOURCE_DIR}"
    GIT_ROOT="$(cd "${GIT_ROOT}" && pwd -P)"
    [[ "${GIT_ROOT}" == "${SHAKA_SOURCE_DIR}" ]] ||
        fail "source path must be the Shaka repository root: ${SHAKA_SOURCE_DIR}"
    EXPECTED_COMMIT="$(git -C "${SHAKA_SOURCE_DIR}" rev-parse --verify \
        "${SHAKA_VERSION}^{commit}" 2>/dev/null)" ||
        fail "tag ${SHAKA_VERSION} is not present in ${SHAKA_SOURCE_DIR}"
    ACTUAL_COMMIT="$(git -C "${SHAKA_SOURCE_DIR}" rev-parse HEAD)"
    [[ "${ACTUAL_COMMIT}" == "${EXPECTED_COMMIT}" ]] ||
        fail "existing Shaka checkout is not at ${SHAKA_VERSION}; refusing to switch revisions"
else
    mkdir -p "$(dirname "${SHAKA_SOURCE_DIR}")"
    printf 'Cloning Shaka Packager %s from %s into %s\n' \
        "${SHAKA_VERSION}" "${SHAKA_REPOSITORY}" "${SHAKA_SOURCE_DIR}"
    git clone --depth 1 --branch "${SHAKA_VERSION}" --recurse-submodules \
        "${SHAKA_REPOSITORY}" "${SHAKA_SOURCE_DIR}" ||
        fail "could not clone Shaka Packager ${SHAKA_VERSION}"
    SHAKA_SOURCE_DIR="$(cd "${SHAKA_SOURCE_DIR}" && pwd -P)"
fi

git -C "${SHAKA_SOURCE_DIR}" submodule update --init --recursive --depth 1 ||
    fail "could not initialize Shaka Packager submodules"

SUBMODULE_STATUS="$(git -C "${SHAKA_SOURCE_DIR}" submodule status --recursive)" ||
    fail "could not inspect Shaka Packager submodule status"
while IFS= read -r status_line; do
    case "${status_line:0:1}" in
        -|+|U) fail "Shaka submodule is incomplete or at the wrong revision: ${status_line}" ;;
    esac
done <<< "${SUBMODULE_STATUS}"

"${SCRIPT_DIR}/apply-shaka-packager-patch.sh" "${SHAKA_SOURCE_DIR}"

mkdir -p "${BUILD_DIR}"
BUILD_DIR="$(cd "${BUILD_DIR}" && pwd -P)"
[[ "${BUILD_DIR}" != "${SHAKA_SOURCE_DIR}" ]] ||
    fail "build directory must not be the Shaka source root"
[[ "${SHAKA_SOURCE_DIR}/" != "${BUILD_DIR}/"* ]] ||
    fail "build directory must not be inside the Shaka source tree"

if [[ -f "${BUILD_DIR}/CMakeCache.txt" ]]; then
    CACHED_SOURCE="$(sed -n 's/^CMAKE_HOME_DIRECTORY:INTERNAL=//p' \
        "${BUILD_DIR}/CMakeCache.txt")"
    [[ -z "${CACHED_SOURCE}" || \
       "$(cd "${CACHED_SOURCE}" && pwd -P)" == "${SHAKA_SOURCE_DIR}" ]] ||
        fail "build directory is configured for a different source tree: ${BUILD_DIR}"
fi

mkdir -p "${BUILD_DIR}/.cmake/api/v1/query/client-python-shaka"
touch "${BUILD_DIR}/.cmake/api/v1/query/client-python-shaka/codemodel-v2"

configure_args=(-S "${SHAKA_SOURCE_DIR}" -B "${BUILD_DIR}" -G Ninja -DSKIP_INTEGRATION_TESTS=ON)
[[ -z "${BUILD_TYPE}" ]] || configure_args+=("-DCMAKE_BUILD_TYPE=${BUILD_TYPE}")
[[ -z "${PACKAGER_VERSION}" ]] || configure_args+=("-DPACKAGER_VERSION=${PACKAGER_VERSION}")
cmake "${configure_args[@]}" || fail "CMake configuration failed"

build_args=(--build "${BUILD_DIR}" --target packager)
[[ -z "${BUILD_TYPE}" ]] || build_args+=(--config "${BUILD_TYPE}")
[[ -z "${PARALLEL}" ]] || build_args+=(--parallel "${PARALLEL}")
cmake "${build_args[@]}" || fail "CMake failed to build the packager target"

ARTIFACT="$(python3 - "${BUILD_DIR}" "${BUILD_TYPE}" <<'PY'
import glob
import json
import os
import sys

build_dir, build_type = sys.argv[1:]
reply_dir = os.path.join(build_dir, ".cmake", "api", "v1", "reply")
indexes = glob.glob(os.path.join(reply_dir, "index-*.json"))
if not indexes:
    raise SystemExit("CMake File API index is missing")
with open(max(indexes, key=os.path.getmtime), encoding="utf-8") as file:
    index = json.load(file)
reply = index.get("reply", {}).get("client-python-shaka", {}).get("codemodel-v2")
if not reply:
    raise SystemExit("CMake File API codemodel-v2 reply is missing")
with open(os.path.join(reply_dir, reply["jsonFile"]), encoding="utf-8") as file:
    codemodel = json.load(file)
configurations = codemodel["configurations"]
if build_type:
    configurations = [item for item in configurations if item.get("name", "") == build_type]
targets = [target for config in configurations for target in config["targets"]
           if target["name"] == "packager"]
if len(targets) != 1:
    raise SystemExit("CMake did not identify exactly one packager target")
with open(os.path.join(reply_dir, targets[0]["jsonFile"]), encoding="utf-8") as file:
    target = json.load(file)
artifacts = target.get("artifacts", [])
if target.get("type") != "EXECUTABLE" or len(artifacts) != 1:
    raise SystemExit("CMake packager target has no unique executable artifact")
path = artifacts[0]["path"]
print(os.path.realpath(path if os.path.isabs(path) else os.path.join(build_dir, path)))
PY
)" || fail "could not locate the built Packager executable"

[[ "${ARTIFACT}" == "${BUILD_DIR}/"* ]] ||
    fail "CMake reported an executable outside the build tree: ${ARTIFACT}"
[[ -f "${ARTIFACT}" && -x "${ARTIFACT}" ]] ||
    fail "Packager executable is missing or not executable: ${ARTIFACT}"

printf 'Built Shaka Packager executable: %s\n' "${ARTIFACT}"
printf 'Add it to PATH, for example:\n  export PATH="%s:$PATH"\n' "$(dirname "${ARTIFACT}")"
