"""Scheduled push loop for the three Together GLM-5.2 CeoBench runs."""

from __future__ import annotations

import modal


APP_NAME = "ceobench-glm52-push-loop"
RUN_VOLUME_NAME = "bossbench-modal-runs"
MONITOR_VOLUME_NAME = "bossbench-glm52-monitor-data"
SECRET_NAME = "bossbench-keys"

LIVE_RUNS: list[tuple[str, str]] = [
    ("899cf4c0", "bash_agent_runs"),  # glm52_together_max_seed42_run1
    ("1a28c744", "bash_agent_runs"),  # glm52_together_max_seed42_run2
    ("00b47f3c", "bash_agent_runs"),  # glm52_together_max_seed42_run3
]

push_image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("curl", "ca-certificates")
    .run_commands(
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "cp /root/.local/bin/uv /usr/local/bin/uv",
    )
)

app = modal.App(APP_NAME, image=push_image)
run_vol = modal.Volume.from_name(RUN_VOLUME_NAME, create_if_missing=False)
monitor_vol = modal.Volume.from_name(MONITOR_VOLUME_NAME, create_if_missing=True)
secret = modal.Secret.from_name(SECRET_NAME)


@app.function(
    volumes={"/data": run_vol, "/monitor_out": monitor_vol},
    secrets=[secret],
    timeout=900,
    schedule=modal.Period(seconds=60),
    max_containers=1,
)
def push_once():
    import json
    import os
    import subprocess
    import time
    from datetime import datetime, timezone

    t0 = time.time()
    try:
        run_vol.reload()
        print("[push_once] run_vol.reload() ok", flush=True)
    except Exception as e:
        print(f"[push_once] run_vol.reload() failed (non-fatal): {e}", flush=True)
    try:
        monitor_vol.reload()
        print("[push_once] monitor_vol.reload() ok", flush=True)
    except Exception as e:
        print(f"[push_once] monitor_vol.reload() failed (non-fatal): {e}", flush=True)

    cwd = "/data/saas-bench"
    env = os.environ.copy()
    env_file = f"{cwd}/.env"
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env.setdefault(key, value.strip().strip("'").strip('"'))

    fetched_runs: list[dict] = []
    for run_id, parent_basename in LIVE_RUNS:
        parent_dir = f"{cwd}/{parent_basename}"
        run_dir = os.path.join(parent_dir, f"run_{run_id}")
        if not os.path.isdir(run_dir):
            print(f"[push_once] SKIP {run_id}: missing {run_dir}", flush=True)
            continue

        helper = f"""
import json, sys
from pathlib import Path

sys.path.insert(0, '{cwd}/monitor')
sys.path.insert(0, '{cwd}/src')

import push_data
push_data.RUN_PARENT['{run_id}'] = Path('{parent_dir}')

print('===DATA===')
print(json.dumps(push_data.get_run_data('{run_id}')))
"""
        helper_path = f"/tmp/_push_glm52_{run_id}.py"
        with open(helper_path, "w") as f:
            f.write(helper)

        try:
            result = subprocess.run(
                ["uv", "run", "python", helper_path],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            print(f"[push_once] {run_id} helper timed out", flush=True)
            continue

        print(f"[push_once] {run_id} helper exit={result.returncode}", flush=True)
        if result.returncode != 0:
            print(f"[push_once] {run_id} stderr tail:\n{result.stderr[-1500:]}", flush=True)
            continue

        marker = "===DATA===\n"
        if marker not in result.stdout:
            print(f"[push_once] {run_id} no DATA marker; stdout tail:\n{result.stdout[-1500:]}", flush=True)
            continue

        try:
            payload = result.stdout.split(marker, 1)[1].strip()
            one_run = json.loads(payload)
        except Exception as e:
            print(f"[push_once] {run_id} parse failed: {e}", flush=True)
            continue

        print(
            f"[push_once] got {one_run.get('run_id')} "
            f"day={one_run.get('current_day')} label={one_run.get('label')}",
            flush=True,
        )
        fetched_runs.append(one_run)

    if not fetched_runs:
        print("[push_once] no runs fetched; not writing data.json", flush=True)
        return

    all_data = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "runs": fetched_runs,
    }
    out_path = "/monitor_out/data.json"
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(all_data, f)
    os.replace(tmp_path, out_path)
    monitor_vol.commit()

    print(
        f"[push_once] published {len(fetched_runs)} runs to "
        f"{MONITOR_VOLUME_NAME}/data.json in {time.time() - t0:.1f}s",
        flush=True,
    )


@app.local_entrypoint()
def main():
    push_once.remote()


if __name__ == "__main__":
    app.deploy()

