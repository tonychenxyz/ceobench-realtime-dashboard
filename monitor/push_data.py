"""Push run data to Modal volume for the monitoring dashboard.

Runs locally on the cluster. Dumps all run stats + recent actions to JSON,
then uploads to a Modal volume that the dashboard app reads from.

Usage:
    # One-shot push
    python push_data.py

    # Continuous push every N seconds
    python push_data.py --loop 30
"""

import atexit
import json
import os
import sqlite3
import sys
import tempfile
import time
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from datetime import datetime

# Add SaaSBench source root to path so we can import db_protection.
_OPS_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PROJECT_ROOT = _OPS_REPO_ROOT.parent / "saas-bench"
_PROJECT_ROOT = Path(
    os.environ.get("SAASBENCH_SOURCE_DIR", str(_DEFAULT_PROJECT_ROOT))
).expanduser().resolve()
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

RUNS_DIR = _PROJECT_ROOT / "bash_agent_runs"
RUNS_DIRS = [
    _PROJECT_ROOT / "bash_agent_runs",
    _PROJECT_ROOT / "codex_agent_runs",
    _PROJECT_ROOT / "claude_code_runs",
    _PROJECT_ROOT / "bash_agent_ablation_runs",
]
RUN_PARENT: dict[str, Path] = {}
OUTPUT_FILE = Path(os.environ.get("BOSSBENCH_PUSH_OUTPUT_FILE", str(Path(__file__).parent / "data.json")))
MODAL_VOLUME = os.environ.get("BOSSBENCH_PUSH_MODAL_VOLUME", "bossbench-monitor-data")

_PLAIN_TMP_DIR = Path(os.environ.get(
    "BOSSBENCH_PUSH_DATA_TMP_DIR",
    str(_OPS_REPO_ROOT / ".tmp" / f"push_data_plain_{os.getuid()}"),
))


def _ensure_plain_tmp_dir():
    _PLAIN_TMP_DIR.mkdir(parents=True, exist_ok=True)


def _sweep_stale_plain_tmp():
    if not _PLAIN_TMP_DIR.exists():
        return
    for f in _PLAIN_TMP_DIR.glob("push_data_*.plain.tmp"):
        try:
            f.unlink()
        except Exception:
            pass


def _sweep_orphan_plain_tmp_for_active_runs(active_rids: set[str]):
    """Parent-process orphan reaper. Keep newest tmp per active rid, delete the rest.

    ProcessPoolExecutor.map doesn't pin tasks to workers — when worker A handles
    rid X in cycle N and worker B handles X in cycle N+1, A's cached tmp is
    orphaned (A may not receive any task in N+1, so its per-worker prune never
    fires). This sweep runs in the parent and trims to one tmp file per rid
    (the newest, which is the one the current cycle's worker just created).
    """
    if not _PLAIN_TMP_DIR.exists():
        return
    by_rid: dict[str, list] = {}
    for f in _PLAIN_TMP_DIR.glob("push_data_*.plain.tmp"):
        name = f.name[len("push_data_"):-len(".plain.tmp")]
        rid = name.rsplit("_", 1)[0]
        by_rid.setdefault(rid, []).append(f)
    for rid, files in by_rid.items():
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        keep = files[:1] if rid in active_rids else []
        for f in files:
            if f in keep:
                continue
            try:
                f.unlink()
            except Exception:
                pass


class _CachedConn:
    """Wrapper around a cached sqlite3 conn whose close() is a no-op.

    Lets existing callers keep calling .close() without evicting the cache.
    """
    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


# Cache value: (mtime, size, conn, plain_tmp_path)
_DB_CACHE: dict[str, tuple[float, int, sqlite3.Connection, str]] = {}


def _close_cached_entry(entry):
    """Close a cached conn and unlink its plain-decrypt tmp file."""
    _, _, conn, tmp_path = entry
    try:
        conn.close()
    except Exception:
        pass
    if tmp_path and os.path.exists(tmp_path):
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _cleanup_all_cached():
    for entry in list(_DB_CACHE.values()):
        _close_cached_entry(entry)
    _DB_CACHE.clear()


atexit.register(_cleanup_all_cached)
_sweep_stale_plain_tmp()


