#!/usr/bin/env python3
"""Live agent action monitor for bash_agent — tails JSONL logs and shows progress.

Adapted from monitor_live.py for the bash_agent architecture:
- Workspace at RUN_DIR/agent_workspace/ (not RUN_DIR itself)
- Shows workspace file contents (notes, strategy docs) instead of _memory entries
- Tool emojis for bash/file tools
- Full bash command + output display

Features:
- Progress bar per day with ETA
- Full tool call details AND results
- Daily dashboard from JSONL logs
- Workspace file snapshots per day
- Hidden stats (reputation, satisfaction, etc.)
"""

import sys
import time
import sqlite3
import json
import os
import re
from pathlib import Path
from datetime import datetime

RUN_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if not RUN_DIR or not RUN_DIR.exists():
    print("Usage: python monitor_bash_agent.py <run_directory>")
    sys.exit(1)

DB_PATH = RUN_DIR / "world.db"
LOGS_DIR = RUN_DIR / "logs"
WORKSPACE_DIR = RUN_DIR / "agent_workspace"
TOTAL_DAYS = 3650
POLL_INTERVAL = 3

# ═══════════════════════════════════════════════════════════════════
# Utility Functions
# ═══════════════════════════════════════════════════════════════════

def get_db_stats():
    """Get current day, cash, subscribers from DB."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        day = conn.execute("SELECT COALESCE(MAX(day), 0) FROM ledger").fetchone()[0]
        cash = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger").fetchone()[0]
        subs = conn.execute("""
            SELECT COUNT(*) FROM subscriptions
            WHERE status='subscribed' AND end_day IS NULL
        """).fetchone()[0]
        conn.close()
        return day, cash, subs
    except:
        return None, None, None


def get_hidden_stats(day):
    """Get stats the agent CANNOT see — for observer only."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        lines = []

        # Group reputations (HIDDEN from agent)
        reps = conn.execute("SELECT group_id, reputation FROM group_reputation ORDER BY group_id").fetchall()
        if reps:
            rep_str = " ".join(f"{r['group_id']}={r['reputation']:.2f}" for r in reps)
            lines.append(f"  🏆 Reputation: {rep_str}")

        # Average satisfaction (HIDDEN from agent)
        avg_sat = conn.execute("""
            SELECT AVG(cs.satisfaction) FROM customer_state cs
            JOIN subscriptions s ON cs.customer_id = s.customer_id
            WHERE s.status = 'subscribed' AND s.end_day IS NULL
        """).fetchone()[0]
        if avg_sat is not None:
            lines.append(f"  😊 Avg Satisfaction: {avg_sat:.3f}")

        # Satisfaction by group (HIDDEN)
        sat_by_group = conn.execute("""
            SELECT c.group_id, AVG(cs.satisfaction) as avg_sat, COUNT(*) as cnt
            FROM customer_state cs
            JOIN subscriptions s ON cs.customer_id = s.customer_id
            JOIN customers c ON cs.customer_id = c.customer_id
            WHERE s.status = 'subscribed' AND s.end_day IS NULL
            GROUP BY c.group_id ORDER BY c.group_id
        """).fetchall()
        if sat_by_group:
            sat_str = " ".join(f"{r['group_id']}={r['avg_sat']:.2f}({r['cnt']})" for r in sat_by_group)
            lines.append(f"  📊 Satisfaction by group: {sat_str}")

        # Group awareness
        awareness = conn.execute("SELECT group_id, awareness FROM group_awareness ORDER BY group_id").fetchall()
        if awareness:
            aw_str = " ".join(f"{r['group_id']}={r['awareness']:.2f}" for r in awareness)
            lines.append(f"  📡 Awareness: {aw_str}")

        # Service metrics
        service = conn.execute(
            "SELECT * FROM service_day WHERE day = ?", (day,)
        ).fetchone()
        if service:
            usage = service['total_usage_units']
            cap = service['capacity_units']
            util = (usage / cap * 100) if cap > 0 else 0
            lines.append(f"  ⚡ Usage: {usage:,}/{cap:,} ({util:.0f}%)  │  P95: {service['p95_ms']:.0f}ms  │  Err: {service['error_rate']:.3f}  │  Down: {service['downtime_minutes']}min")

        # Day's financials
        revenue = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE day = ? AND amount > 0",
            (day,)
        ).fetchone()[0]
        costs = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE day = ? AND amount < 0",
            (day,)
        ).fetchone()[0]
        new_subs = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status='subscribed' AND start_day = ?",
            (day,)
        ).fetchone()[0]
        cancels = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status='cancelled' AND end_day = ?",
            (day,)
        ).fetchone()[0]
        free_trials = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status='free_trial' AND start_day = ?",
            (day,)
        ).fetchone()[0]

        # Dividends (use dividends table which has founder_payout directly)
        founder_dividends = abs(conn.execute(
            "SELECT COALESCE(SUM(founder_payout), 0) FROM dividends"
        ).fetchone()[0])
        total_shares = conn.execute(
            "SELECT COALESCE(SUM(shares_held), 1) FROM shareholders"
        ).fetchone()[0]
        founder_shares = conn.execute(
            "SELECT COALESCE(shares_held, 0) FROM shareholders WHERE shareholder_type = 'founder'"
        ).fetchone()
        founder_pct = (founder_shares[0] / total_shares) if founder_shares else 1.0

        # MRR by plan
        subs_by_plan = conn.execute("""
            SELECT plan, COUNT(*) as cnt, COALESCE(SUM(effective_price), 0) as mrr
            FROM subscriptions WHERE status='subscribed' AND end_day IS NULL
            GROUP BY plan ORDER BY plan
        """).fetchall()
        total_mrr = sum(r['mrr'] for r in subs_by_plan)

        lines.append(f"  💵 MRR: ${total_mrr:,.0f}  │  Rev: ${revenue:,.0f}  │  Costs: ${abs(costs):,.0f}  │  Net: ${revenue + costs:+,.0f}")
        lines.append(f"  💰 Founder Dividends: ${founder_dividends:,.0f} (founder owns {founder_pct*100:.1f}%)")
        lines.append(f"  📊 Today: +{new_subs} new, -{cancels} cancel, {free_trials} free_trial")
        for r in subs_by_plan:
            lines.append(f"     Plan {r['plan']}: {r['cnt']:>4} subs (${r['mrr']:,.0f}/mo)")

        # Enterprise/VC threads
        open_enterprise = conn.execute("""
            SELECT COUNT(DISTINCT thread_id) as cnt
            FROM enterprise_turns
            WHERE closed = 0
        """).fetchone()['cnt']
        open_vc = conn.execute("""
            SELECT COUNT(DISTINCT shareholder_id) as cnt
            FROM vc_turns
            WHERE closed = 0
        """).fetchone()['cnt']
        if open_enterprise or open_vc:
            lines.append(f"  🤝 Open threads: enterprise={open_enterprise} vc={open_vc}")

        # R&D projects
        try:
            rd = conn.execute("""
                SELECT tier, status, actual_completion_day
                FROM research_projects ORDER BY tier
            """).fetchall()
            if rd:
                rd_str = " ".join(
                    f"T{r['tier']}={r['status']}" + (f"(d{r['actual_completion_day']})" if r['actual_completion_day'] else "")
                    for r in rd
                )
                lines.append(f"  🔬 R&D: {rd_str}")
        except:
            pass

        conn.close()
        return "\n".join(lines)
    except Exception as e:
        return f"  (hidden stats error: {e})"


