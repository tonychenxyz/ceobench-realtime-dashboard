"""Launch a bossbench bash_agent run inside a Modal Sandbox.

Usage:
    cd projects/saasbench-base-agent-dashboard
    bash scripts/create_modal_secret.sh       # one-time setup
    SAASBENCH_SOURCE_DIR=../saas-bench \
      uv run python modal_deploy/launch_bash_agent.py \
        --model gpt-5.5 --provider openai --effort xhigh \
        --days 500 --label gpt55_v3.4aj_modal

Layout:
    Modal App:    bossbench-runs
    Modal Secret: bossbench-keys (all API keys + NMDB_KEY + MODAL_TOKEN_*)
    Modal Volume: bossbench-modal-runs (mounted at /data; persists across sandboxes)

Resources per sandbox: cpu=8, memory=16 GB, no GPU, timeout=24h, idle_timeout=24h.

The sandbox starts (a) optionally a push_data sidecar, and (b) the bash_agent
run itself. Both run as `nohup setsid` background processes so they survive
`sb.exec` returning.

Multi-launch caveats — when you have many sandboxes mounting the same volume:
  * `--skip-deps`: skip rsync + `uv sync`. Use for follow-on launches once an
    earlier launch has populated /data/saas-bench/ + /data/saas-bench/.venv/.
    Avoids 7 sandboxes racing on the same source tree / venv.
  * `--no-push-data`: don't start a push_data sidecar. Only one push_data
    should exist per volume (it picks the most-recent run dir; multiple
    instances flap on the dashboard URL). Use this for follow-on launches
    when an earlier launch already runs push_data.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

import modal

APP_NAME = "bossbench-runs"
VOLUME_NAME = "bossbench-modal-runs"
SECRET_NAME = "bossbench-keys"

OPS_REPO_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_SOURCE_DIR = OPS_REPO_DIR.parent / "saas-bench"
PROJECT_DIR = Path(
    os.environ.get("SAASBENCH_SOURCE_DIR", str(_DEFAULT_SOURCE_DIR))
).expanduser().resolve()

image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install(
        "git", "curl", "build-essential", "rsync", "sqlite3", "ca-certificates",
        # CRITICAL: bubblewrap is required to sandbox the agent's bash from the
        # private engine source under /data/saas-bench/src/. Without bwrap,
        # tools.py:_exec_bash silently falls back to plain `bash -c` and the
        # agent can read config.py/simulation.py.
        "bubblewrap",
    )
    .run_commands(
        # Install uv globally so non-login shells find it
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "cp /root/.local/bin/uv /usr/local/bin/uv",
        # Install Modal CLI (push_data shells out to it)
        "pip install --no-cache-dir modal==1.3.3",
    )
    .add_local_dir(
        str(PROJECT_DIR),
        remote_path="/app/saas-bench",
        ignore=[
            "bash_agent_runs/**",
            "bash_agent_ablation_runs/**",
            "codex_agent_runs/**",
            "baseline_runs/**",
            "oracle_runs/**",
            "replay_runs/**",
            "backups/**",
            "logs/**",
            "*.log",
            ".venv/**",
            "__pycache__/**",
            "**/__pycache__/**",
            "*.pyc",
            ".git",
            ".git/**",
            "**/.git",
            "**/.git/**",        # nested git repos (paper/overleaf/.git, public/.git — 4GB)
            "public/.git",
            "public/.git/**",
            "public/sessions/**",  # legacy run state we don't need
            "paper",
            "paper/**",          # latex/overleaf — not needed at runtime, mutates during build
            "tmp",
            "tmp/**",
            "**/tmp/**",
            ".pytest_cache",
            ".pytest_cache/**",
            "**/.pytest_cache/**",
            "public_sources/**", # mirror of src/ — already shipped via src/, would double the image
            "modal_deploy/launch.py",  # not needed at runtime
            "modal_deploy/state/**",
            ".minion-claude-sessions.json",
        ],
    )
)


_DEPS_FULL = r"""
# (1) Sync the latest code into the Volume (preserves bash_agent_runs/).
mkdir -p /data/saas-bench
if [ -d /data/saas-bench/.venv ]; then
    echo "[bootstrap] removing stale .venv from prior launch"
    chmod -R u+w /data/saas-bench/.venv 2>/dev/null || true
    rm -rf /data/saas-bench/.venv 2>/dev/null || true