def _open_run_db(run_dir: Path):
    """Open the run's obfuscated .nmdb database as a plain SQLite conn.

    Bulk-decrypts the .nmdb to a plain SQLite tmp file under
    `_PLAIN_TMP_DIR` (on /scratch, not /tmp — these files can be 1-3 GB).
    The resulting plain file is queried directly with stdlib sqlite3 so
    follow-up queries pay zero per-page AES cost.

    Cache key is (nmdb_path, mtime, size). When the .nmdb is rewritten by
    the simulation server (atomic replace → new mtime/size), the old
    cached conn is closed and its plain tmp file is unlinked before a
    fresh decrypt runs.
    """
    candidates = [c for c in run_dir.rglob("world.nmdb") if c.stat().st_size > 0]
    if not candidates:
        return None
    nmdb_path = max(candidates, key=lambda c: c.stat().st_mtime)
    key = str(nmdb_path)
    st = nmdb_path.stat()
    cached = _DB_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return _CachedConn(cached[2])

    plain_path = None
    try:
        from saas_bench.db_protection import _export_encrypted_to_plain, _get_key
        _ensure_plain_tmp_dir()
        rid_hint = run_dir.name.replace("run_", "") or "run"
        fd, plain_path = tempfile.mkstemp(
            prefix=f"push_data_{rid_hint}_",
            suffix=".plain.tmp",
            dir=str(_PLAIN_TMP_DIR),
        )
        os.close(fd)
        os.unlink(plain_path)  # _export_encrypted_to_plain creates the file fresh
        _export_encrypted_to_plain(str(nmdb_path), plain_path, _get_key())
        conn = sqlite3.connect(plain_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA cache_size=-200000")
    except Exception:
        if plain_path and os.path.exists(plain_path):
            try:
                os.unlink(plain_path)
            except Exception:
                pass
        return None

    if cached is not None:
        _close_cached_entry(cached)

    _DB_CACHE[key] = (st.st_mtime, st.st_size, conn, plain_path)
    return _CachedConn(conn)


def _derive_current_day_from_db(conn) -> int | None:
    """Best-effort current simulation day from tables that advance with the world."""
    queries = (
        "SELECT MAX(day) FROM _hidden_group_params_history",
        "SELECT MAX(day) FROM ledger",
        "SELECT MAX(day) FROM service_day",
        "SELECT MAX(day) FROM social_media_posts",
        "SELECT MAX(day) FROM agent_posts",
    )
    for sql in queries:
        try:
            row = conn.execute(sql).fetchone()
            if row and row[0] is not None:
                return int(row[0])
        except Exception:
            continue
    return None


# Run registry
RUN_REGISTRY = {
    "af67e8ef": {"label": "GPT-5.4 xhigh v3.3s", "model": "gpt-5.4", "seed": 42, "days": 500},
}


def _read_run_label(run_dir: Path) -> str | None:
    """Read `label` from a run's config.json (handles --workspace nested layout)."""
    candidates = [run_dir / "config.json"]
    candidates += list(run_dir.glob("run_*/config.json"))
    for cfg in candidates:
        try:
            with open(cfg) as f:
                return (json.load(f).get("label") or None)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return None


def get_run_ids():
    RUN_PARENT.clear()
    fresh_min = float(os.environ.get("BOSSBENCH_PUSH_FRESH_MINUTES", "30"))
    fresh_cutoff = time.time() - fresh_min * 60 if fresh_min > 0 else None
    label_prefixes_raw = os.environ.get("BOSSBENCH_PUSH_LABEL_PREFIXES", "").strip()
    label_prefixes = tuple(p.strip() for p in label_prefixes_raw.split(",") if p.strip()) if label_prefixes_raw else ()
    label_excludes_raw = os.environ.get("BOSSBENCH_PUSH_LABEL_EXCLUDES", "").strip()
    label_excludes = tuple(p.strip() for p in label_excludes_raw.split(",") if p.strip()) if label_excludes_raw else ()
    run_includes_raw = os.environ.get("BOSSBENCH_PUSH_RUN_INCLUDES", "").strip()
    run_includes = frozenset(r.strip() for r in run_includes_raw.split(",") if r.strip()) if run_includes_raw else frozenset()
    for parent in RUNS_DIRS:
        if not parent.exists():
            continue
        for d in sorted(parent.iterdir()):
            if d.is_dir() and d.name.startswith("run_"):
                rid = d.name.replace("run_", "")
                if run_includes and rid not in run_includes:
                    continue
                if fresh_cutoff is not None and not run_includes:
                    # Use the most recently-touched of: tool_results jsonl
                    # (appended every tool call), checkpoint.json (per day), or
                    # world.nmdb (per week). The jsonl is the canonical liveness
                    # signal — world.nmdb only flushes at week boundaries.
                    candidates = [d / "checkpoint.json", d / "world.nmdb"]
                    candidates += list((d / "logs").glob("tool_results_*.jsonl"))
                    latest = 0.0
                    for c in candidates:
                        try:
                            latest = max(latest, c.stat().st_mtime)
                        except FileNotFoundError:
                            pass
                    if latest < fresh_cutoff:
                        continue
                if label_prefixes or label_excludes:
                    if rid in run_includes:
                        pass  # explicit allowlist bypasses label filter
                    else:
                        label = _read_run_label(d) or ""
                        if label_prefixes and not any(label.startswith(p) for p in label_prefixes):
                            continue
                        if label_excludes and any(label.startswith(p) for p in label_excludes):
                            continue
                # First writer wins (bash_agent_runs is listed first).
                RUN_PARENT.setdefault(rid, parent)
    ids = list(RUN_PARENT.keys())
    registry_order = list(RUN_REGISTRY.keys())
    known = [r for r in registry_order if r in ids]
    unknown = [r for r in ids if r not in registry_order]
    return known + unknown


def get_founder_dividends_from_db(run_dir: Path) -> float:
    """Quick SQLite query for cumulative founder dividends. Returns 0 if DB locked."""
    conn = _open_run_db(run_dir)
    if not conn:
        return 0
    try:
        row = conn.execute("SELECT COALESCE(SUM(founder_payout), 0) FROM dividends").fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def get_dividend_series_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Cumulative founder dividends by day. Returns list of {day, dividends}."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT day, founder_payout FROM dividends ORDER BY day"
        ).fetchall()
        conn.close()
        if not rows:
            return []
        # Build cumulative series
        series = []
        cumulative = 0.0
        for day, payout in rows:
            cumulative += payout
            series.append({"day": day, "dividends": round(cumulative, 2)})
        # Downsample if too many points
        if len(series) > max_points:
            step = len(series) // max_points
            series = [s for i, s in enumerate(series) if i % step == 0 or i == len(series) - 1]
        return series
    except Exception:
        return []


def get_profit_series_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Weekly (7-day non-overlapping) profit series. Returns list of {day, revenue, costs, profit}."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        # Get all distinct days from ledger
        max_day_row = conn.execute("SELECT MAX(day) FROM ledger").fetchone()
        if not max_day_row or max_day_row[0] is None:
            conn.close()
            return []
        max_day = max_day_row[0]
        series = []
        # Non-overlapping 7-day windows
        day = 6  # first full 7-day window ends at day 6 (days 0-6)
        while day <= max_day:
            start_day = day - 6
            row = conn.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN amount > 0 AND category != 'initial_funding' THEN amount ELSE 0 END), 0) as revenue,
                    COALESCE(SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END), 0) as costs
                FROM ledger WHERE day BETWEEN ? AND ?
            """, (start_day, day)).fetchone()
            revenue = round(row[0], 2)
            costs = round(row[1], 2)
            series.append({"day": day, "revenue": revenue, "costs": costs, "profit": round(revenue + costs, 2)})
            day += 7
        conn.close()
        if len(series) > max_points:
            step = len(series) // max_points
            series = [s for i, s in enumerate(series) if i % step == 0 or i == len(series) - 1]
        return series
    except Exception:
        return []


def get_ads_revenue_series_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Daily ads revenue per active customer group. Returns list of {day, group_id, revenue}."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT day, group_id, SUM(revenue) as total_revenue "
            "FROM ads_revenue GROUP BY day, group_id ORDER BY day, group_id"
        ).fetchall()
        conn.close()
        if not rows:
            return []
        series = [{"day": r[0], "group_id": r[1], "revenue": round(r[2], 2)} for r in rows]
        unique_days = sorted(set(r[0] for r in rows))
        if len(unique_days) > max_points:
            step = len(unique_days) // max_points
            keep_days = set(d for i, d in enumerate(unique_days) if i % step == 0 or i == len(unique_days) - 1)
            series = [s for s in series if s["day"] in keep_days]
        return series
    except Exception:
        return []


def get_reputation_series_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Daily reputation per group from hidden snapshot table. Returns list of {day, group_id, reputation}."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT day, group_id, reputation FROM _hidden_group_params_history ORDER BY day, group_id"
        ).fetchall()
        conn.close()
        if not rows:
            return []
        series = [{"day": r[0], "group_id": r[1], "reputation": round(r[2], 6)} for r in rows]
        # Downsample if too many points (per-group, so total rows = days × groups)
        unique_days = sorted(set(r[0] for r in rows))
        if len(unique_days) > max_points:
            step = len(unique_days) // max_points
            keep_days = set(d for i, d in enumerate(unique_days) if i % step == 0 or i == len(unique_days) - 1)
            series = [s for s in series if s["day"] in keep_days]
        return series
    except Exception:
        return []


def get_quality_series_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Quality per group × plan over time from _hidden_quality_snapshot."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT day, group_id, plan, delivered_quality FROM _hidden_quality_snapshot ORDER BY day, group_id, plan"
        ).fetchall()
        conn.close()
        if not rows:
            return []
        series = [{"day": r[0], "group_id": r[1], "plan": r[2], "quality": round(r[3], 4)} for r in rows]
        unique_days = sorted(set(r[0] for r in rows))
        if len(unique_days) > max_points:
            step = len(unique_days) // max_points
            keep_days = set(d for i, d in enumerate(unique_days) if i % step == 0 or i == len(unique_days) - 1)
            series = [s for s in series if s["day"] in keep_days]
        return series
    except Exception:
        return []


def get_qmin_drift_only_series_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Per-group q_bias drift accumulator (no global, no base) over time.

    This isolates the GROUP-specific drift contribution — both the daily
    `GROUP_PREFERENCE_DRIFT.q_bias_drift` and the per-group competitor-event
    shocks (`COMPETITOR_REACTIVITY_Q_BIAS[g] × boost`). Useful for visualizing
    how different groups react to the same market shocks (otherwise dominated
    by the much larger global accumulator at chart y-scale).

    Returns rows like {day, group_id, drift_q_bias} so the dashboard can plot
    one line per group starting from 0 at day 0.
    """
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(_hidden_group_params_history)").fetchall()}
        if 'drift_q_bias_total' not in cols:
            return []  # old schema — drift-only view not available
        rows = conn.execute(
            "SELECT day, group_id, drift_q_bias_total "
            "FROM _hidden_group_params_history ORDER BY day, group_id"
        ).fetchall()
        if not rows:
            return []
        series = [
            {"day": r[0], "group_id": r[1], "drift_q_bias": round(r[2], 5)}
            for r in rows
        ]
        unique_days = sorted(set(s["day"] for s in series))
        if len(unique_days) > max_points:
            step = len(unique_days) // max_points
            keep_days = set(d for i, d in enumerate(unique_days) if i % step == 0 or i == len(unique_days) - 1)
            series = [s for s in series if s["day"] in keep_days]
        return series
    except Exception:
        return []


