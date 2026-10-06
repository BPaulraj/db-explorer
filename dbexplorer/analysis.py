"""Run the analyzer over every module and assemble the analysis document used by all reports."""
from collections import defaultdict
from datetime import datetime

from .model import MODULE_TYPES
from .prefix import detect_prefixes
from .relations import build_relationships
from .sensitive import classify
from .jobs import analyze_jobs
from .tsql import Analyzer, error_catalog, header_parameters, metrics

OP_ORDER = ["INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "SELECT", "REFERENCED"]
WRITE_OPS = {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"}


def run_analysis(catalog, cfg, validation=None, profile=None):
    profile = profile or {}
    prefixes = detect_prefixes(catalog, cfg)
    analyzer = Analyzer(catalog)
    analyses, errors = {}, []
    for m in catalog.modules():
        try:
            analyses[m["key"]] = analyzer.analyze(m.get("definition") or "", m["schema"].lower())
        except Exception as ex:  # keep going; report the module for manual review
            errors.append({"object": m["display"], "error": repr(ex)})

    edges, key_groups, logical = build_relationships(catalog, prefixes, analyses, cfg, validation)

    # ---- usage rows: one per (table/view/function, module, operation)
    usage = {}
    for mkey, an in analyses.items():
        for r in an.refs:
            k = (r.key, mkey, r.op)
            u = usage.setdefault(k, {"object": r.key, "module": mkey, "op": r.op, "lines": set(),
                                     "cols_read": set(), "cols_written": set(), "dynamic": False,
                                     "details": set(), "source": "parser"})
            u["lines"].add(r.line)
            u["cols_read"] |= r.cols_read
            u["cols_written"] |= r.cols_written
            u["dynamic"] = u["dynamic"] or r.dynamic
            if r.detail:
                u["details"].add(r.detail)

    # Catalog dependencies the parser did not classify (or could not see).
    calls = defaultdict(dict)       # caller -> {callee: [lines]}
    for mkey, an in analyses.items():
        for target, line, resolved in an.calls:
            if target != mkey:
                calls[mkey].setdefault(target, []).append(line)
    parsed_pairs = {(u["object"], u["module"]) for u in usage.values()}
    for d in catalog.data["dependencies"]:
        src = catalog.by_id.get(d["referencing_id"])
        dst = catalog.by_id.get(d["referenced_id"]) if d.get("referenced_id") else None
        if not src or src["type"] not in MODULE_TYPES or not dst:
            continue
        if dst["type"] in ("U", "V") and (dst["key"], src["key"]) not in parsed_pairs:
            usage[(dst["key"], src["key"], "REFERENCED")] = {
                "object": dst["key"], "module": src["key"], "op": "REFERENCED", "lines": set(),
                "cols_read": set(), "cols_written": set(), "dynamic": False,
                "details": {"listed by sys.sql_expression_dependencies only"}, "source": "catalog"}
        elif dst["type"] in ("P", "FN", "IF", "TF") and dst["key"] not in calls[src["key"]] and dst["key"] != src["key"]:
            calls[src["key"]][dst["key"]] = []

    usage_rows = []
    for u in usage.values():
        usage_rows.append({**u, "lines": sorted(u["lines"]), "cols_read": sorted(u["cols_read"]),
                           "cols_written": sorted(u["cols_written"]), "details": sorted(u["details"])})
    usage_rows.sort(key=lambda u: (u["object"], u["module"], OP_ORDER.index(u["op"])))

    rules = defaultdict(list)          # table key -> CHECK / DEFAULT / COMPUTED rows
    for r in catalog.data.get("constraints", []):
        o = catalog.by_id.get(r["id"])
        if o:
            rules[o["key"]].append(r)

    tables = []
    for o in sorted(catalog.objects.values(), key=lambda o: o["key"]):
        if o["type"] not in ("U", "V"):
            continue
        pk = catalog.pk.get(o["key"], [])
        p = prefixes.get(o["key"], {})
        prof = profile.get(o["key"], {})
        pcols = prof.get("columns", {})
        cols = []
        for c in catalog.columns.get(o["key"], []):
            low = c["name"].lower()
            lname = logical.get((o["key"], low), _strip(c["name"], p.get("prefix", "")))
            col = {"name": c["name"], "type": _type_str(c), "nullable": bool(c["nullable"]),
                   "identity": bool(c["identity"]), "pk": low in pk, "logical": lname}
            for r in rules[o["key"]]:
                if (r["column_name"] or "").lower() == low:
                    col[{"CHECK": "check", "DEFAULT": "default", "COMPUTED": "computed"}[r["kind"]]] = r["definition"]
            if low in pcols:
                col["profile"] = pcols[low]
            # Name/config rules are re-evaluated every run; format-based hits come from the profile.
            pc = pcols.get(low, {})
            excluded = low in cfg.sensitive_exclude or lname in cfg.sensitive_exclude
            sens = classify(low, lname, None, cfg, o["name"]) or (
                pc.get("sensitive") if pc.get("sensitive_basis", "values") == "values" and not excluded else None)
            if sens:
                col["sensitive"] = sens
            cols.append(col)
        tables.append({
            "key": o["key"], "display": o["display"], "kind": "table" if o["type"] == "U" else "view",
            "rows": o.get("rows"), "prefix": p.get("prefix", ""), "prefix_method": p.get("method", ""),
            "prefix_coverage": p.get("coverage", 0), "pk": pk,
            "unique": [u for u in catalog.unique.get(o["key"], []) if u != pk],
            "created": o.get("created"), "modified": o.get("modified"),
            "checks": [{"name": r["name"], "column": r["column_name"], "definition": r["definition"],
                        "disabled": bool(r["disabled"])} for r in rules[o["key"]] if r["kind"] == "CHECK"],
            "profiled": {k: prof[k] for k in ("sampled_rows", "full_scan", "profiled_at", "error") if k in prof},
            "columns": cols,
        })

    latest = max((m.get("modified") or "" for m in catalog.modules()), default="")
    user_messages = {m["message_id"]: m for m in catalog.data.get("messages") or []}
    modules = []
    for m in sorted(catalog.modules(), key=lambda m: m["key"]):
        an = analyses.get(m["key"])
        defaults = header_parameters(m.get("definition")) if m.get("definition") else {}
        params = [{"name": p["name"], "type": _type_str(p), "output": bool(p["is_output"]),
                   "default": defaults.get(p["name"].lower(), {}).get("default")}
                  for p in m.get("parameters", [])]
        if not params and defaults:   # catalog without sys.parameters (older extract / fixture)
            params = [{"name": k, "type": "", "output": v["output"], "default": v["default"]} for k, v in defaults.items()]
        modules.append({
            "key": m["key"], "display": m["display"], "type": MODULE_TYPES[m["type"]],
            "type_code": m["type"], "encrypted": bool(m.get("encrypted")),
            "dynamic_sql": bool(an and an.dynamic_sql), "dynamic_lines": sorted(set(an.dynamic_lines)) if an else [],
            "calls": [{"target": t, "lines": sorted(set(ls))} for t, ls in sorted(calls[m["key"]].items())],
            "unresolved": sorted({n for n, _ in an.unresolved}) if an else [],
            "external": sorted({n for n, _, _ in an.external}) if an else [],
            "temp_tables": sorted(an.temp_tables) if an else [],
            "parent": m.get("parent_key"), "events": m.get("events", []),
            "instead_of": m.get("instead_of", False), "disabled": m.get("disabled", False),
            "created": m.get("created"), "modified": m.get("modified"),
            "parameters": params,
            "metrics": metrics(m.get("definition")) if m.get("definition") else {},
            "errors": error_catalog(m.get("definition"), user_messages) if m.get("definition") else [],
        })

    doc = {
        "meta": {**catalog.data["meta"], "analyzed_at": datetime.now().isoformat(timespec="seconds")},
        "tables": tables,
        "modules": modules,
        "edges": edges,
        "key_groups": sorted(key_groups, key=lambda g: (-g["tables"], g["logical"])),
        "usage": usage_rows,
        "errors": errors,
    }
    doc["jobs"], doc["meta"]["jobs_skipped"] = analyze_jobs(catalog, analyzer, doc)
    doc["meta"]["jobs_error"] = catalog.data.get("jobs_error")
    _add_test_planning(doc, cfg, latest)
    return doc


def _add_test_planning(doc, cfg, latest_modified):
    """Per-table test considerations and per-module risk scores."""
    names = {t["key"]: t["display"] for t in doc["tables"]} | {m["key"]: m["display"] for m in doc["modules"]}
    writers = defaultdict(set)
    for u in doc["usage"]:
        if u["op"] in WRITE_OPS:
            writers[u["object"]].add(u["module"])
    triggers = defaultdict(list)
    for m in doc["modules"]:
        if m["type_code"] == "TR" and m["parent"]:
            triggers[m["parent"]].append(m)
    callers = defaultdict(set)
    for m in doc["modules"]:
        for c in m["calls"]:
            callers[c["target"]].add(m["key"])

    for t in doc["tables"]:
        notes = []
        if t["kind"] == "table" and not t["pk"]:
            notes.append("No primary key: duplicate rows are possible. Check how the application identifies a row.")
        for ck in t["checks"]:
            notes.append(f"CHECK {ck['definition']}{' (disabled!)' if ck['disabled'] else ''}: "
                         "test that values violating it are rejected.")
        mandatory = [c["name"] for c in t["columns"] if not c["nullable"] and not c["identity"]
                     and "default" not in c and "computed" not in c]
        if mandatory:
            notes.append("Mandatory (NOT NULL, no default): " + ", ".join(mandatory) + ". Test missing values.")
        defaults = [f"{c['name']} = {c['default']}" for c in t["columns"] if "default" in c]
        if defaults:
            notes.append("Defaults: " + "; ".join(defaults) + ". Verify the value when the field is omitted.")
        for u in t["unique"]:
            notes.append(f"Unique on ({', '.join(u)}): test duplicate entries.")
        child_cols = {e[side]["column"] for e in doc["edges"] for side, other in (("a", "b"), ("b", "a"))
                      if e[side]["table"] == t["key"] and e["parent"] == other and e["confidence"] >= 50}
        for c in t["columns"]:
            p = c.get("profile") or {}
            vals = p.get("values")
            base = c["type"].split("(")[0]
            code_like = (base in ("char", "varchar", "nchar", "nvarchar", "bit", "tinyint")
                         and c["name"].lower() not in child_cols and not c["pk"])
            if vals and code_like and 2 <= len(vals) <= 12 and not c.get("sensitive"):
                total = sum(n for _, n in vals) or 1
                shown = ", ".join(f"{v if v is not None else 'NULL'} ({100 * n / total:.0f}%)" for v, n in vals)
                notes.append(f"{c['name']} ({c['logical']}) takes values {shown}: cover each value; rare ones are easily missed.")
            if p.get("negatives"):
                k = p["negatives"]
                notes.append(f"{c['name']} has negative values in the sample ({k} row{'s' if k != 1 else ''}): "
                             "confirm they are valid" + (" (a CHECK constraint exists, so it may be disabled or NOCHECK)."
                                                          if c.get("check") else "."))
            if p.get("blank") and not c["nullable"]:
                notes.append(f"{c['name']} is NOT NULL but has {p['blank']} blank values: the application may treat blank as missing.")
        for e in doc["edges"]:
            if e["confidence"] < 50 or not e["parent"]:
                continue
            child = e["b"] if e["parent"] == "a" else e["a"]
            parent = e["a"] if e["parent"] == "a" else e["b"]
            if child["table"] != t["key"]:
                continue
            col = next((c for c in t["columns"] if c["name"].lower() == child["column"]), None)
            nullp = ((col or {}).get("profile") or {}).get("null_pct")
            if col and col["nullable"] and nullp:
                notes.append(f"{col['name']} references {names[parent['table']]}.{parent['column']} but is NULL in "
                             f"{nullp}% of rows: test screens/reports with a missing {col['logical']}.")
            d = e["sources"].get("data")
            if d and not d.get("error") and d.get("sampled") and d["orphan_pct"] > 0:
                notes.append(f"{d['orphan_pct']}% of sampled {child['column']} values have no match in "
                             f"{names[parent['table']]}.{parent['column']}: orphan data exists; test how it is shown.")
        for tr in triggers[t["key"]]:
            notes.append(f"Trigger {tr['display']} ({'INSTEAD OF' if tr['instead_of'] else 'AFTER'} "
                         f"{', '.join(tr['events'])}): verify its side effects in every test that changes this table.")
        w = writers[t["key"]]
        if len(w) >= 3:
            notes.append(f"Written by {len(w)} objects ({', '.join(sorted(names.get(x, x) for x in w))}): "
                         "a regression hotspot; also test concurrent updates.")
        sens = sorted({f"{c['name']} ({c['sensitive']})" for c in t["columns"] if c.get("sensitive")})
        if sens:
            notes.append("Sensitive data: " + ", ".join(sens) + ". Use masked/synthetic test data and check it never "
                         "appears in logs, exports or error messages.")
        t["test_notes"] = notes

    run_by = defaultdict(list)          # module -> [(job, step)]
    for j in doc.get("jobs", []):
        sched = "; ".join(s["text"] for s in j["schedules"] if s["enabled"]) or "no active schedule"
        for st in j["steps"]:
            for k in st["calls"]:
                run_by[k].append({"job": j["name"], "step": st["id"], "schedule": sched, "how": "direct"})
        for k in set(j["procedures"]) - set(j["direct_procedures"]):
            run_by[k].append({"job": j["name"], "step": None, "schedule": sched, "how": "through the call chain"})
        for k, via in j["writes"].items():
            if k in {t["key"] for t in doc["tables"]}:
                t = next(t for t in doc["tables"] if t["key"] == k)
                sched = "; ".join(s["text"] for s in j["schedules"] if s["enabled"]) or "no active schedule"
                t.setdefault("jobs", []).append({"job": j["name"], "schedule": sched,
                                                 "via": [names.get(v, v) for v in via]})
                t["test_notes"].append(f"Changed by scheduled job '{j['name']}' ({sched}) via "
                                       f"{', '.join(names.get(v, v) for v in via)}: include batch runs in tests, "
                                       "and test screens/reports while the job is running.")
    tables_with_triggers = set(triggers)
    for m in doc["modules"]:
        mt = m["metrics"]
        written = {u["object"] for u in doc["usage"] if u["module"] == m["key"] and u["op"] in WRITE_OPS}
        m["writes_tables"] = sorted(written)
        m["fires_triggers"] = sorted(tr["key"] for k in written & tables_with_triggers for tr in triggers[k])
        m["run_by_jobs"] = run_by.get(m["key"], [])
        recent = bool(latest_modified and m.get("modified") and m["modified"][:10] >= _days_before(latest_modified, 90))
        score = (mt.get("complexity", 1) + 4 * len(written) + (8 if m["dynamic_sql"] else 0)
                 + 3 * mt.get("cursors", 0) + 2 * len(callers[m["key"]]) + (10 if recent else 0)
                 + (6 if m["encrypted"] else 0) + (0 if mt.get("try_catch") or not written else 3))
        reasons = []
        if mt.get("complexity", 0) >= 10:
            reasons.append(f"complexity {mt['complexity']}")
        if len(written) >= 2:
            reasons.append(f"writes {len(written)} tables")
        if m["dynamic_sql"]:
            reasons.append("dynamic SQL")
        if mt.get("cursors"):
            reasons.append("cursor")
        if recent:
            reasons.append("recently modified")
        if written and not mt.get("try_catch"):
            reasons.append("no TRY/CATCH")
        if m["fires_triggers"]:
            reasons.append("fires triggers")
        if m["encrypted"]:
            reasons.append("encrypted")
        if m["run_by_jobs"]:
            score += 5
            reasons.append("runs in a scheduled job")
        if any(e.get("warning") for e in m.get("errors", [])):
            score += 3
            reasons.append("suspicious error severity")
        m["risk"] = {"score": score, "reasons": reasons, "recently_modified": recent}


def _days_before(iso, days):
    from datetime import timedelta
    try:
        return (datetime.fromisoformat(iso[:19]) - timedelta(days=days)).isoformat()[:10]
    except ValueError:
        return ""


def _type_str(c):
    t = c["type"]
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if c['max_length'] == -1 else c['max_length']})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if c['max_length'] == -1 else c['max_length'] // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({c['precision']},{c['scale']})"
    return t