fi
rsync -a --delete \
  --exclude='bash_agent_runs/' \
  --exclude='bash_agent_ablation_runs/' \
  --exclude='codex_agent_runs/' \
  --exclude='baseline_runs/' \
  --exclude='logs/' \
  --exclude='.venv/' \
  /app/saas-bench/ /data/saas-bench/

cd /data/saas-bench
mkdir -p logs bash_agent_runs bash_agent_ablation_runs

# (2) Modal CLI auth (push_data shells out to `modal volume put`)
mkdir -p /root
cat > /root/.modal.toml <<EOF
[default]
token_id = "${MODAL_TOKEN_ID}"
token_secret = "${MODAL_TOKEN_SECRET}"
active = true
EOF
chmod 600 /root/.modal.toml

# (3) Sync deps (uv reads pyproject.toml)
export PATH="/usr/local/bin:/root/.local/bin:$PATH"
echo "[bootstrap] uv sync starting..."
uv sync 2>&1 | tail -20
"""

_DEPS_SKIP = r"""
echo "[bootstrap] --skip-deps: assuming /data/saas-bench/ + .venv already populated"
mkdir -p /data/saas-bench
cd /data/saas-bench
mkdir -p logs bash_agent_runs bash_agent_ablation_runs
if [ ! -d .venv ]; then
    echo "[bootstrap] FATAL: --skip-deps was set but /data/saas-bench/.venv is missing."
    exit 13
fi
if [ ! -d src/saas_bench ]; then
    echo "[bootstrap] FATAL: --skip-deps was set but /data/saas-bench/src/saas_bench is missing."
    exit 14
fi
mkdir -p /root
cat > /root/.modal.toml <<EOF
[default]
token_id = "${MODAL_TOKEN_ID}"
token_secret = "${MODAL_TOKEN_SECRET}"
active = true
EOF
chmod 600 /root/.modal.toml
export PATH="/usr/local/bin:/root/.local/bin:$PATH"
"""

_ISOLATION_PROBE = r"""
# (3.5) ISOLATION PRE-FLIGHT — refuse to launch the agent unless bwrap works
# AND can hide /data/saas-bench/src from a sandboxed bash.
echo "[bootstrap] isolation pre-flight"
if ! command -v bwrap >/dev/null 2>&1; then
    echo "[bootstrap] FATAL: bwrap not installed — agent would have unrestricted FS access."
    exit 11