def get_disk_usage():
    """Get disk usage for the run directory."""
    try:
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        logs_size = sum(f.stat().st_size for f in LOGS_DIR.glob("*") if f.is_file()) if LOGS_DIR.exists() else 0
        total_size = sum(f.stat().st_size for f in RUN_DIR.rglob("*") if f.is_file())
        return db_size, logs_size, total_size
    except:
        return 0, 0, 0


def fmt_size(n):
    """Format bytes to human-readable."""
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n/1024:.1f}KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n/1024/1024:.1f}MB"
    else:
        return f"{n/1024/1024/1024:.2f}GB"


def progress_bar(current, total, width=40):
    pct = current / total if total > 0 else 0
    filled = int(width * pct)
    bar = '█' * filled + '░' * (width - filled)
    return f"[{bar}] {pct*100:5.1f}%"


def format_eta(seconds):
    if seconds <= 0:
        return "done"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def format_timestamp(ts_str):
    """Parse ISO timestamp to HH:MM:SS."""
    if ts_str:
        try:
            dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            return dt.strftime("%H:%M:%S")
        except:
            return ts_str[:8]
    return datetime.now().strftime("%H:%M:%S")


# Tool emojis for bash_agent
TOOL_EMOJIS = {
    'bash': '🖥️ ',
    'read_file': '📂',
    'write_file': '✍️ ',
    'edit_file': '✏️ ',
    'search_files': '🔍',
    'glob_files': '📁',
    '_dashboard': '📊',
}