def _strip(name, prefix):
    n = name.lower()
    return n[len(prefix):].lstrip("_") if prefix and n.startswith(prefix) and len(n) > len(prefix) + 1 else n


# ---------------------------------------------------------------- impact

def impact(doc, table_key, column=None, min_confidence=40):
    """Everything affected by changing a table (optionally one column)."""
    by_module = {m["key"]: m for m in doc["modules"]}
    usage_by_obj = defaultdict(list)
    for u in doc["usage"]:
        usage_by_obj[u["object"]].append(u)
    callers = defaultdict(set)
    for m in doc["modules"]:
        for c in m["calls"]:
            callers[c["target"]].add(m["key"])

    col = column.lower() if column else None

    def touches(u):
        if not col:
            return True
        if col in u["cols_read"] or col in u["cols_written"]:
            return True
        # Column unknown: SELECT *, INSERT without column list, catalog-only references.
        return bool({"SELECT *", "all columns"} & set(u["details"])) or u["source"] == "catalog"

    direct = [dict(u, via="direct") for u in usage_by_obj[table_key] if touches(u)]

    # Through views (and views of views).
    via_view = []
    seen = {table_key}
    frontier = [(table_key, [])]
    while frontier:
        obj, path = frontier.pop()
        for u in usage_by_obj[obj]:
            mod = by_module.get(u["module"])
            if obj != table_key:
                via_view.append(dict(u, via=" -> ".join(path)))
            if mod and mod["type_code"] in ("V", "IF", "TF") and u["module"] not in seen:
                if obj == table_key and not touches(u):
                    continue
                seen.add(u["module"])
                frontier.append((u["module"], path + [mod["display"]]))

    # Callers of anything affected (transitively).
    affected = {u["module"] for u in direct + via_view}
    chain = []
    frontier = list(affected)
    visited = set(affected)
    while frontier:
        callee = frontier.pop()
        for caller in sorted(callers.get(callee, ())):
            chain.append({"caller": caller, "calls": callee})
            if caller not in visited:
                visited.add(caller)
                frontier.append(caller)

    triggers = [m for m in doc["modules"] if m["type_code"] == "TR" and m["parent"] == table_key]
    related = []
    for e in doc["edges"]:
        if e["confidence"] < min_confidence:
            continue
        for me, other in (("a", "b"), ("b", "a")):
            if e[me]["table"] == table_key and (not col or e[me]["column"] == col):
                related.append({"column": e[me]["column"], "other_table": e[other]["table"],
                                "other_column": e[other]["column"], "confidence": e["confidence"],
                                "role": "is referenced by" if e["parent"] == me
                                else ("references" if e["parent"] == other else "shares key with"),
                                "evidence": e["evidence"]})
    everything = {u["module"] for u in direct + via_view} | {c["caller"] for c in chain}
    jobs = []
    for j in doc.get("jobs", []):
        hit = sorted(set(j["procedures"]) & everything)
        step_hit = any(r["object"] == table_key for st in j["steps"] for r in st["refs"])
        if hit or step_hit:
            jobs.append({"job": j["name"], "schedules": [s["text"] for s in j["schedules"] if s["enabled"]],
                         "via": hit or ["job step SQL"], "enabled": j["enabled"]})
    return {"table": table_key, "column": column, "direct": direct, "via_view": via_view,
            "callers": chain, "triggers": triggers, "related": related, "jobs": jobs}
