"""Merge-only push loop: every 180s, re-fetch ONE run (`dbf0daa5`) and merge
into the dashboard data.json. The other 29 curated runs stay frozen as written
by `_rebuild_curated_dashboard.py`.

Why:
    The dashboard is curated to show 3 baselines per model + the live
    `dbf0daa5` (gpt-5.5 xhigh ai_sandbox). Only `dbf0daa5` should update live;
    everything else is fixed. A full push_data rebuild is unnecessary and would
    drag in ablation/oracle runs we deliberately excluded.

Logic (per cycle):
    1. vol.reload()  — pick up latest /data snapshot
    2. Run a tiny helper inside the saas-bench .venv that calls
       push_data.get_run_data(LIVE_RUN_ID) and prints JSON.
    3. Read /monitor_out/data.json, replace the entry for LIVE_RUN_ID, write
       back, monitor_vol.commit().

Per CLAUDE.md rule 10, monitor/push_data.py is NOT modified — this wrapper
just imports `get_run_data` and merges the result.

Usage:
    cd projects/saas-bench
    uv run modal deploy modal_deploy/push_loop.py   # deploy & start scheduling
    modal app stop bossbench-push-loop --yes        # stop the loop
    modal app logs bossbench-push-loop              # tail invocation logs
"""
from __future__ import annotations

import modal

APP_NAME = "bossbench-push-loop"
VOLUME_NAME = "bossbench-modal-runs"
MONITOR_VOLUME_NAME = "bossbench-monitor-data"
SECRET_NAME = "bossbench-keys"

# Runs that update live. Everything else in data.json is frozen.
# Each entry is (run_id, parent_dir_basename). parent_dir is bash_agent_runs
# for bash_agent / oracle / codex (legacy) runs; claude_code_runs for the
# new Claude Code agent. Order doesn't matter — they're refreshed in series.
#
# History:
# - dbf0daa5 frozen at d203 (2026-05-10).
# - c8419b3b: gpt-5.5 / v3.4an_v2 (bash_agent on Modal).
# - 2026-05-12: added 3 claude-code Opus 4.7 max-effort runs.
LIVE_RUNS: list[tuple[str, str]] = [
    # Active run: gemini-3.5-flash bash_agent, seed 42, 500 days, launched
    # 2026-05-15. Kept first so each cycle refreshes it within the 180s budget.
    ("77ef61a2", "bash_agent_runs"),  # gemini35flash_seed42
    ("c8419b3b", "bash_agent_runs"),  # gpt-5.5 v3.4an_v2 (frozen at d490)
    # 3 Claude Code Opus 4.7 max-effort runs launched 2026-05-12 on Modal:
    ("6515e7f2", "claude_code_runs"),  # opus47max_run1_seed42
    ("6257daa3", "claude_code_runs"),  # opus47max_run2_seed42
    ("243dde9d", "claude_code_runs"),  # opus47max_run3_seed42
    # 3 Codex CLI gpt-5.5 high-effort runs launched 2026-05-13 on Modal:
    ("02e9c7fc", "codex_agent_runs"),  # gpt55_codex_run1_seed42 (high)
    ("a4bb8e03", "codex_agent_runs"),  # gpt55_codex_run2_seed42 (high)
    ("ec0ced22", "codex_agent_runs"),  # gpt55_codex_run3_seed42 (high)
    # 3 Codex CLI gpt-5.5 xhigh-effort runs launched 2026-05-13 on Modal:
    ("1cc13216", "codex_agent_runs"),  # gpt55_codex_xhigh_run1_seed42
    ("d762e255", "codex_agent_runs"),  # gpt55_codex_xhigh_run2_seed42
    ("a789c009", "codex_agent_runs"),  # gpt55_codex_xhigh_run3_seed42
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
vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=False)
monitor_vol = modal.Volume.from_name(MONITOR_VOLUME_NAME, create_if_missing=True)
secret = modal.Secret.from_name(SECRET_NAME)