# ═══════════════════════════════════════════════════════════════════
# Workspace File Tracking
# ═══════════════════════════════════════════════════════════════════

# Files to always show contents of at day transitions (agent's "memory")
MEMORY_FILES = [
    'MEMORY.md',
    'strategy.md',
    'notes.md',
    'status.md',
    'plan.md',
    'scratchpad.md',
]

def read_workspace_notes(workspace_dir):
    """Read the agent's note/strategy files from workspace."""
    notes = {}
    if not workspace_dir.exists():
        return notes

    # Check known memory-like files first
    for fname in MEMORY_FILES:
        fpath = workspace_dir / fname
        if fpath.exists():
            try:
                content = fpath.read_text()
                if content.strip():
                    notes[fname] = content
            except:
                pass

    # Also check for any .md or .txt files in root (agent's custom files)
    for fpath in sorted(workspace_dir.glob('*.md')):
        fname = fpath.name
        if fname not in notes and fname not in ('README.md',):
            try:
                content = fpath.read_text()
                if content.strip():
                    notes[fname] = content
            except:
                pass

    for fpath in sorted(workspace_dir.glob('*.txt')):
        fname = fpath.name
        if fname not in notes:
            try:
                content = fpath.read_text()
                if content.strip():
                    notes[fname] = content
            except:
                pass

    return notes


def snapshot_workspace(workspace_dir):
    """Take a snapshot of all files in workspace (excluding docs/ and daily_scripts/)."""
    snapshot = {}
    if not workspace_dir.exists():
        return snapshot
    for path in workspace_dir.rglob("*"):
        if path.is_file():
            rel = str(path.relative_to(workspace_dir))
            # Skip docs (static, generated), novamind_api (static), __pycache__
            if rel.startswith(("docs/", "novamind_api/", "__pycache__/", ".")) or rel.endswith(('.pyc',)):
                continue
            try:
                content = path.read_text(errors='replace')
                snapshot[rel] = content
            except:
                try:
                    snapshot[rel] = f"<binary: {path.stat().st_size} bytes>"
                except:
                    pass
    return snapshot


def compute_workspace_diff(old_snap, new_snap):
    """Compute diff between two workspace snapshots."""
    diffs = []
    all_files = set(old_snap.keys()) | set(new_snap.keys())
    for f in sorted(all_files):
        if f not in old_snap:
            content = new_snap[f]
            diffs.append(f"  +++ NEW FILE: {f}\n{_indent(content, '  │ ')}")
        elif f not in new_snap:
            diffs.append(f"  --- DELETED: {f}")
        elif old_snap[f] != new_snap[f]:
            old_lines = old_snap[f].splitlines()
            new_lines = new_snap[f].splitlines()
            diff_lines = _simple_diff(old_lines, new_lines, max_lines=20)
            if diff_lines:
                diffs.append(f"  ~~~ MODIFIED: {f}\n{_indent(diff_lines, '  │ ')}")
    return diffs


def _simple_diff(old_lines, new_lines, max_lines=200):
    if old_lines == new_lines:
        return ""
    lines = []
    import difflib
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm='', n=2))
    for line in diff[:max_lines]:
        lines.append(line)
    if len(diff) > max_lines:
        lines.append(f"... ({len(diff) - max_lines} more diff lines)")
    return "\n".join(lines)


def _indent(text, prefix):
    return "\n".join(prefix + line for line in text.splitlines())


# ═══════════════════════════════════════════════════════════════════
# Main Monitor Loop
# ═══════════════════════════════════════════════════════════════════

