#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export SAASBENCH_SOURCE_DIR="${SAASBENCH_SOURCE_DIR:-${REPO_DIR}/../saas-bench}"

cd "${REPO_DIR}"
uv run modal deploy modal_deploy/push_loop.py