@app.function(
    volumes={"/data": vol, "/monitor_out": monitor_vol},
    secrets=[secret],
    timeout=600,
    schedule=modal.Period(seconds=180),
    max_containers=1,
)
def push_once():
    """Refresh LIVE_RUN_ID and merge into data.json. Scheduled every 180s."""
    import os
    import json
    import subprocess
    import time

    t0 = time.time()
    try:
        vol.reload()
        print("[push_once] vol.reload() ok", flush=True)
    except Exception as e:
        print(f"[push_once] vol.reload() failed (non-fatal): {e}", flush=True)
    try:
        monitor_vol.reload()
        print("[push_once] monitor_vol.reload() ok", flush=True)
    except Exception as e:
        print(f"[push_once] monitor_vol.reload() failed (non-fatal): {e}",
              flush=True)

    cwd = "/data/saas-bench"
    env = os.environ.copy()
    # Hydrate .env on the volume so NMDB_KEY etc. are available even if not in
    # the Modal Secret.
    env_file = f"{cwd}/.env"
    if os.path.exists(env_file):
        with open(env_file) as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                env.setdefault(k, v.strip().strip("'").strip('"'))

    if not os.path.exists(f"{cwd}/monitor/push_data.py"):
        print(f"[push_once] FATAL: {cwd}/monitor/push_data.py missing",
              flush=True)
        return
    if not os.path.isdir(f"{cwd}/.venv"):
        print(f"[push_once] FATAL: {cwd}/.venv missing — run launch.py first.",
              flush=True)
        return

    # Fetch each LIVE run in series, accumulate into `fetched_runs`, then do
    # one read-modify-write of data.json at the end. This avoids partial
    # publishes if one run fails mid-cycle.
    fetched_runs: list[dict] = []
    for run_id, parent_basename in LIVE_RUNS:
        parent_dir = f"{cwd}/{parent_basename}"
        if not os.path.isdir(os.path.join(parent_dir, f"run_{run_id}")):
            print(
                f"[push_once] SKIP {run_id}: run dir not on volume yet "
                f"({parent_dir}/run_{run_id})",
                flush=True,
            )
            continue

        helper = f"""
import json, os, sys
from pathlib import Path

sys.path.insert(0, '{cwd}/monitor')
sys.path.insert(0, '{cwd}/src')

import push_data
push_data.RUN_PARENT['{run_id}'] = Path('{parent_dir}')

d = push_data.get_run_data('{run_id}')
print('===DATA===')
print(json.dumps(d))
"""
        helper_path = f"/tmp/_pushloop_helper_{run_id}.py"
        with open(helper_path, "w") as f:
            f.write(helper)

        try:
            r = subprocess.run(
                ["uv", "run", "python", helper_path],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=420,
            )
        except subprocess.TimeoutExpired:
            print(f"[push_once] {run_id} helper timed out (>420s)", flush=True)
            continue
        except Exception as e:
            print(f"[push_once] {run_id} helper raised: {e}", flush=True)
            continue

        print(
            f"[push_once] {run_id} helper exit={r.returncode}",
            flush=True,
        )
        if r.returncode != 0:
            print(
                f"[push_once] {run_id} STDERR (tail):\n"
                f"{(r.stderr or '')[-1500:]}",
                flush=True,
            )
            continue

        marker = "===DATA===\n"
        if marker not in r.stdout:
            print(
                f"[push_once] {run_id} no DATA marker — STDOUT tail:\n"
                f"{r.stdout[-1500:]}",
                flush=True,
            )
            continue

        try:
            payload = r.stdout.split(marker, 1)[1].strip()
            one_run = json.loads(payload)
        except Exception as e:
            print(
                f"[push_once] {run_id} payload parse failed: {e}",
                flush=True,
            )
            continue

        print(
            f"[push_once] got run_id={one_run.get('run_id')} "
            f"current_day={one_run.get('current_day')} "
            f"last_heartbeat={one_run.get('last_heartbeat')}",
            flush=True,
        )
        fetched_runs.append(one_run)

    if not fetched_runs:
        print("[push_once] nothing fetched; skipping data.json write", flush=True)
        return

    dj_path = "/monitor_out/data.json"
    if not os.path.exists(dj_path):
        print(
            f"[push_once] FATAL: {dj_path} missing — "
            "run _rebuild_curated_dashboard.py first",
            flush=True,
        )
        return

    with open(dj_path) as f:
        all_data = json.load(f)

    runs = all_data.setdefault("runs", [])
    replaced_ids: list[str] = []
    appended_ids: list[str] = []
    for one_run in fetched_runs:
        rid = one_run.get("run_id")
        replaced = False
        for i, r_entry in enumerate(runs):
            if r_entry.get("run_id") == rid:
                runs[i] = one_run
                replaced = True
                break
        if replaced:
            replaced_ids.append(rid)
        else:
            runs.append(one_run)
            appended_ids.append(rid)

    from datetime import datetime, timezone
    all_data["timestamp"] = datetime.now(tz=timezone.utc).isoformat()

    tmp = dj_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(all_data, f)
    os.replace(tmp, dj_path)
    size_mb = os.path.getsize(dj_path) / 1024 / 1024

    try:
        monitor_vol.commit()
    except Exception as e:
        print(f"[push_once] monitor_vol.commit() FAILED: {e}", flush=True)
        return

    dt2 = time.time() - t0
    print(
        f"[push_once] replaced={replaced_ids} appended={appended_ids}; "
        f"{len(runs)} runs total; published {size_mb:.1f} MB "
        f"→ {MONITOR_VOLUME_NAME}/data.json (committed); dt={dt2:.1f}s",
        flush=True,
    )


@app.local_entrypoint()
def main():
    """`modal run modal_deploy/push_loop.py` for a one-shot manual push."""
    push_once.remote()


if __name__ == "__main__":
    app.deploy()