def main():
    # Find log files
    tool_log = None
    for f in LOGS_DIR.glob("tool_results_*.jsonl"):
        tool_log = f
        break

    print("═" * 80, flush=True)
    print("  SaaS Bench Bash Agent - LIVE MONITOR", flush=True)
    print(f"  Run: {RUN_DIR.name}", flush=True)
    print(f"  Workspace: {WORKSPACE_DIR}", flush=True)
    print("═" * 80, flush=True)
    print(flush=True)

    start_time = time.time()
    tool_pos = 0
    last_day = -1
    last_action_day = None
    day_times = []
    workspace_snapshot = snapshot_workspace(WORKSPACE_DIR)
    pending_dashboard = None

    # On startup, scan entire JSONL to catch up
    if tool_log and tool_log.exists():
        try:
            with open(tool_log, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get('tool') == '_dashboard':
                            pending_dashboard = entry.get('result', '')
                    except json.JSONDecodeError:
                        pass
                tool_pos = f.tell()
        except:
            tool_pos = 0

    while True:
        day, cash, subs = get_db_stats()
        if day is None:
            time.sleep(POLL_INTERVAL)
            continue

        now = time.time()

        # ═══════════════════════════════════════════════════════
        # Read new tool results from JSONL
        # ═══════════════════════════════════════════════════════
        new_actions = []
        if tool_log and tool_log.exists():
            try:
                with open(tool_log, 'r') as f:
                    f.seek(tool_pos)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            new_actions.append(entry)
                        except json.JSONDecodeError:
                            pass
                    tool_pos = f.tell()
            except:
                pass

        # Separate dashboard entries from tool actions
        tool_actions = []
        for entry in new_actions:
            if entry.get('tool') == '_dashboard':
                pending_dashboard = entry.get('result', '')
            else:
                tool_actions.append(entry)

        # ═══════════════════════════════════════════════════════
        # Day Transition: progress bar + dashboard + notes + file diffs
        # ═══════════════════════════════════════════════════════
        if day != last_day:
            day_times.append((day, now))
            if len(day_times) > 50:
                day_times = day_times[-50:]

            # ETA
            eta_str = "..."
            if len(day_times) >= 2:
                recent = day_times[-min(20, len(day_times)):]
                d_elapsed = recent[-1][0] - recent[0][0]
                t_elapsed = recent[-1][1] - recent[0][1]
                if d_elapsed > 0:
                    secs_per_day = t_elapsed / d_elapsed
                    remaining = (TOTAL_DAYS - day) * secs_per_day
                    eta_str = format_eta(remaining)

            elapsed = format_eta(now - start_time)
            bar = progress_bar(day, TOTAL_DAYS)

            # Workspace file diffs
            new_snapshot = snapshot_workspace(WORKSPACE_DIR)
            diffs = compute_workspace_diff(workspace_snapshot, new_snapshot)
            workspace_snapshot = new_snapshot

            # Print diffs from previous day
            if last_day >= 0 and diffs:
                print(flush=True)
                print(f"  📁 WORKSPACE CHANGES (Day {last_day}):", flush=True)
                for d in diffs:
                    print(d, flush=True)
                print(flush=True)

            db_sz, logs_sz, total_sz = get_disk_usage()

            print(f"{'═'*80}", flush=True)
            print(f"  {bar}  Day {day}/{TOTAL_DAYS}  ETA: {eta_str}  Elapsed: {elapsed}", flush=True)
            print(f"  💾 Disk: DB={fmt_size(db_sz)}  Logs={fmt_size(logs_sz)}  Total={fmt_size(total_sz)}", flush=True)
            print(f"{'═'*80}", flush=True)

            # Agent dashboard from JSONL
            print(f"  ┌─ AGENT DASHBOARD ────────────────────────────────────────┐", flush=True)
            if pending_dashboard:
                for dline in pending_dashboard.splitlines():
                    print(f"  │ {dline}", flush=True)
                pending_dashboard = None
            else:
                print(f"  │ (waiting for dashboard data...)", flush=True)
            print(f"  └──────────────────────────────────────────────────────────┘", flush=True)

            # Agent workspace notes (the agent's persistent "memory")
            notes = read_workspace_notes(WORKSPACE_DIR)
            print(f"  ┌─ 📝 AGENT WORKSPACE NOTES (persistent memory) ───────────┐", flush=True)
            if notes:
                for fname, content in notes.items():
                    print(f"  │ ╔══ {fname} ══╗", flush=True)
                    for nline in content.splitlines():
                        print(f"  │ ║ {nline}", flush=True)
                    print(f"  │ ╚{'═'*len(fname)}{'═'*4}╝", flush=True)
            else:
                print(f"  │ (no notes files yet)", flush=True)
            print(f"  └──────────────────────────────────────────────────────────┘", flush=True)

            # Hidden stats
            print(f"  ┌─ 🔒 HIDDEN STATS (agent cannot see) ─────────────────────┐", flush=True)
            hidden = get_hidden_stats(day)
            print(hidden, flush=True)
            print(f"  └──────────────────────────────────────────────────────────┘", flush=True)
            print(f"{'─'*80}", flush=True)

            last_day = day

        # ═══════════════════════════════════════════════════════
        # Display new tool actions
        # ═══════════════════════════════════════════════════════
        for entry in tool_actions:
            ts_short = format_timestamp(entry.get('timestamp', ''))
            tool = entry.get('tool', '?')
            turn = entry.get('turn', '?')
            eday = entry.get('day', '?')
            args = entry.get('arguments', {})
            result = str(entry.get('result', ''))
            emoji = TOOL_EMOJIS.get(tool, '🔧')

            # Day separator when actions cross day boundary
            if isinstance(eday, int) and eday != last_action_day:
                elapsed = format_eta(now - start_time)
                bar = progress_bar(eday, TOTAL_DAYS)
                print(f"{'─'*80}", flush=True)
                print(f"  {bar}  Day {eday}/{TOTAL_DAYS}  Elapsed: {elapsed}", flush=True)
                print(f"{'─'*80}", flush=True)
                last_action_day = eday

            prefix = f" D{eday:<3}│ {ts_short} │ T{turn:<4}│"
            indent = f"     │          │      │"

            # Tool call header
            print(f"{prefix} {emoji} {tool}", flush=True)

            # Tool-specific argument display
            if tool == 'bash':
                cmd = args.get('command', '')
                for cmd_line in cmd.splitlines():
                    print(f"{indent}   $ {cmd_line}", flush=True)
            elif tool == 'read_file':
                path = args.get('path', '')
                offset = args.get('offset', '')
                limit = args.get('limit', '')
                extra = ""
                if offset:
                    extra += f" offset={offset}"
                if limit:
                    extra += f" limit={limit}"
                print(f"{indent}   📂 {path}{extra}", flush=True)
            elif tool == 'write_file':
                path = args.get('path', '')
                content = args.get('content', '')
                print(f"{indent}   📄 {path} ({len(content)} chars)", flush=True)
                # Show full file content (no truncation)
                content_lines = content.splitlines()
                for cl in content_lines:
                    print(f"{indent}   │ {cl}", flush=True)
            elif tool == 'edit_file':
                path = args.get('path', '')
                old_str = args.get('old_string', '')
                new_str = args.get('new_string', '')
                print(f"{indent}   📄 {path}", flush=True)
                for ol in old_str.splitlines():
                    print(f"{indent}   - {ol}", flush=True)
                for nl in new_str.splitlines():
                    print(f"{indent}   + {nl}", flush=True)
            elif tool == 'search_files':
                pattern = args.get('pattern', '')
                path = args.get('path', '.')
                glob_filter = args.get('glob', '')
                print(f"{indent}   🔎 /{pattern}/ in {path}" + (f" (glob={glob_filter})" if glob_filter else ""), flush=True)
            elif tool == 'glob_files':
                pattern = args.get('pattern', '')
                print(f"{indent}   📁 {pattern}", flush=True)
            else:
                args_str = json.dumps(args, default=str, ensure_ascii=False)
                print(f"{indent}   📥 {args_str}", flush=True)

            # Result display (no truncation — show everything)
            if result:
                result_lines = result.splitlines()
                for rline in result_lines:
                    print(f"{indent}   → {rline}", flush=True)

            print(f"{indent}", flush=True)

        # Check completion
        if day >= TOTAL_DAYS:
            new_snapshot = snapshot_workspace(WORKSPACE_DIR)
            diffs = compute_workspace_diff(workspace_snapshot, new_snapshot)
            if diffs:
                print(flush=True)
                print(f"  📁 FINAL WORKSPACE CHANGES:", flush=True)
                for d in diffs:
                    print(d, flush=True)

            elapsed = format_eta(now - start_time)
            print(flush=True)
            print("═" * 80, flush=True)
            print(f"  ✅ SIMULATION COMPLETE!", flush=True)
            print(f"  Final Cash:  ${cash:,.0f}", flush=True)
            print(f"  Final Subs:  {subs}", flush=True)
            print(f"  Total Time:  {elapsed}", flush=True)
            print("═" * 80, flush=True)
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
