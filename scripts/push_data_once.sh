#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export SAASBENCH_SOURCE_DIR="${SAASBENCH_SOURCE_DIR:-${REPO_DIR}/../saas-bench}"
export BOSSBENCH_PUSH_DATA_TMP_DIR="${BOSSBENCH_PUSH_DATA_TMP_DIR:-${REPO_DIR}/.tmp/push_data_plain_$(id -u)}"

cd "${REPO_DIR}"
uv run python monitor/push_data.py "$@"