def get_qmin_series_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Effective q_min per group over time from _hidden_group_params_history.

    Supports both old schema (current_q_min_mean column) and new accumulator
    schema (drift_q_bias_total + global_q_bias_total applied to static base).
    """
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        # Detect schema: check which columns exist
        cols = {r[1] for r in conn.execute("PRAGMA table_info(_hidden_group_params_history)").fetchall()}
        use_accumulators = 'drift_q_bias_total' in cols

        if use_accumulators:
            from saas_bench.config import CUSTOMER_GROUPS
            base_qmin = {gid: g.q_min_mean for gid, g in CUSTOMER_GROUPS.items()}
            rows = conn.execute(
                "SELECT day, group_id, drift_q_bias_total, global_q_bias_total "
                "FROM _hidden_group_params_history ORDER BY day, group_id"
            ).fetchall()
            conn.close()
            if not rows:
                return []
            series = []
            for r in rows:
                day, group_id, drift_q_bias, global_q_bias = r
                base = base_qmin.get(group_id, 0.5)
                effective_qmin = base + global_q_bias + drift_q_bias
                series.append({"day": day, "group_id": group_id, "q_min": round(effective_qmin, 4)})
        else:
            # Old schema: current_q_min_mean is already the effective value
            rows = conn.execute(
                "SELECT day, group_id, current_q_min_mean "
                "FROM _hidden_group_params_history ORDER BY day, group_id"
            ).fetchall()
            conn.close()
            if not rows:
                return []
            series = [{"day": r[0], "group_id": r[1], "q_min": round(r[2], 4)} for r in rows]

        unique_days = sorted(set(s["day"] for s in series))
        if len(unique_days) > max_points:
            step = len(unique_days) // max_points
            keep_days = set(d for i, d in enumerate(unique_days) if i % step == 0 or i == len(unique_days) - 1)
            series = [s for s in series if s["day"] in keep_days]
        return series
    except Exception:
        return []


def get_discovered_group_ids(run_dir: Path) -> set:
    """Return set of discovered group_ids (info_level >= 1)."""
    conn = _open_run_db(run_dir)
    if not conn:
        return set()
    try:
        rows = conn.execute(
            "SELECT group_id FROM group_info_levels WHERE info_level >= 1"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def get_prediction_accuracy_series(run_dir: Path, max_points: int = 300) -> list:
    """Prediction accuracy series for cash predictions.

    Joins the ``predictions`` table with per-day cash (cumulative ledger sum)
    to compute percent error for every prediction whose target day has been
    reached. Returns a flat list of rows keyed by (submit_day, horizon_days):

        {
            "submit_day": int,            # day prediction was made
            "target_day": int,            # submit_day + horizon_days
            "horizon_days": int,          # 7, 28, 84, or 182
            "predicted_value": float,     # dollars (point estimate)
            "predicted_lower": float|None, # 95% CI lower (None for legacy rows)
            "predicted_upper": float|None, # 95% CI upper (None for legacy rows)
            "actual_value": float,        # dollars (cumulative ledger at target_day)
            "pct_diff": float,            # (predicted - actual) / actual * 100
        }

    Rows are sorted by submit_day, then horizon_days. Rows whose target day
    has not yet been reached in the sim are omitted (they cannot be scored).
    """
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        # Does the table exist? (Older runs may not have it.)
        has_pred = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
        ).fetchone()
        if not has_pred:
            conn.close()
            return []

        # Detect which CI columns exist (older runs only have predicted_value).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)").fetchall()}
        has_ci = 'predicted_lower' in cols and 'predicted_upper' in cols
        select_cols = "submit_day, horizon_days, metric, predicted_value"
        if has_ci:
            select_cols += ", predicted_lower, predicted_upper"

        preds = conn.execute(f"""
            SELECT {select_cols}
            FROM predictions
            WHERE metric = 'cash'
            ORDER BY submit_day, horizon_days
        """).fetchall()
        if not preds:
            conn.close()
            return []

        max_day_row = conn.execute("SELECT MAX(day) FROM ledger").fetchone()
        max_day = (max_day_row[0] if max_day_row and max_day_row[0] is not None else 0)

        # Pre-aggregate ledger by day, then build cumulative cash(day) lookup.
        ledger_rows = conn.execute(
            "SELECT day, SUM(amount) FROM ledger GROUP BY day ORDER BY day"
        ).fetchall()
        conn.close()

        cum_by_day = {}
        running = 0.0
        for day, amt in ledger_rows:
            running += (amt or 0.0)
            cum_by_day[day] = running

        # Forward-fill: cash on days with no ledger activity = last known cash.
        cash_on_day = {}
        if ledger_rows:
            min_day = ledger_rows[0][0]
            running = 0.0
            for d in range(min_day, max_day + 1):
                if d in cum_by_day:
                    running = cum_by_day[d]
                cash_on_day[d] = running

        series = []
        for row in preds:
            if has_ci:
                submit_day, horizon_days, _metric, predicted_value, predicted_lower, predicted_upper = row
            else:
                submit_day, horizon_days, _metric, predicted_value = row
                predicted_lower = None
                predicted_upper = None
            target_day = submit_day + horizon_days
            if target_day > max_day:
                continue  # can't score yet
            actual = cash_on_day.get(target_day)
            if actual is None:
                continue
            # Percent diff — guard tiny/zero actuals by using max(|actual|, 1).
            denom = abs(actual) if abs(actual) > 1.0 else 1.0
            pct = (predicted_value - actual) / denom * 100.0
            series.append({
                "submit_day": int(submit_day),
                "target_day": int(target_day),
                "horizon_days": int(horizon_days),
                "predicted_value": round(float(predicted_value), 2),
                "predicted_lower": round(float(predicted_lower), 2) if predicted_lower is not None else None,
                "predicted_upper": round(float(predicted_upper), 2) if predicted_upper is not None else None,
                "actual_value": round(float(actual), 2),
                "pct_diff": round(pct, 3),
            })

        if len(series) > max_points:
            step = len(series) // max_points
            series = [s for i, s in enumerate(series) if i % step == 0 or i == len(series) - 1]
        return series
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return []


def get_seat_series_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Individual subs + enterprise seats per day from _hidden_group_params_history days."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        # Get all days from group_params_history as reference
        days = conn.execute(
            "SELECT DISTINCT day FROM _hidden_group_params_history ORDER BY day"
        ).fetchall()
        if not days:
            conn.close()
            return []
        series = []
        for (day,) in days:
            row = conn.execute(
                """SELECT
                    COALESCE(SUM(CASE WHEN seat_count = 1 THEN 1 ELSE 0 END), 0) as individual,
                    COALESCE(SUM(CASE WHEN seat_count > 1 THEN seat_count ELSE 0 END), 0) as enterprise_seats
                FROM subscriptions
                WHERE status IN ('subscribed', 'cancelled') AND start_day <= ? AND (end_day IS NULL OR end_day > ?)""",
                (day, day)
            ).fetchone()
            series.append({"day": day, "individual": row[0], "enterprise_seats": row[1]})
        conn.close()
        if len(series) > max_points:
            step = len(series) // max_points
            series = [s for i, s in enumerate(series) if i % step == 0 or i == len(series) - 1]
        return series
    except Exception:
        return []


def get_seat_series_by_group_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Per-group seat counts per day. Returns list of {day, group_id, count}."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        days = [d for (d,) in conn.execute(
            "SELECT DISTINCT day FROM _hidden_group_params_history ORDER BY day"
        ).fetchall()]
        if not days:
            conn.close()
            return []
        if len(days) > max_points:
            step = len(days) // max_points
            days = [d for i, d in enumerate(days) if i % step == 0 or i == len(days) - 1]
        series = []
        for day in days:
            rows = conn.execute(
                """SELECT c.group_id, COALESCE(SUM(s.seat_count), 0) AS seats
                   FROM subscriptions s
                   LEFT JOIN customers c ON s.customer_id = c.customer_id
                   WHERE s.status IN ('subscribed', 'cancelled')
                     AND s.start_day <= ? AND (s.end_day IS NULL OR s.end_day > ?)
                   GROUP BY c.group_id""",
                (day, day),
            ).fetchall()
            for gid, count in rows:
                if gid is None:
                    continue
                series.append({"day": day, "group_id": gid, "count": int(count)})
        conn.close()
        return series
    except Exception:
        return []


def get_weekly_churn_by_group_series_from_db(run_dir: Path, max_points: int = 200) -> list:
    """Trailing 7-day churn rate per customer group over time.

    For each (group, day d), with week window = [d-6, d]:
        cancelled = subs ending in window (status='cancelled')
        active_at_start = subs active at day (d-7) — i.e. denominator
        rate = cancelled / max(active_at_start, 1)

    Returns list of {day, group_id, churn_rate, cancelled, voluntary, involuntary, active_at_start}.
    Filters to days >= 7 (need a full week). Skips group-days with 0 active and 0 churn.
    """
    from bisect import bisect_right
    from collections import defaultdict

    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        subs = conn.execute(
            "SELECT c.group_id, s.start_day, s.end_day, s.status, s.churn_reason "
            "FROM subscriptions s JOIN customers c ON c.customer_id = s.customer_id"
        ).fetchall()
        max_row = conn.execute("SELECT MAX(day) FROM _hidden_group_params_history").fetchone()
        conn.close()

        max_day = (max_row[0] if max_row else None) or 0
        if max_day < 7 or not subs:
            return []

        groups = sorted({s[0] for s in subs if s[0] is not None})

        # (group, end_day) → cancellation counts (total + involuntary subset)
        cancel_by_gd: dict = defaultdict(int)
        invol_by_gd: dict = defaultdict(int)
        # Per-group sorted lists of starts and effective ends (NULL → +inf) for active_at(d).
        starts_by_g: dict = defaultdict(list)
        ends_by_g: dict = defaultdict(list)
        for g, sd, ed, status, reason in subs:
            if g is None:
                continue
            starts_by_g[g].append(sd)
            ends_by_g[g].append(ed if ed is not None else 10**9)
            if status == "cancelled" and ed is not None:
                cancel_by_gd[(g, ed)] += 1
                if reason == "involuntary":
                    invol_by_gd[(g, ed)] += 1
        for g in groups:
            starts_by_g[g].sort()
            ends_by_g[g].sort()

        def active_at(g: str, d: int) -> int:
            # active at end of day d = #starts <= d − #ends <= d
            return bisect_right(starts_by_g[g], d) - bisect_right(ends_by_g[g], d)

        days_out = list(range(7, max_day + 1))
        if len(days_out) > max_points:
            step = len(days_out) // max_points
            days_out = [d for i, d in enumerate(days_out) if i % step == 0 or i == len(days_out) - 1]

        series = []
        for d in days_out:
            ws = d - 6  # window: [d-6, d] inclusive
            for g in groups:
                cancelled = sum(cancel_by_gd.get((g, x), 0) for x in range(ws, d + 1))
                involuntary = sum(invol_by_gd.get((g, x), 0) for x in range(ws, d + 1))
                active_start = active_at(g, ws - 1)
                if active_start == 0 and cancelled == 0:
                    continue
                rate = (cancelled / active_start) if active_start > 0 else 0.0
                series.append({
                    "day": d,
                    "group_id": g,
                    "churn_rate": round(rate, 6),
                    "cancelled": cancelled,
                    "voluntary": cancelled - involuntary,
                    "involuntary": involuntary,
                    "active_at_start": active_start,
                })
        return series
    except Exception:
        return []


def get_group_discovery_from_db(run_dir: Path) -> list:
    """Group discovery status from group_info_levels."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT group_id, info_level, is_discoverable, discovered_day FROM group_info_levels ORDER BY group_id"
        ).fetchall()
        conn.close()
        return [{"group_id": r[0], "info_level": r[1], "is_discoverable": r[2], "discovered_day": r[3]} for r in rows]
    except Exception:
        return []


