"""SQL Agent jobs: readable schedules, step flow, and what each job changes in this database."""
from collections import defaultdict

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
ORDINAL = {1: "first", 2: "second", 4: "third", 8: "fourth", 16: "last"}
REL_DAY = {1: "Sunday", 2: "Monday", 3: "Tuesday", 4: "Wednesday", 5: "Thursday", 6: "Friday", 7: "Saturday",
           8: "day", 9: "weekday", 10: "weekend day"}
ACTION = {1: "quit reporting success", 2: "quit reporting failure", 3: "go to next step", 4: "go to step {}"}
WRITE_OPS = {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"}


def hhmm(t):
    t = int(t or 0)
    return f"{t // 10000:02d}:{t // 100 % 100:02d}"


def schedule_text(s):
    ft = s.get("freq_type")
    fi = s.get("freq_interval") or 0
    rec = s.get("freq_recurrence_factor") or 1
    if ft == 1:
        d = str(s.get("active_start_date") or "")
        return f"Once on {d[:4]}-{d[4:6]}-{d[6:8]} at {hhmm(s.get('active_start_time'))}"
    if ft == 64:
        return "When SQL Server Agent starts"
    if ft == 128:
        return "When the CPU is idle"
    if ft == 4:
        days = "Daily" if fi <= 1 else f"Every {fi} days"
    elif ft == 8:
        picked = [DAYS[i] for i in range(7) if fi & (1 << i)]
        if picked == DAYS[1:6]:
            days = "Every weekday (Mon-Fri)"
        elif len(picked) == 7:
            days = "Daily"
        else:
            days = ("Weekly" if rec <= 1 else f"Every {rec} weeks") + " on " + ", ".join(picked)
    elif ft == 16:
        days = ("Monthly" if rec <= 1 else f"Every {rec} months") + f" on day {fi}"
    elif ft == 32:
        days = (("Monthly" if rec <= 1 else f"Every {rec} months")
                + f" on the {ORDINAL.get(s.get('freq_relative_interval'), '?')} {REL_DAY.get(fi, '?')}")
    else:
        days = f"Schedule type {ft}"
    sub, n = s.get("freq_subday_type"), s.get("freq_subday_interval") or 0
    if sub in (None, 0, 1):
        return f"{days} at {hhmm(s.get('active_start_time'))}"
    unit = {2: "seconds", 4: "minutes", 8: "hours"}.get(sub, "?")
    return (f"{days}, every {n} {unit} between {hhmm(s.get('active_start_time'))} and "
            f"{hhmm(s.get('active_end_time'))}")


def interval_seconds(s):
    """Repeat interval for intra-day schedules, else None."""
    mult = {2: 1, 4: 60, 8: 3600}.get(s.get("freq_subday_type"))
    return mult * (s.get("freq_subday_interval") or 0) if mult else None


def _fmt_secs(x):
    if x is None:
        return ""
    x = int(x)
    return f"{x // 3600}h {x // 60 % 60:02d}m" if x >= 3600 else f"{x // 60}m {x % 60:02d}s" if x >= 60 else f"{x}s"


def analyze_jobs(catalog, analyzer, doc):
    """Returns the jobs that touch this database, with their effect on procedures and tables."""
    db = catalog.database
    by_mod = {m["key"]: m for m in doc["modules"]}
    calls = {m["key"]: [c["target"] for c in m["calls"]] for m in doc["modules"]}
    usage = defaultdict(list)
    for u in doc["usage"]:
        usage[u["module"]].append(u)

    def reach(keys):
        """All modules reachable through EXEC calls."""
        seen, stack = set(), list(keys)
        while stack:
            k = stack.pop()
            if k in seen or k not in by_mod:
                continue
            seen.add(k)
            stack += calls.get(k, [])
        return seen

    out, skipped = [], 0
    for j in catalog.data.get("jobs") or []:
        steps, direct_mods, direct_refs, relevant = [], set(), [], False
        for s in j["steps"]:
            sub = (s.get("subsystem") or "").upper()
            step = {"id": s["step_id"], "name": s["step_name"], "subsystem": s["subsystem"],
                    "database": s.get("database_name"), "command": (s.get("command") or "")[:4000],
                    "on_success": ACTION.get(s.get("on_success_action"), "?").format(s.get("on_success_step_id")),
                    "on_fail": ACTION.get(s.get("on_fail_action"), "?").format(s.get("on_fail_step_id")),
                    "retries": s.get("retry_attempts") or 0, "calls": [], "refs": []}
            if sub == "TSQL":
                in_db = (s.get("database_name") or "").lower() == db
                an = analyzer.analyze(s.get("command") or "", "dbo")
                step["calls"] = sorted({k for k, _, ok in an.calls if ok})
                step["refs"] = sorted({(r.key, r.op) for r in an.refs})
                if in_db or step["calls"] or step["refs"] or db in (s.get("command") or "").lower():
                    relevant = True
                direct_mods |= set(step["calls"])
                direct_refs += step["refs"]
                step["dynamic_sql"] = an.dynamic_sql
            else:
                step["external"] = True
                if db in (s.get("command") or "").lower() or (s.get("database_name") or "").lower() == db:
                    relevant = True
            steps.append(step)
        if not relevant:
            skipped += 1
            continue

        mods = reach(direct_mods)
        writes, reads = defaultdict(set), defaultdict(set)
        for key, op in direct_refs:
            (writes if op in WRITE_OPS else reads)[key].add("job step")
        for m in mods:
            for u in usage[m]:
                (writes if u["op"] in WRITE_OPS else reads)[u["object"]].add(m)
        triggers = sorted({tr["key"] for tr in doc["modules"] if tr["type_code"] == "TR" and tr["parent"] in writes})

        scheds = [{"name": s["name"], "enabled": s["enabled"], "text": schedule_text(s),
                   "start": hhmm(s.get("active_start_time")), "interval_secs": interval_seconds(s),
                   "next_run": (f"{str(s['next_run_date'])[:4]}-{str(s['next_run_date'])[4:6]}-{str(s['next_run_date'])[6:8]} "
                                f"{hhmm(s.get('next_run_time'))}") if s.get("next_run_date") else None}
                  for s in j["schedules"]]
        h = j.get("history") or {}
        hist = {"runs_30d": h.get("runs_30d"), "failures_30d": h.get("failures_30d"),
                "avg": _fmt_secs(h.get("avg_secs")), "max": _fmt_secs(h.get("max_secs")),
                "last_status": {0: "Failed", 1: "Succeeded", 2: "Retry", 3: "Canceled", 4: "In progress"}.get(h.get("last_status")),
                "last_run": (f"{str(h['last_run_date'])[:4]}-{str(h['last_run_date'])[4:6]}-{str(h['last_run_date'])[6:8]} "
                             f"{hhmm(h.get('last_run_time'))}") if h.get("last_run_date") else None,
                "last_duration": _fmt_secs(h.get("last_secs"))}

        warn = []
        if not j["enabled"]:
            warn.append("Job is disabled.")
        if not scheds:
            warn.append("No schedule: started manually, by another job or by an application.")
        elif not any(s["enabled"] for s in scheds):
            warn.append("All schedules are disabled.")
        for s in j["steps"]:
            if s.get("on_fail_action") == 1:
                warn.append(f"Step {s['step_id']} '{s['step_name']}': on failure the job quits reporting SUCCESS, so a "
                            "failure is hidden from monitoring.")
            elif s.get("on_fail_action") == 3 and s["step_id"] < len(j["steps"]):
                warn.append(f"Step {s['step_id']} '{s['step_name']}': on failure the job continues with the next step; "
                            "check later steps cope with partial data.")
        last_step = max((s["step_id"] for s in j["steps"]), default=0)
        for s in j["steps"]:
            if s.get("on_success_action") == 1 and s["step_id"] < last_step:
                warn.append(f"Step {s['step_id']} quits on success, so steps after it never run unless jumped to.")
        if h.get("failures_30d"):
            warn.append(f"Failed {h['failures_30d']} of {h.get('runs_30d')} runs in the last 30 days.")
        for s in scheds:
            if s["interval_secs"] and h.get("max_secs") and h["max_secs"] > s["interval_secs"]:
                warn.append(f"Longest run ({_fmt_secs(h['max_secs'])}) exceeds the repeat interval of schedule "
                            f"'{s['name']}' ({_fmt_secs(s['interval_secs'])}): runs can be skipped or overlap.")
        if any(st.get("external") for st in steps):
            warn.append("Has non-T-SQL steps (" + ", ".join(sorted({st['subsystem'] for st in steps if st.get('external')}))
                        + "): external integration point; its table usage is not analyzed.")
        if any(st.get("dynamic_sql") for st in steps) or any(by_mod[m]["dynamic_sql"] for m in mods):
            warn.append("Runs dynamic SQL: tables chosen at runtime are not listed.")

        out.append({
            "id": j["job_id"], "name": j["name"], "enabled": j["enabled"], "category": j.get("category"),
            "owner": j.get("owner"), "description": j.get("description"), "modified": j.get("modified"),
            "schedules": scheds, "history": hist, "steps": [
                {**st, "refs": [{"object": k, "op": op} for k, op in st["refs"]]} for st in steps],
            "procedures": sorted(mods), "direct_procedures": sorted(direct_mods),
            "writes": {k: sorted(v) for k, v in sorted(writes.items())},
            "reads": {k: sorted(v) for k, v in sorted(reads.items())},
            "fires_triggers": triggers, "warnings": warn,
            "first_start": min((s["start"] for s in scheds if s["enabled"]), default="99:99"),
        })
    out.sort(key=lambda x: (x["first_start"], x["name"].lower()))
    return out, skipped
