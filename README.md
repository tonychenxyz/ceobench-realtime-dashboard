# SaaSBench Bash Agent + Modal Dashboard Ops

This repo extracts the existing operational code used to:

- launch SaaSBench `bash_agent` runs inside Modal Sandboxes,
- deploy the Modal monitoring dashboard,
- push run data into the dashboard Modal Volume,
- watch a local `bash_agent` run from a terminal.

It is an ops repo. It does not vendor the full SaaSBench simulation source. Point it at an existing SaaSBench checkout with `SAASBENCH_SOURCE_DIR`.

## Layout

```text
modal_deploy/launch_bash_agent.py  # Modal Sandbox launcher for bash_agent
modal_deploy/push_loop.py          # Scheduled Modal refresher for curated/live runs
monitor/modal_app.py               # Modal ASGI dashboard app
monitor/push_data.py               # Extract run summaries and upload data.json
monitor/monitor_bash_agent.py      # Local terminal monitor for a run directory
scripts/*.sh                       # Portable command wrappers
docs/extracted_files.md            # Provenance and role of copied files
```

## Setup

```bash
cd projects/saasbench-base-agent-dashboard
export SAASBENCH_SOURCE_DIR="$PWD/../saas-bench"
uv sync
```

Create or refresh the Modal secret from the SaaSBench `.env`:

```bash
bash scripts/create_modal_secret.sh
```

The secret script reads Modal credentials from `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` if set, otherwise from `MODAL_CONFIG` and `MODAL_PROFILE_NAME` (default `princeton-tony`).

## Launch a Bash Agent on Modal

```bash
bash scripts/launch_bash_agent_modal.sh \
  --model gpt-5.5 \
  --provider openai \
  --effort xhigh \
  --days 500 \
  --seed 42 \
  --label gpt55_baseline_seed42
```

The launcher packages `SAASBENCH_SOURCE_DIR` into the Modal image, syncs it into the persistent `bossbench-modal-runs` volume, runs `uv sync`, verifies the bash sandbox isolation, optionally starts `monitor/push_data.py` as a sidecar, and backgrounds the agent process.

## Dashboard

Deploy the dashboard:

```bash
bash scripts/deploy_dashboard.sh
```

Push current local/volume run data once:

```bash
bash scripts/push_data_once.sh
```

Run a continuous local push loop:

```bash
bash scripts/push_data_once.sh --loop 60
```

Deploy the scheduled Modal push loop:

```bash
bash scripts/deploy_push_loop.sh
```

`monitor/modal_app.py` serves `/` and `/api/data` from the `bossbench-monitor-data` Modal Volume.

## Local Run Monitor

```bash
bash scripts/monitor_bash_agent.sh "$SAASBENCH_SOURCE_DIR/bash_agent_runs/run_<id>"
```

This uses the extracted `monitor/monitor_bash_agent.py` and expects the run directory to contain `world.db`, `logs/`, and `agent_workspace/`.