fi
PROBE_WS=/tmp/_isolation_probe_$$
mkdir -p $PROBE_WS
PROBE_OUT=$(bwrap \
    --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib \
    --ro-bind /lib64 /lib64 --ro-bind /etc /etc --ro-bind /sbin /sbin \
    --proc /proc --dev /dev --tmpfs /tmp \
    --bind $PROBE_WS $PROBE_WS --chdir $PROBE_WS \
    --unshare-all --share-net \
    bash -c '
        if [ -e /data/saas-bench/src/saas_bench/config.py ]; then
            echo "LEAK: config.py visible"; exit 21
        fi
        if [ -e /data/saas-bench/src/saas_bench/simulation.py ]; then
            echo "LEAK: simulation.py visible"; exit 22
        fi
        if ls /data 2>/dev/null | grep -q saas-bench; then
            echo "LEAK: /data/saas-bench listable"; exit 23
        fi
        echo "ISOLATION_OK"
    ' 2>&1) || true
rm -rf $PROBE_WS
echo "[bootstrap] isolation probe: $PROBE_OUT"
if ! echo "$PROBE_OUT" | grep -q ISOLATION_OK; then
    echo "[bootstrap] FATAL: isolation probe failed — refusing to launch agent."
    exit 12
fi
echo "[bootstrap] bwrap isolation OK — engine source not visible to sandboxed bash."
"""

def _push_data_on_block(
    modal_volume: str,
    label_prefixes: str,
    fresh_minutes: str,
    run_includes: str = "",
) -> str:
    label_filter = (
        f"export BOSSBENCH_PUSH_LABEL_PREFIXES={shlex.quote(label_prefixes)}\n"
        if label_prefixes else
        "unset BOSSBENCH_PUSH_LABEL_PREFIXES\n"
    )
    run_filter = (
        f"export BOSSBENCH_PUSH_RUN_INCLUDES={shlex.quote(run_includes)}\n"
        if run_includes else
        "unset BOSSBENCH_PUSH_RUN_INCLUDES\n"
    )
    return f"""
# (4) Start push_data sidecar (continuous mode)
echo "[bootstrap] starting push_data sidecar"
export BOSSBENCH_PUSH_MODAL_VOLUME={shlex.quote(modal_volume)}
export BOSSBENCH_PUSH_FRESH_MINUTES={shlex.quote(fresh_minutes)}
{label_filter}{run_filter}echo "[bootstrap] push_data target volume=$BOSSBENCH_PUSH_MODAL_VOLUME label_prefixes=${{BOSSBENCH_PUSH_LABEL_PREFIXES:-<none>}} run_includes=${{BOSSBENCH_PUSH_RUN_INCLUDES:-<none>}} fresh_minutes=$BOSSBENCH_PUSH_FRESH_MINUTES"
nohup uv run python monitor/push_data.py --loop 60 \
    > logs/push_data.log 2>&1 &
PUSH_PID=$!
echo "[bootstrap] push_data PID=$PUSH_PID"

sleep 3
if kill -0 $PUSH_PID 2>/dev/null; then
    echo "[bootstrap] push_data alive"
else
    echo "[bootstrap] WARNING: push_data died already"
    tail -30 logs/push_data.log
fi
"""

_PUSH_DATA_OFF = r"""
echo "[bootstrap] --no-push-data: skipping push_data sidecar"
"""


# Oracle public-dir builder — runs INSIDE the agent's sandbox so its volume
# writes are immediately visible to the same mount (no cross-sandbox snapshot
# delay). Mirrors /data/saas-bench/public/ → /data/saas-bench/public_oracle/,
# then splices the patched api_server.pyc (compiled from the in-tree source
# under /data/saas-bench/src/saas_bench/api_server.py — which contains the
# `_ORACLE_MODE` bypass) into the zipapp.
_ORACLE_BUILD = r"""
echo "[bootstrap] oracle-build: ensuring /data/saas-bench/public_oracle/ is up to date"
SRC_DIR=/data/saas-bench/public
DST_DIR=/data/saas-bench/public_oracle
PATCHED_PY=/data/saas-bench/src/saas_bench/api_server.py

if [ ! -f "$PATCHED_PY" ]; then
    echo "[bootstrap] FATAL: oracle build needs $PATCHED_PY"; exit 30
fi
if [ ! -d "$SRC_DIR" ]; then
    echo "[bootstrap] FATAL: oracle build needs $SRC_DIR"; exit 31
fi
if ! grep -q "_ORACLE_MODE" "$PATCHED_PY"; then
    echo "[bootstrap] FATAL: $PATCHED_PY is missing _ORACLE_MODE marker"; exit 32
fi

rm -rf "$DST_DIR"
mkdir -p "$DST_DIR"
for f in "$SRC_DIR"/*; do
    bn=$(basename "$f")
    if [ "$bn" = "novamind-operation" ]; then continue; fi
    cp -a "$f" "$DST_DIR/$bn"
done
for f in "$SRC_DIR"/.[!.]*; do
    [ -e "$f" ] || continue
    cp -a "$f" "$DST_DIR/$(basename "$f")"
done

WORK=$(mktemp -d)
cp "$PATCHED_PY" "$WORK/api_server.py"
python3.13 - <<PYEOF
import py_compile
py_compile.compile("$WORK/api_server.py", cfile="$WORK/api_server.pyc", doraise=True)
import os
print("[bootstrap] oracle: compiled api_server.pyc size=" + str(os.path.getsize("$WORK/api_server.pyc")))
PYEOF

python3.13 - <<PYEOF
import os, shutil, zipfile
src_zipapp = "$SRC_DIR/novamind-operation"
dst_zipapp = "$DST_DIR/novamind-operation"
patched_pyc = "$WORK/api_server.pyc"

shutil.copy(src_zipapp, dst_zipapp)
os.chmod(dst_zipapp, 0o755)

with open(dst_zipapp, "rb") as f:
    head = f.read(2048)
nl = head.find(b"\n")
shebang = head[:nl+1] if head.startswith(b"#!") and nl != -1 else b""

with open(dst_zipapp, "rb") as f:
    f.seek(len(shebang))
    body = f.read()

tmp_zip = "$WORK/zipapp_body.zip"
with open(tmp_zip, "wb") as f:
    f.write(body)

new_zip = "$WORK/zipapp_new.zip"
with zipfile.ZipFile(tmp_zip, "r") as zin:
    with zipfile.ZipFile(new_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        replaced = False
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "saas_bench/api_server.pyc":
                with open(patched_pyc, "rb") as pf:
                    data = pf.read()
                replaced = True
                print(f"[bootstrap] oracle: spliced api_server.pyc ({len(data)} bytes)")
            zout.writestr(item, data)
        if not replaced:
            raise SystemExit("FATAL: saas_bench/api_server.pyc not found inside zipapp")

with open(dst_zipapp, "wb") as f:
    f.write(shebang)
    with open(new_zip, "rb") as g:
        f.write(g.read())
os.chmod(dst_zipapp, 0o755)
print(f"[bootstrap] oracle: wrote {dst_zipapp}")
PYEOF

rm -rf "$WORK"
ls -la "$DST_DIR" | head -10
echo "[bootstrap] oracle-build done"
"""


def _bootstrap_script(model: str, provider: str, effort: str, days: int, seed: int,
                      label: str, resume_run_id: str | None,
                      skip_deps: bool = False, push_data: bool = True,
                      push_modal_volume: str = "bossbench-monitor-data",
                      push_label_prefixes: str = "",
                      push_fresh_minutes: str = "30",
                      push_run_includes: str = "",
                      agent_module: str = "saas_bench.agents.bash_agent.run_test",
                      agent_workspace: str = "bash_agent_runs",
                      novamind_public_dir: str | None = None,
                      oracle_mode: bool = False) -> str:
    """Generate the inline bash bootstrap that runs inside the sandbox."""
    if resume_run_id:
        # Must pass --days even on resume: run_test.py's argparse default for
        # --days is 3650, and the agent overwrites config.json on startup with
        # total_days=args.days. Without this, resumed runs silently extend to 3650.
        run_args = (
            f"--continue-from /data/saas-bench/{shlex.quote(agent_workspace)}/run_{shlex.quote(resume_run_id)} "
            f"--days {days}"
        )
    else:
        run_args = (
            f"--label {shlex.quote(label)} "
            f"--seed {seed} "
            f"--days {days}"
        )

    deps_block = _DEPS_SKIP if skip_deps else _DEPS_FULL
    push_data_block = (
        _push_data_on_block(
            push_modal_volume,
            push_label_prefixes,
            push_fresh_minutes,
            push_run_includes,
        )
        if push_data else
        _PUSH_DATA_OFF
    )

    # Oracle mode auto-builds /data/saas-bench/public_oracle/ inside the same
    # sandbox (so the new dir is immediately visible to its own /data mount —
    # cross-sandbox volume snapshots can lag). When oracle_mode is set and the
    # user didn't pass an explicit --novamind-public-dir, default to the built
    # oracle dir.
    if oracle_mode:
        oracle_build_block = _ORACLE_BUILD
        if not novamind_public_dir:
            novamind_public_dir = "/data/saas-bench/public_oracle"
    else:
        oracle_build_block = ""

    # `omit` means: do not pass --reasoning-effort at all (some Together models
    # reject any reasoning_effort field; agent.py already handles thinking via
    # extra_body for those models).
    if effort == "omit":
        effort_line = ""
        effort_log = "(omitted)"
    else:
        effort_line = f"    --reasoning-effort {shlex.quote(effort)} \\\n"
        effort_log = effort

    if novamind_public_dir:
        public_export = (
            f'export NOVAMIND_PUBLIC_DIR={shlex.quote(novamind_public_dir)}\n'
            f'echo "[bootstrap] NOVAMIND_PUBLIC_DIR={shlex.quote(novamind_public_dir)}"\n'
            f'if [ ! -d "{novamind_public_dir}" ]; then\n'
            f'    echo "[bootstrap] FATAL: NOVAMIND_PUBLIC_DIR={novamind_public_dir} does not exist on volume."\n'
            f'    exit 15\n'
            f'fi\n'
        )
    else:
        public_export = ""

    if oracle_mode:
        oracle_export = (
            'export ORACLE_MODE=1\n'
            'export ORACLE_SOURCE_DIR=/data/saas-bench/src\n'
            'echo "[bootstrap] ORACLE_MODE=1 (api_server hide-filter bypassed; bwrap binds $ORACLE_SOURCE_DIR; sitecustomize import-blocker off)"\n'
            'if [ ! -f "$ORACLE_SOURCE_DIR/saas_bench/config.py" ]; then\n'
            '    echo "[bootstrap] FATAL: ORACLE_SOURCE_DIR=$ORACLE_SOURCE_DIR missing saas_bench/config.py — agent will not be able to read source."\n'
            '    exit 16\n'
            'fi\n'
            'echo "[bootstrap] oracle source files (head -5):"\n'
            'ls -1 "$ORACLE_SOURCE_DIR/saas_bench/" | head -5 | sed "s/^/[bootstrap]   /"\n'
        )
    else:
        oracle_export = ""

    agent_block = f"""
# (5) Start the bash_agent run (python -u for unbuffered stdout)
LOG=logs/agent_{shlex.quote(label)}.log
mkdir -p {shlex.quote(agent_workspace)}
{public_export}{oracle_export}echo "[bootstrap] starting agent: model={shlex.quote(model)} provider={shlex.quote(provider)} effort={shlex.quote(effort_log)}"
nohup setsid uv run python -u -m {shlex.quote(agent_module)} \\
    --model {shlex.quote(model)} \\
    --provider {shlex.quote(provider)} \\
{effort_line}    --workspace {shlex.quote(agent_workspace)} \\
    {run_args} \\
    > $LOG 2>&1 &
AGENT_PID=$!
echo "[bootstrap] agent PID=$AGENT_PID, log=$LOG"

# Disable strict mode — we don't want post-launch verification to kill the script.
set +eo pipefail

# (6) Wait for THIS run's dir to appear by matching $LOG content
RUN_DIR=""
for i in $(seq 1 30); do
    # Pull run id from agent log; first-line printed by run_test.py
    RID=$(grep -oE 'run_[0-9a-f]{{8}}' "$LOG" 2>/dev/null | head -1)
    if [ -n "$RID" ] && [ -f "{shlex.quote(agent_workspace)}/$RID/config.json" ]; then
        RUN_DIR="{shlex.quote(agent_workspace)}/$RID/"
        break
    fi
    sleep 2
done
echo "[bootstrap] run_dir=$RUN_DIR"

# (7) Tail first 30 log lines so we can see early errors
sleep 5
echo "----- agent log (first 30 lines) -----"
head -30 "$LOG" 2>/dev/null || echo "(empty)"

echo "[bootstrap] DONE — agent backgrounded."
exit 0
"""

    return ("set -euo pipefail\n" + deps_block + oracle_build_block
            + _ISOLATION_PROBE + push_data_block + agent_block)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="e.g. gpt-5.5")
    ap.add_argument("--provider", required=True, help="e.g. openai")
    ap.add_argument("--effort", default="xhigh")
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--label", required=True, help="e.g. gpt55_v3.4aj_modal")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cpu", type=float, default=8.0)
    ap.add_argument("--memory", type=int, default=16384, help="MiB")
    ap.add_argument("--timeout", type=int, default=86400)
    ap.add_argument("--idle-timeout", type=int, default=86400)
    ap.add_argument("--resume", default=None,
                    help="Existing run_id (under /data/saas-bench/bash_agent_runs) to continue from.")
    ap.add_argument("--skip-deps", action="store_true",
                    help="Skip rsync + uv sync (use for follow-on launches once volume is populated).")
    ap.add_argument("--no-push-data", action="store_true",
                    help="Don't start a push_data sidecar (only one per volume).")
    ap.add_argument("--push-modal-volume", default="bossbench-monitor-data",
                    help="Modal monitor volume for the push_data sidecar.")
    ap.add_argument("--push-label-prefixes", default="",
                    help="Comma-separated label prefixes for the push_data sidecar.")
    ap.add_argument("--push-fresh-minutes", default="30",
                    help="Freshness window for the push_data sidecar; 0 disables age filtering.")
    ap.add_argument("--push-run-includes", default="",
                    help="Comma-separated exact run IDs for the push_data sidecar. "
                         "When set, push_data bypasses label/freshness filters and "
                         "publishes only these runs.")
    ap.add_argument("--agent-module", default="saas_bench.agents.bash_agent.run_test",
                    help="Python module for the agent runner, e.g. "
                         "saas_bench.agents.bash_agent_ablation.run_test.")
    ap.add_argument("--agent-workspace", default="bash_agent_runs",
                    help="Run directory parent for the agent, e.g. bash_agent_ablation_runs.")
    ap.add_argument("--novamind-public-dir", default=None,
                    help="Path on the volume to a custom public bundle "
                         "(sets NOVAMIND_PUBLIC_DIR for the agent process). "
                         "Use this to pin a specific public commit per run "
                         "without disturbing /data/saas-bench/public.")
    ap.add_argument("--oracle-mode", action="store_true",
                    help="Set ORACLE_MODE=1 so api_server bypasses _HIDDEN_TABLES / "
                         "_HIDDEN_COLUMNS / schema-introspection blocks. ONLY for "
                         "oracle benchmark runs — normal runs must omit this flag.")
    args = ap.parse_args()

    if not (PROJECT_DIR / "pyproject.toml").exists() or not (PROJECT_DIR / "src" / "saas_bench").exists():
        sys.stderr.write(
            "ERROR: SAASBENCH_SOURCE_DIR must point to a SaaSBench checkout "
            f"(current: {PROJECT_DIR})\n"
        )
        sys.exit(2)

    print(f"→ Looking up Modal app/volume/secret...")
    app_ref = modal.App.lookup(APP_NAME, create_if_missing=True)
    vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
    secret = modal.Secret.from_name(SECRET_NAME)

    print(f"→ Creating sandbox: cpu={args.cpu}, mem={args.memory} MiB, "
          f"timeout={args.timeout}s, idle_timeout={args.idle_timeout}s")
    sb = modal.Sandbox.create(
        image=image,
        app=app_ref,
        volumes={"/data": vol},
        secrets=[secret],
        cpu=args.cpu,
        memory=args.memory,
        timeout=args.timeout,
        idle_timeout=args.idle_timeout,
    )
    print(f"✅ Sandbox created: {sb.object_id}")

    bootstrap = _bootstrap_script(
        model=args.model, provider=args.provider, effort=args.effort,
        days=args.days, seed=args.seed, label=args.label,
        resume_run_id=args.resume,
        skip_deps=args.skip_deps,
        push_data=not args.no_push_data,
        push_modal_volume=args.push_modal_volume,
        push_label_prefixes=args.push_label_prefixes,
        push_fresh_minutes=args.push_fresh_minutes,
        push_run_includes=args.push_run_includes,
        agent_module=args.agent_module,
        agent_workspace=args.agent_workspace,
        novamind_public_dir=args.novamind_public_dir,
        oracle_mode=args.oracle_mode,
    )

    print("→ Running bootstrap inside sandbox...")
    proc = sb.exec("bash", "-lc", bootstrap)
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    err = proc.stderr.read()
    if err:
        sys.stderr.write("STDERR:\n" + err + "\n")
    rc = proc.wait()
    if rc != 0:
        sys.stderr.write(f"\n⚠️  bootstrap exited with code {rc}\n")
        sys.exit(rc)

    print(f"\n✅ Bootstrap complete.")
    print(f"   Sandbox ID: {sb.object_id}")
    print(f"   Reconnect:  modal sandbox logs {sb.object_id}")
    print(f"   Shell:      modal sandbox shell {sb.object_id}")
    print(f"   Stop:       modal sandbox terminate {sb.object_id}")

    # Persist sandbox ID locally so the user can reconnect/resume later.
    state_dir = OPS_REPO_DIR / "modal_deploy" / "state"
    state_dir.mkdir(exist_ok=True, parents=True)
    state_file = state_dir / f"{args.label}.sandbox_id"
    state_file.write_text(sb.object_id + "\n")
    print(f"   Wrote: {state_file}")


if __name__ == "__main__":
    main()
