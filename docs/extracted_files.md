# Extracted Files

The initial repo was extracted from `projects/saas-bench` at source commit `607b30a`.

| New path | Source path | Purpose |
| --- | --- | --- |
| `modal_deploy/launch_bash_agent.py` | `modal_deploy/launch.py` | Launch a `bash_agent` run inside a Modal Sandbox. Patched to read `SAASBENCH_SOURCE_DIR` and write sandbox state under this ops repo. |
| `modal_deploy/push_loop.py` | `modal_deploy/push_loop.py` | Scheduled Modal job that refreshes selected run summaries into `bossbench-monitor-data/data.json`. |
| `monitor/modal_app.py` | `monitor/modal_app.py` | Modal ASGI dashboard app that serves run comparison/detail views from `data.json`. |
| `monitor/push_data.py` | `monitor/push_data.py` | Extracts run metrics, traces, social posts, forecasts, and timing into dashboard JSON, then uploads it to Modal. Patched to read `SAASBENCH_SOURCE_DIR`. |
| `monitor/monitor_bash_agent.py` | `monitor_bash_agent.py` | Local terminal monitor for `bash_agent` JSONL logs, workspace state, and hidden DB stats. |
| `scripts/create_modal_secret.sh` | `modal_deploy/create_secret.sh` | Creates the `bossbench-keys` Modal secret from a SaaSBench `.env`. Patched to be portable. |

The full SaaSBench simulation remains in a separate checkout. This repo owns only launch/monitor/push operational code.