def get_customer_social_posts_from_db(run_dir: Path, limit: int = 50) -> list:
    """Last N customer social media posts."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        # Check if source_group_id column exists (added in v3.2k)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(social_media_posts)").fetchall()}
        has_source_gid = 'source_group_id' in cols
        if has_source_gid:
            group_expr = "COALESCE(p.source_group_id, c.group_id)"
        else:
            group_expr = "c.group_id"
        rows = conn.execute(
            f"""SELECT p.post_id, p.day, p.customer_id,
                      {group_expr} AS group_id,
                      p.sentiment, p.content,
                      p.likes, p.shares, p.reply_to_agent_post_id
               FROM social_media_posts p
               LEFT JOIN customers c ON p.customer_id = c.customer_id
               ORDER BY p.post_id DESC LIMIT ?""", (limit,)
        ).fetchall()
        conn.close()
        def _to_int(v):
            """Convert bytes/numpy types to plain int for JSON serialization."""
            if isinstance(v, bytes):
                return int.from_bytes(v, 'little') if v else 0
            if v is None:
                return 0
            return int(v)
        return [{"post_id": r[0], "day": r[1], "customer_id": r[2], "group_id": r[3],
                 "sentiment": r[4], "content": r[5], "likes": _to_int(r[6]), "shares": _to_int(r[7]),
                 "reply_to_agent_post_id": r[8]} for r in rows]
    except Exception:
        return []


def get_agent_social_posts_from_db(run_dir: Path, limit: int = 50) -> list:
    """Last N agent social media posts with scores, views, and customer replies."""
    conn = _open_run_db(run_dir)
    if not conn:
        return []
    try:
        # Check column availability
        col_names = [c[1] for c in conn.execute("PRAGMA table_info(agent_social_media_posts)").fetchall()]
        has_reasoning = 'reasoning_by_group' in col_names
        smp_cols = {c[1] for c in conn.execute("PRAGMA table_info(social_media_posts)").fetchall()}
        reply_group_expr = "COALESCE(s.source_group_id, c.group_id)" if 'source_group_id' in smp_cols else "c.group_id"
        if has_reasoning:
            posts = conn.execute(
                """SELECT agent_post_id, day, content, reply_to_post_id,
                          effect_by_group, views, views_by_group, reasoning_by_group
                   FROM agent_social_media_posts ORDER BY agent_post_id DESC LIMIT ?""", (limit,)
            ).fetchall()
        else:
            posts = conn.execute(
                """SELECT agent_post_id, day, content, reply_to_post_id,
                          effect_by_group, views, views_by_group
                   FROM agent_social_media_posts ORDER BY agent_post_id DESC LIMIT ?""", (limit,)
            ).fetchall()
        result = []
        for p in posts:
            post_id = p[0]
            effects = {}
            views_by_group = {}
            reasoning = {}
            try:
                effects = json.loads(p[4]) if p[4] else {}
            except Exception:
                pass
            try:
                views_by_group = json.loads(p[6]) if p[6] else {}
            except Exception:
                pass
            if has_reasoning:
                try:
                    reasoning = json.loads(p[7]) if p[7] else {}
                except Exception:
                    pass
            # Get customer replies to this agent post
            replies = conn.execute(
                f"""SELECT s.post_id, s.day, s.customer_id,
                          {reply_group_expr} AS group_id,
                          s.sentiment, s.content
                   FROM social_media_posts s
                   LEFT JOIN customers c ON s.customer_id = c.customer_id
                   WHERE s.reply_to_agent_post_id = ?
                   ORDER BY s.post_id""", (post_id,)
            ).fetchall()
            reply_list = [{"post_id": r[0], "day": r[1], "customer_id": r[2], "group_id": r[3],
                           "sentiment": r[4], "content": r[5]} for r in replies]
            mults = {}  # Per-post multiplier removed; overall next-day multiplier is at run level
            result.append({
                "agent_post_id": post_id, "day": p[1], "content": p[2],
                "reply_to_post_id": p[3], "effect_by_group": effects,
                "views": p[5], "views_by_group": views_by_group,
                "reasoning_by_group": reasoning,
                "replies": reply_list, "multipliers": mults,
            })
        conn.close()
        return result
    except Exception:
        return []


def _brief_args(args):
    """Short preview of tool arguments."""
    if not args:
        return ""
    if isinstance(args, str):
        return args[:80]
    if isinstance(args, dict):
        if "command" in args:
            return str(args["command"])[:80]
        if "path" in args:
            return str(args["path"])[:80]
        if "code" in args:
            return str(args["code"])[:80]
    try:
        s = json.dumps(args)
        return s[:80]
    except Exception:
        return ""


def _resolve_inner_run_dir(run_dir: Path) -> Path:
    """Resolve the actual inner run directory created by --workspace.

    --workspace creates: run_<outer>/run_<inner>/config.json, logs/, agent_workspace/...
    Returns the inner dir if it exists, otherwise the original run_dir.
    """
    # Look for config.json recursively — it's always at the inner run root
    candidates = list(run_dir.glob("run_*/config.json"))
    if candidates:
        return candidates[0].parent
    return run_dir


def get_run_data(run_id: str) -> dict:
    parent = RUN_PARENT.get(run_id, RUNS_DIR)
    run_dir = parent / f"run_{run_id}"
    inner_dir = _resolve_inner_run_dir(run_dir)
    reg = RUN_REGISTRY.get(run_id, {})
    data = {
        "run_id": run_id,
        "label": reg.get("label", f"run_{run_id}"),
        "model": reg.get("model", "unknown"),
        "seed": reg.get("seed"),
        "total_days": reg.get("days"),
    }

    # Last heartbeat: newest file mtime in the run directory
    try:
        newest_mtime = max(
            f.stat().st_mtime
            for f in run_dir.rglob("*")
            if f.is_file()
        )
        data["last_heartbeat"] = datetime.fromtimestamp(
            newest_mtime, tz=__import__('datetime').timezone.utc
        ).isoformat()
    except (ValueError, OSError):
        data["last_heartbeat"] = None

    # Config
    config_path = inner_dir / "config.json"
    data["agent_type"] = None
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
            data["model"] = cfg.get("model", data["model"])
            data["seed"] = cfg.get("seed", data["seed"])
            if data["total_days"] is None:
                data["total_days"] = cfg.get("total_days")
            data["agent_type"] = cfg.get("agent_type")
            # Variant label (e.g. "leads_x1.25"). Falls back to RUN_REGISTRY-derived
            # label, then to "run_<id>". When present, prepended to the displayed
            # label so the dashboard makes the variant identifiable at a glance.
            cfg_label = cfg.get("label")
            if cfg_label:
                data["label"] = f"{cfg_label} ({run_id[:8]})"
                data["variant"] = cfg_label
    # Fallback: infer from which runs-parent dir holds this run_id
    if not data["agent_type"]:
        if parent.name == "codex_agent_runs":
            data["agent_type"] = "codex"
        elif parent.name == "claude_code_runs":
            data["agent_type"] = "claude_code"
        else:
            data["agent_type"] = "bash_agent"

    # Checkpoint
    cp_path = inner_dir / "checkpoint.json"
    if cp_path.exists():
        try:
            with open(cp_path) as f:
                cp = json.load(f)
                data["current_day"] = cp.get("day", cp.get("current_day"))
                data["agent_turns"] = cp.get("agent_total_turns")
                data["total_input_tokens"] = cp.get("total_input_tokens", 0)
                data["total_output_tokens"] = cp.get("total_output_tokens", 0)
                data["total_cached_tokens"] = cp.get("total_cached_tokens", 0)
                data["total_reasoning_tokens"] = cp.get("total_reasoning_tokens", 0)
        except (json.JSONDecodeError, ValueError):
            data["current_day"] = None
            data["agent_turns"] = None
    else:
        data["current_day"] = None
        data["agent_turns"] = None

    # Stats: try JSONL run log first, fall back to DB
    # Search for run_*.jsonl recursively (--workspace nests it deeply)
    run_jsonl = inner_dir / "logs" / f"run_{run_id}.jsonl"
    if not run_jsonl.exists():
        jsonl_candidates = list(run_dir.rglob("run_*.jsonl"))
        if jsonl_candidates:
            run_jsonl = jsonl_candidates[0]
    got_stats_from_jsonl = False
    if run_jsonl.exists():
        try:
            snapshots = []
            with open(run_jsonl) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("category") == "daily_snapshot":
                            d = entry.get("details", {})
                            d["day"] = entry.get("day")
                            snapshots.append(d)
                    except json.JSONDecodeError:
                        continue

            if snapshots:
                got_stats_from_jsonl = True
                latest = snapshots[-1]
                data["cash"] = latest.get("cash", 0)
                data["subscribers"] = latest.get("subscribers", 0)
                data["mrr"] = latest.get("mrr", 0)

                # Derive current_day from latest snapshot if checkpoint missing
                if data.get("current_day") is None and latest.get("day") is not None:
                    data["current_day"] = latest["day"]

                step = max(1, len(snapshots) // 200)
                data["cash_series"] = [
                    {"day": s["day"], "cash": round(s.get("cash", 0), 2)}
                    for i, s in enumerate(snapshots)
                    if i % step == 0 or i == len(snapshots) - 1
                ]
                data["sub_series"] = [
                    {"day": s["day"], "subscribers": s.get("subscribers", 0)}
                    for i, s in enumerate(snapshots)
                    if i % step == 0 or i == len(snapshots) - 1
                ]
        except Exception as e:
            data["db_error"] = str(e)

    # Fallback: get cash/subs/MRR from DB when JSONL not available
    if not got_stats_from_jsonl:
        conn = _open_run_db(run_dir)
        if conn:
            # Each query in its own try/except so one failure doesn't block others
            try:
                row = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger").fetchone()
                data["cash"] = round(row[0], 2) if row else 0
            except Exception as e:
                data.setdefault("db_errors", []).append(f"cash: {e}")

            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE status='subscribed' AND end_day IS NULL"
                ).fetchone()
                data["subscribers"] = row[0] if row else 0
            except Exception as e:
                data.setdefault("db_errors", []).append(f"subscribers: {e}")

            try:
                row = conn.execute("""
                    SELECT COALESCE(SUM(s.effective_price * s.seat_count), 0)
                    FROM subscriptions s
                    WHERE s.status='subscribed' AND s.end_day IS NULL
                """).fetchone()
                data["mrr"] = round(row[0], 2) if row else 0
            except Exception as e:
                data.setdefault("db_errors", []).append(f"mrr: {e}")

            try:
                rows = conn.execute("""
                    SELECT day, SUM(amount) as daily_total
                    FROM ledger GROUP BY day ORDER BY day
                """).fetchall()
                if rows:
                    cash_series = []
                    cumulative = 0.0
                    for day, daily_total in rows:
                        cumulative += daily_total
                        cash_series.append({"day": day, "cash": round(cumulative, 2)})
                    if len(cash_series) > 200:
                        step = len(cash_series) // 200
                        cash_series = [s for i, s in enumerate(cash_series) if i % step == 0 or i == len(cash_series) - 1]
                    data["cash_series"] = cash_series
            except Exception as e:
                data.setdefault("db_errors", []).append(f"cash_series: {e}")

            try:
                hist_days = conn.execute(
                    "SELECT DISTINCT day FROM _hidden_group_params_history ORDER BY day"
                ).fetchall()
                sub_series = []
                for (day,) in hist_days:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM subscriptions WHERE status='subscribed' AND start_day <= ? AND (end_day IS NULL OR end_day > ?)",
                        (day, day)
                    ).fetchone()
                    sub_series.append({"day": day, "subscribers": row[0]})
                if len(sub_series) > 200:
                    step = len(sub_series) // 200
                    sub_series = [s for i, s in enumerate(sub_series) if i % step == 0 or i == len(sub_series) - 1]
                data["sub_series"] = sub_series
            except Exception as e:
                data.setdefault("db_errors", []).append(f"sub_series: {e}")

            # Derive current_day from DB if still missing
            if data.get("current_day") is None:
                current_day = _derive_current_day_from_db(conn)
                if current_day is not None:
                    data["current_day"] = current_day

            conn.close()

    # Always derive current_day from the actual simulation DB (most accurate).
    # checkpoint.json "day" may be in week-units if using step_week(), while
    # total_days and series data are in actual simulation days.
    db_conn = _open_run_db(run_dir)
    if db_conn:
        try:
            current_day = _derive_current_day_from_db(db_conn)
            if current_day is not None:
                data["current_day"] = current_day
        except Exception:
            pass
        try:
            db_conn.close()
        except Exception:
            pass

    if data.get("current_day") is None:
        series_days = [
            point.get("day")
            for series_name in ("cash_series", "sub_series")
            for point in data.get(series_name, [])
            if point.get("day") is not None
        ]
        if series_days:
            data["current_day"] = max(series_days)

    # Founder dividends from SQLite DB (small table, quick query)
    data["founder_dividends"] = get_founder_dividends_from_db(run_dir)
    data["dividend_series"] = get_dividend_series_from_db(run_dir)

    # Cash prediction accuracy per horizon (1wk / 4wk / 12wk)
    data["prediction_accuracy_series"] = get_prediction_accuracy_series(run_dir)

    # Weekly profit series (7-day non-overlapping windows)
    profit_series = get_profit_series_from_db(run_dir)
    data["profit_series"] = profit_series

    # Weekly profit (last 7 days net cash flow from ledger)
    data["weekly_profit"] = None
    db_conn_wp = _open_run_db(run_dir)
    if db_conn_wp:
        try:
            max_day_row = db_conn_wp.execute("SELECT MAX(day) FROM ledger").fetchone()
            if max_day_row and max_day_row[0] is not None:
                max_day = max_day_row[0]
                start_day = max(0, max_day - 6)
                row = db_conn_wp.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM ledger "
                    "WHERE day BETWEEN ? AND ? AND category != 'initial_funding'",
                    (start_day, max_day)
                ).fetchone()
                data["weekly_profit"] = round(row[0], 2) if row else 0.0
        except Exception as e:
            data.setdefault("db_errors", []).append(f"weekly_profit: {e}")
        finally:
            try:
                db_conn_wp.close()
            except Exception:
                pass

    # Discovered groups — used to filter charts
    discovered = get_discovered_group_ids(run_dir)

    # Per-group reputation timeseries (discovered only)
    data["reputation_series"] = [s for s in get_reputation_series_from_db(run_dir) if s["group_id"] in discovered]

    # Quality per group/plan over time (discovered only)
    data["quality_series"] = [s for s in get_quality_series_from_db(run_dir) if s["group_id"] in discovered]

    # Q_min per group over time (discovered only)
    data["qmin_series"] = [s for s in get_qmin_series_from_db(run_dir) if s["group_id"] in discovered]

    # Per-group q_bias drift only — isolates group reactivity to competitor shocks
    # (excludes the much larger global accumulator). Discovered groups only.
    data["qmin_drift_only_series"] = [
        s for s in get_qmin_drift_only_series_from_db(run_dir) if s["group_id"] in discovered
    ]

    # Ads revenue per group over time (discovered only)
    data["ads_revenue_series"] = [s for s in get_ads_revenue_series_from_db(run_dir) if s["group_id"] in discovered]

    # Seat series (individual + enterprise)
    data["seat_series"] = get_seat_series_from_db(run_dir)

    # Seat series broken down per customer group
    data["seat_series_by_group"] = get_seat_series_by_group_from_db(run_dir)

    # Trailing 7-day churn rate per discovered group over time
    data["weekly_churn_by_group_series"] = [
        s for s in get_weekly_churn_by_group_series_from_db(run_dir)
        if s["group_id"] in discovered
    ]

    # Group discovery status
    data["group_discovery"] = get_group_discovery_from_db(run_dir)

    # Customer social media posts (last 50)
    data["customer_social_posts"] = get_customer_social_posts_from_db(run_dir)

    # Agent social media posts with scores, views, multipliers, replies (last 50)
    data["agent_social_posts"] = get_agent_social_posts_from_db(run_dir)

    # Next-day overall lead multiplier per group (from social media effects)
    try:
        from saas_bench.database import compute_social_media_multiplier
        sm_conn = _open_run_db(run_dir)
        if sm_conn and data.get("current_day"):
            next_day = data["current_day"] + 1
            next_day_mults = {}
            for gid in discovered:
                next_day_mults[gid] = round(compute_social_media_multiplier(sm_conn, next_day, gid), 4)
            data["next_day_social_multiplier"] = next_day_mults
            sm_conn.close()
    except Exception:
        pass

    # Recent actions (last 100)
    inner_id = inner_dir.name.replace("run_", "") if inner_dir != run_dir else run_id
    tr_path = inner_dir / "logs" / f"tool_results_{inner_id}.jsonl"
    actions = []
    if tr_path.exists():
        with open(tr_path) as f:
            for line in f:
                try:
                    actions.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        data["tool_calls_count"] = len(actions)
        # Keep last 100
        actions = actions[-100:]
        actions.reverse()
    data["recent_actions"] = actions

    # Daily rationales — primary source: bash agent's tool_results.jsonl, where
    # log_rationale calls appear as bash invocations of `nm.analytics.log_rationale(...)`.
    # We parse the rationale text out of the bash command argument.
    #
    # Why not the engine events JSONL? The engine's EventLogger appends to
    # agent_workspace/sessions/<sid>/logs/run_<sid>.jsonl from inside the Modal
    # sandbox, but append-mode writes don't sync reliably to the committed
    # volume view (only commits-after-close do). Empirically this leaves the
    # events file empty / very stale on the volume. tool_results.jsonl is
    # written by the bash agent's outer loop and IS committed reliably, so
    # it's our authoritative source for rationale text.
    #
    # We still UNION with events JSONL when present (gives us the engine's
    # canonical timestamp/day for entries that happen to be flushed).
    import re as _re
    rationales = []
    seen = set()  # dedupe by (day, first 64 chars of text)

    def _add(day, ts, text, turn=None):
        if not text:
            return
        key = (day, text[:64])
        if key in seen:
            return
        seen.add(key)
        rationales.append({
            "day": day,
            "turn": turn,
            "timestamp": ts,
            "text": text[:3000],
        })

    def _extract_rationale_from_bash(cmd):
        """Pull rationale text out of a bash command that invokes log_rationale.

        Patterns the agent uses (across models):
          1. log_rationale(rationale='''...''') / log_rationale('''...''')
          2. rationale = '''...''' ... log_rationale(rationale)
          3. cat > /tmp/x.txt << 'EOF'\n...\nEOF (then python reads file)
          4. python-c "rationale = \"\"\"...\"\"\" ..."  (escaped triples inside shell quotes)
          5. inline single/double-quoted rationale (rare)
        """
        if "log_rationale" not in cmd:
            return None
        # Some agents wrap python-c args in double quotes and escape inner `"""`
        # as `\"\"\"`. Normalize that variant by un-escaping a working copy.
        unesc = cmd.replace('\\"\\"\\"', '"""').replace("\\'\\'\\'", "'''")
        for src in (cmd, unesc) if unesc != cmd else (cmd,):
            # 1) Inline triple-quoted within the call
            m = _re.search(r"log_rationale\s*\([^)]*?'''(.*?)'''", src, _re.DOTALL)
            if m:
                return m.group(1).strip()
            m = _re.search(r'log_rationale\s*\([^)]*?"""(.*?)"""', src, _re.DOTALL)
            if m:
                return m.group(1).strip()
            # 2) Triple-quoted variable assignment (longest wins)
            candidates = list(_re.finditer(r"(?:^|\n)\s*[a-zA-Z_]\w*\s*=\s*'''(.*?)'''", src, _re.DOTALL))
            candidates += list(_re.finditer(r'(?:^|\n)\s*[a-zA-Z_]\w*\s*=\s*"""(.*?)"""', src, _re.DOTALL))
            if candidates:
                best = max(candidates, key=lambda mm: len(mm.group(1)))
                if len(best.group(1).strip()) > 50:
                    return best.group(1).strip()
            # 3) Bash heredoc body (longest wins)
            herecands = list(_re.finditer(r"<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1\b", src, _re.DOTALL))
            if herecands:
                best = max(herecands, key=lambda mm: len(mm.group(2)))
                if len(best.group(2).strip()) > 50:
                    return best.group(2).strip()
            # 4) Single/double-quoted inline (last resort)
            m = _re.search(r"log_rationale\s*\(\s*(?:rationale\s*=\s*)?'((?:[^'\\]|\\.)+)'", src, _re.DOTALL)
            if m and len(m.group(1).strip()) > 50:
                return m.group(1).strip()
            m = _re.search(r'log_rationale\s*\(\s*(?:rationale\s*=\s*)?"((?:[^"\\]|\\.)+)"', src, _re.DOTALL)
            if m and len(m.group(1).strip()) > 50:
                return m.group(1).strip()
        return None

    # Primary: scan tool_results.jsonl for bash calls that invoke log_rationale
    if tr_path.exists():
        with open(tr_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("tool") != "bash":
                    continue
                args = e.get("arguments") or {}
                cmd = args.get("command", "") if isinstance(args, dict) else ""
                if "log_rationale" not in cmd:
                    continue
                text = _extract_rationale_from_bash(cmd)
                if not text:
                    continue
                _add(e.get("day"), e.get("timestamp"), text, turn=e.get("turn"))

    # Supplement: events JSONL (the engine's authoritative source — when synced)
    events_glob = list((inner_dir / "agent_workspace" / "sessions").glob("*/logs/run_*.jsonl"))
    if events_glob:
        events_path = events_glob[0]
        # Build (timestamp → day) timeline from tool_results for events that
        # don't carry a day field.
        timeline = []  # list of (timestamp_str, day)
        if tr_path.exists():
            with open(tr_path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        ts = e.get("timestamp")
                        d = e.get("day")
                        if ts is not None and d is not None:
                            timeline.append((ts, d))
                    except json.JSONDecodeError:
                        continue
        timeline.sort(key=lambda x: x[0])

        def _day_at(ts):
            if not timeline or ts is None:
                return None
            import bisect
            idx = bisect.bisect_right([t[0] for t in timeline], ts)
            if idx == 0:
                return timeline[0][1]
            return timeline[idx - 1][1]

        try:
            with open(events_path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("category") != "log_rationale":
                        continue
                    a = (e.get("details") or {}).get("arguments") or {}
                    text = a.get("rationale") or ""
                    if not text:
                        continue
                    _add(_day_at(e.get("timestamp")), e.get("timestamp"), text)
        except OSError:
            pass

    rationales.sort(key=lambda r: (r.get("day") if r.get("day") is not None else -1,
                                    r.get("timestamp") or ""))
    data["daily_rationales"] = rationales

    # Recent raw responses (last 30)
    rr_path = inner_dir / "logs" / f"raw_responses_{inner_id}.jsonl"
    responses = []
    if rr_path.exists():
        with open(rr_path) as f:
            for line in f:
                try:
                    responses.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        responses = responses[-30:]
        responses.reverse()
    data["recent_responses"] = responses

    # Timing data (from timing_<run_id>.jsonl)
    timing_path = inner_dir / "logs" / f"timing_{inner_id}.jsonl"
    recent_turns = []
    if timing_path.exists():
        day_summaries = []
        try:
            with open(timing_path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("event") == "day_summary":
                            day_summaries.append(entry)
                        elif entry.get("event") in ("llm_call", "tool_exec"):
                            recent_turns.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

        # All day summaries for charts
        data["timing_day_summaries"] = day_summaries
        # Recent turns (last 50) for the timing log
        data["timing_recent_turns"] = recent_turns[-50:][::-1]
        # Cumulative timing stats
        if day_summaries:
            data["timing_total_llm"] = sum(d.get("llm_total_s", 0) for d in day_summaries)
            data["timing_total_step"] = sum(d.get("step_day_s", 0) for d in day_summaries)
            data["timing_total_tool"] = sum(d.get("tool_total_s", 0) for d in day_summaries)
            data["timing_avg_day"] = round(
                sum(d.get("elapsed_s", 0) for d in day_summaries) / len(day_summaries), 1
            )

    # Build unified recent_activity: merge tool_results + timing llm_calls
    # This ensures LLM thinking turns show up in the dashboard too
    activity = []
    for a in (actions or []):
        activity.append({
            "type": "tool",
            "tool": a.get("tool", "?"),
            "day": a.get("day"),
            "turn": a.get("turn"),
            "timestamp": a.get("timestamp"),
            "preview": _brief_args(a.get("arguments")),
        })
    for t in recent_turns[-100:]:
        if t.get("event") == "llm_call":
            activity.append({
                "type": "llm",
                "tool": t.get("tool", ""),
                "day": t.get("day"),
                "turn": t.get("turn"),
                "timestamp": t.get("timestamp"),
                "elapsed_s": t.get("elapsed_s"),
                "preview": (t.get("tool_preview") or "")[:80],
            })
    # Sort by timestamp descending, keep last 10
    activity.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    data["recent_activity"] = activity[:10]

    return data


_POOL: ProcessPoolExecutor | None = None
_POOL_SIZE: int = 0


def _worker_get_run_data(args):
    """Worker entry: bind RUN_PARENT for this rid, then call get_run_data.

    Each worker process keeps its own module globals (RUN_PARENT, _DB_CACHE)
    alive across calls. ProcessPoolExecutor.map does not pin tasks to workers,
    so a worker may process rid X in one cycle and rid Y in the next — the
    cache entry for X would otherwise leak its plain-decrypt tmp file. Prune
    any cache entries whose nmdb path is not under the current rid's run dir.
    """
    rid, parent_str = args
    RUN_PARENT[rid] = Path(parent_str)
    rid_marker = f"/run_{rid}/"
    for k in list(_DB_CACHE.keys()):
        if rid_marker not in k:
            _close_cached_entry(_DB_CACHE.pop(k))
    return get_run_data(rid)


def _get_pool(n_workers: int) -> ProcessPoolExecutor:
    """Lazily create and reuse a process pool sized to the run count."""
    global _POOL, _POOL_SIZE
    if _POOL is not None and _POOL_SIZE >= n_workers:
        return _POOL
    if _POOL is not None:
        _POOL.shutdown(wait=False, cancel_futures=True)
    _POOL_SIZE = max(n_workers, 1)
    _POOL = ProcessPoolExecutor(max_workers=_POOL_SIZE)
    return _POOL


def push_data():
    """Collect all run data and write to JSON file."""
    run_ids = get_run_ids()
    _sweep_orphan_plain_tmp_for_active_runs(set(run_ids))
    n = len(run_ids)
    if n > 1:
        # Parallelize per-run collection: each worker independently decrypts
        # its nmdb and runs DB queries. Cap workers to avoid CPU oversubscription
        # against the 8 active sim processes.
        max_workers = min(n, max((os.cpu_count() or 4) // 2, 1))
        pool = _get_pool(max_workers)
        args = [(rid, str(RUN_PARENT.get(rid, RUNS_DIR))) for rid in run_ids]
        runs = list(pool.map(_worker_get_run_data, args))
    else:
        runs = [get_run_data(rid) for rid in run_ids]
    all_data = {
        "timestamp": datetime.now(tz=__import__('datetime').timezone.utc).isoformat(),
        "runs": runs,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(all_data, f)
    size_mb = OUTPUT_FILE.stat().st_size / 1024 / 1024
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Pushed {len(run_ids)} runs ({size_mb:.1f} MB) to {OUTPUT_FILE}")

    # Upload to Modal volume
    try:
        result = subprocess.run(
            ["modal", "volume", "put", MODAL_VOLUME, str(OUTPUT_FILE), "/data.json", "--force"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print(f"  → Uploaded to Modal volume {MODAL_VOLUME}")
        else:
            # Volume might not exist yet, create it
            if "not found" in result.stderr.lower():
                subprocess.run(["modal", "volume", "create", MODAL_VOLUME], capture_output=True, text=True)
                subprocess.run(
                    ["modal", "volume", "put", MODAL_VOLUME, str(OUTPUT_FILE), "/data.json", "--force"],
                    capture_output=True, text=True, timeout=30,
                )
                print(f"  → Created volume and uploaded")
            else:
                print(f"  → Upload failed: {result.stderr.strip()}")
    except Exception as e:
        print(f"  → Upload error: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0, help="Loop interval in seconds (0 = one-shot)")
    args = parser.parse_args()

    if args.loop > 0:
        print(f"Pushing data every {args.loop}s. Ctrl+C to stop.")
        while True:
            try:
                push_data()
                time.sleep(args.loop)
            except KeyboardInterrupt:
                break
    else:
        push_data()


if __name__ == "__main__":
    main()
