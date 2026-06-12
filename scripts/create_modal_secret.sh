#!/bin/bash
# Create the Modal Secret `bossbench-keys` from a SaaSBench `.env`.
# Idempotent — uses `modal secret create` which is upsert.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OPS_REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_SOURCE_DIR="$(cd "${OPS_REPO_DIR}/../saas-bench" 2>/dev/null && pwd || true)"
SAASBENCH_SOURCE_DIR="${SAASBENCH_SOURCE_DIR:-${DEFAULT_SOURCE_DIR}}"
ENV_FILE="${SAASBENCH_ENV_FILE:-${SAASBENCH_SOURCE_DIR}/.env}"

if [ -z "${SAASBENCH_SOURCE_DIR}" ] || [ ! -f "${ENV_FILE}" ]; then
    echo "ERROR: SaaSBench .env not found. Set SAASBENCH_SOURCE_DIR or SAASBENCH_ENV_FILE." >&2
    exit 1
fi

SECRET_NAME="bossbench-keys"
DASHBOARD_URL="https://princeton-tony--ceobench-dashboard-ceobenchdashboard.us-east.modal.direct"
MODAL_PROFILE_NAME="${MODAL_PROFILE_NAME:-princeton-tony}"
MODAL_CONFIG="${MODAL_CONFIG:-${HOME}/.modal.toml}"

# Read all KEY=VALUE pairs from .env (skip blanks/comments)
ARGS=()
while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    ARGS+=("$line")
done < "${ENV_FILE}"

# Add Modal auth tokens (so push_data CLI inside sandbox can talk to Modal)
MODAL_ID="${MODAL_TOKEN_ID:-}"
MODAL_SECRET="${MODAL_TOKEN_SECRET:-}"
if [ -z "${MODAL_ID}" ] || [ -z "${MODAL_SECRET}" ]; then
    MODAL_ID=$(awk -v profile="${MODAL_PROFILE_NAME}" '$0=="["profile"]"{p=1;next} /^\[/{p=0} p&&/token_id/{gsub(/"/,""); print $3; exit}' "${MODAL_CONFIG}")
    MODAL_SECRET=$(awk -v profile="${MODAL_PROFILE_NAME}" '$0=="["profile"]"{p=1;next} /^\[/{p=0} p&&/token_secret/{gsub(/"/,""); print $3; exit}' "${MODAL_CONFIG}")
fi
ARGS+=("MODAL_TOKEN_ID=${MODAL_ID}")
ARGS+=("MODAL_TOKEN_SECRET=${MODAL_SECRET}")
ARGS+=("CEOBENCH_DASHBOARD_URL=${DASHBOARD_URL}")

echo "Creating/updating Modal secret '${SECRET_NAME}' with ${#ARGS[@]} keys..."
modal secret create "${SECRET_NAME}" "${ARGS[@]}" --force
echo "✅ Secret '${SECRET_NAME}' written."
