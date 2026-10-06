"""Knowledge base + AI context export: one Markdown file per table / procedure, prompt templates, and a
JSON bundle. Hand-written 'Business notes' sections are preserved when the files are regenerated.
Sensitive column values are never written - only their category and format."""
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

NOTES_START = "<!-- NOTES:START - edit freely, kept on regeneration -->"
NOTES_END = "<!-- NOTES:END -->"
TABLE_NOTES_HINT = ("_What does this table hold, in business terms? Meaning of codes/statuses, who creates the rows "
                    "(screen, batch, interface), known data issues, related test cases._")
MODULE_NOTES_HINT = ("_Business purpose, which screen/batch calls it, when it runs, expected outcome, known issues, "
                     "related test cases._")
GLOSSARY_HINT = ("_Domain terms and where they live, e.g. **Settlement date (T+1)**: dbo.trade.trddate + 1 business "
                 "day; **Contract note**: ..._")
OPS = ["INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "SELECT", "REFERENCED"]


def _fname(display):
    return re.sub(r'[<>:"/\\|?*]', "_", display) + ".md"


def _cell(s):
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def read_notes(path):
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    m = re.search(re.escape(NOTES_START) + r"\n(.*?)\n?" + re.escape(NOTES_END), text, re.S)
    return m.group(1).strip() if m else None


def _notes_block(existing, hint):
    body = existing if existing else hint
    return f"{NOTES_START}\n{body}\n{NOTES_END}"


def _has_notes(text, hint):
    return bool(text) and text.strip() != hint


def collect_notes(kb_dir):
    """{object key: notes text} for every table / module file with real (non-placeholder) notes."""
    kb = Path(kb_dir)
    out = {}
    for sub in ("tables", "procedures", "objects"):
        for f in (kb / sub).glob("*.md") if (kb / sub).exists() else []:
            n = read_notes(f)
            if n and n not in (TABLE_NOTES_HINT, MODULE_NOTES_HINT):
                out[f.stem.lower()] = n
    return out


def _profile_text(c):
    p = c.get("profile") or {}
    if not p:
        return ""
    bits = []
    if p.get("null_pct"):
        bits.append(f"{p['null_pct']}% null")
    if p.get("distinct") is not None:
        bits.append(f"{p['distinct']} distinct")
    if c.get("sensitive"):
        if p.get("formats"):
            bits.append("format " + " / ".join(f"`{s}`" for s, _ in p["formats"][:2]))
        return " · ".join(bits)
    if p.get("values"):
        total = sum(n for _, n in p["values"]) or 1
        bits.append("values: " + ", ".join(f"`{'NULL' if v is None else v}` {100 * n / total:.0f}%" for v, n in p["values"][:10])
                    + (" …" if len(p["values"]) > 10 else ""))
    elif "min" in p:
        bits.append(f"range {p['min']} … {p['max']}")
    elif "max_len" in p:
        bits.append(f"length {p.get('min_len')}–{p['max_len']}")
        if p.get("formats"):
            bits.append("format " + " / ".join(f"`{s}`" for s, _ in p["formats"][:2]))
    if p.get("examples"):
        bits.append("e.g. " + ", ".join(f"`{e}`" for e in p["examples"]))
    return " · ".join(bits)


def _rules_text(c):
    bits = []
    if c.get("check"):
        bits.append(f"CHECK `{c['check']}`")
    if c.get("default"):
        bits.append(f"default `{c['default']}`")
    if c.get("computed"):
        bits.append(f"computed `{c['computed']}`")
    return "; ".join(bits)


class Exporter:
    def __init__(self, doc, cfg, catalog=None):
        self.doc, self.cfg = doc, cfg
        self.defs = {o["key"]: o.get("definition") for o in catalog.objects.values()} if catalog else {}
        self.T = {t["key"]: t for t in doc["tables"]}
        self.M = {m["key"]: m for m in doc["modules"]}
        self.names = {k: v["display"] for k, v in self.T.items()} | {k: v["display"] for k, v in self.M.items()}
        self.usage_by_obj = defaultdict(list)
        self.usage_by_mod = defaultdict(list)
        for u in doc["usage"]:
            self.usage_by_obj[u["object"]].append(u)
            self.usage_by_mod[u["module"]].append(u)
        self.callers = defaultdict(set)
        for m in doc["modules"]:
            for c in m["calls"]:
                self.callers[c["target"]].add(m["key"])
        self.stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    def n(self, k):
        return self.names.get(k, k)

    def relations(self, key, min_conf=40):
        out = {"references": [], "referenced_by": [], "shares_key": []}
        for e in self.doc["edges"]:
            if e["confidence"] < min_conf:
                continue
            for me, other in (("a", "b"), ("b", "a")):
                if e[me]["table"] != key:
                    continue
                item = {"column": e[me]["column"], "table": self.n(e[other]["table"]), "table_key": e[other]["table"],
                        "other_column": e[other]["column"], "confidence": e["confidence"], "evidence": e["evidence"]}
                if e["parent"] == other:
                    out["references"].append(item)
                elif e["parent"] == me:
                    out["referenced_by"].append(item)
                else:
                    out["shares_key"].append(item)
        return out

    # ------------------------------------------------------------------ tables
    def table_md(self, t, notes):
        L = [f"# {t['display']}" + (" (view)" if t["kind"] == "view" else ""), "",
             f"> Generated {self.stamp} from {self.doc['meta'].get('database', '')}. Everything except the "
             "Business notes section is regenerated.", ""]
        facts = []
        if t.get("rows") is not None:
            facts.append(f"**Rows:** {t['rows']:,}")
        facts.append(f"**Column prefix:** `{t['prefix'] or '-'}`")
        facts.append(f"**Primary key:** {', '.join(t['pk']) if t['pk'] else '**none**'}")
        if t.get("modified"):
            facts.append(f"**Schema last changed:** {t['modified'][:10]}")
        L += [" · ".join(facts), "", "## Business notes", _notes_block(notes, TABLE_NOTES_HINT), ""]

        L += ["## Columns", "", "| Column | Meaning | Type | Null | Key | Rules | Data profile |",
              "|---|---|---|---|---|---|---|"]
        rel = self.relations(t["key"])
        fk_cols = {r["column"]: r for r in rel["references"]}
        for c in t["columns"]:
            key = []
            if c["pk"]:
                key.append("PK")
            if c["identity"]:
                key.append("identity")
            low = c["name"].lower()
            if low in fk_cols:
                key.append(f"→ {fk_cols[low]['table']}")
            meaning = c["logical"] + (f" 🔒 {c['sensitive']}" if c.get("sensitive") else "")
            L.append(f"| {_cell(c['name'])} | {_cell(meaning)} | {_cell(c['type'])} | {'yes' if c['nullable'] else 'no'} "
                     f"| {_cell(', '.join(key))} | {_cell(_rules_text(c))} | {_cell(_profile_text(c))} |")
        if t.get("profiled", {}).get("sampled_rows") is not None:
            pr = t["profiled"]
            L += ["", f"_Profile from {'all' if pr.get('full_scan') else 'a sample of'} {pr['sampled_rows']:,} rows "
                      f"({pr.get('profiled_at', '')[:10]}). Distinct counts are within the sample._"]
        L.append("")

        L += ["## Relationships", ""]
        if not any(rel.values()):
            L.append("_None found._")
        for label, k in (("References (this table → parent)", "references"),
                         ("Referenced by (child → this table)", "referenced_by"),
                         ("Shares a key with (direction unknown)", "shares_key")):
            if rel[k]:
                L.append(f"**{label}**")
                L.append("")
                for r in rel[k]:
                    L.append(f"- `{r['column']}` ↔ [{r['table']}]({_fname(r['table'])}).`{r['other_column']}` "
                             f"— confidence {r['confidence']} ({'; '.join(r['evidence'])})")
                L.append("")
        L.append("")

        L += ["## Used by", ""]
        uses = sorted(self.usage_by_obj[t["key"]], key=lambda u: (OPS.index(u["op"]), self.n(u["module"])))
        if uses:
            L += ["| Object | Type | Operation | Columns written | Columns read |", "|---|---|---|---|---|"]
            for u in uses:
                m = self.M.get(u["module"], {})
                folder = "../procedures" if m.get("type_code") == "P" else "../objects"
                L.append(f"| [{self.n(u['module'])}]({folder}/{_fname(self.n(u['module']))}) | {m.get('type', '')} "
                         f"| {u['op']}{' (dynamic SQL)' if u['dynamic'] else ''} | {_cell(', '.join(u['cols_written']) or '; '.join(u['details']))} "
                         f"| {_cell(', '.join(u['cols_read']))} |")
        else:
            L.append("_Not referenced by any procedure, view, trigger or function (may be used directly by the application)._")
        L.append("")

        trig = [m for m in self.doc["modules"] if m["type_code"] == "TR" and m["parent"] == t["key"]]
        if trig:
            L += ["## Triggers", ""]
            for m in trig:
                L.append(f"- [{m['display']}](../objects/{_fname(m['display'])}): "
                         f"{'INSTEAD OF' if m['instead_of'] else 'AFTER'} {', '.join(m['events'])}"
                         f"{' (disabled)' if m['disabled'] else ''}")
            L.append("")

        if t.get("test_notes"):
            L += ["## Test considerations", ""] + [f"- {x}" for x in t["test_notes"]] + [""]

        if rel["references"]:
            L += ["## Handy queries", "", "```sql", f"-- {t['display']} with its parent tables"]
            alias = "t"
            joins, cols = [], [f"{alias}.*"]
            for i, r in enumerate(rel["references"][:6], 1):
                pa = f"p{i}"
                joins.append(f"LEFT JOIN {r['table']} {pa} ON {pa}.{r['other_column']} = {alias}.{r['column']}")
                cols.append(f"{pa}.{r['other_column']} AS {r['column']}_found")
            L += [f"SELECT TOP 50 {', '.join(cols)}", f"FROM {t['display']} {alias} WITH (NOLOCK)"] + joins
            L += ["", "-- Orphans: child values with no parent row"]
            for r in rel["references"][:6]:
                L.append(f"SELECT COUNT(*) AS orphans_{r['column']} FROM {t['display']} c WITH (NOLOCK) "
                         f"WHERE c.{r['column']} IS NOT NULL AND NOT EXISTS "
                         f"(SELECT 1 FROM {r['table']} p WHERE p.{r['other_column']} = c.{r['column']});")
            L += ["```", ""]
        return "\n".join(L)

    # ------------------------------------------------------------------ modules
    def module_md(self, m, notes):
        L = [f"# {m['display']} ({m['type']})", "",
             f"> Generated {self.stamp}. Everything except the Business notes section is regenerated.", ""]
        risk = m.get("risk", {})
        facts = [f"**Risk score:** {risk.get('score', '-')}" + (f" ({', '.join(risk['reasons'])})" if risk.get("reasons") else "")]
        if m.get("modified"):
            facts.append(f"**Last modified:** {m['modified'][:10]}")
        if m["type_code"] == "TR":
            facts.append(f"**Fires:** {'INSTEAD OF' if m['instead_of'] else 'AFTER'} {', '.join(m['events'])} on "
                         f"[{self.n(m['parent'])}](../tables/{_fname(self.n(m['parent']))})")
        L += [" · ".join(facts), "", "## Business notes", _notes_block(notes, MODULE_NOTES_HINT), ""]

        if m.get("parameters"):
            L += ["## Parameters", "", "| Name | Type | Default | Output |", "|---|---|---|---|"]
            for p in m["parameters"]:
                L.append(f"| {p['name']} | {_cell(p['type'])} | {_cell(p['default'] if p['default'] is not None else '(required)')} "
                         f"| {'yes' if p['output'] else ''} |")
            L.append("")

        L += ["## Data operations", ""]
        uses = sorted(self.usage_by_mod[m["key"]], key=lambda u: (min(u["lines"]) if u["lines"] else 10 ** 6))
        if uses:
            L += ["| Line | Operation | Table/view | Columns written | Columns read |", "|---|---|---|---|---|"]
            for u in uses:
                L.append(f"| {', '.join(map(str, u['lines']))} | {u['op']}{' (dynamic)' if u['dynamic'] else ''} "
                         f"| [{self.n(u['object'])}](../tables/{_fname(self.n(u['object']))}) "
                         f"| {_cell(', '.join(u['cols_written']) or '; '.join(u['details']))} | {_cell(', '.join(u['cols_read']))} |")
        else:
            L.append("_None found._")
        L.append("")

        if m.get("run_by_jobs"):
            L += ["## Run by scheduled jobs", ""]
            for r in m["run_by_jobs"]:
                L.append(f"- [{r['job']}](../jobs/{_fname(r['job'])}) "
                         f"({'step ' + str(r['step']) if r['step'] else r['how']}): {r['schedule']}")
            L.append("")

        if m.get("errors"):
            L += ["## Errors raised (expected results for negative tests)", "",
                  "| Line | Kind | Number | Severity | Message | Raised when |", "|---|---|---|---|---|---|"]
            for e in m["errors"]:
                when = "inside CATCH" if e["in_catch"] else (e["condition"] or "always / unconditional")
                msg = e["message"] + (f" ⚠ {e['warning']}" if e.get("warning") else "")
                L.append(f"| {e['line']} | {e['kind']} | {_cell(e['number'])} | {_cell(e['severity'])} "
                         f"| {_cell(msg)} | `{_cell(when)}` |")
            L.append("")

        if m["calls"] or self.callers[m["key"]] or m.get("fires_triggers"):
            L += ["## Call chain", ""]
            for c in m["calls"]:
                L.append(f"- Calls **{self.n(c['target'])}**" + (f" (line {', '.join(map(str, c['lines']))})" if c["lines"] else ""))
            for c in sorted(self.callers[m["key"]]):
                L.append(f"- Called by **{self.n(c)}**")
            for tr in m.get("fires_triggers", []):
                L.append(f"- Its writes fire trigger **{self.n(tr)}**")
            L.append("")

        mt = m.get("metrics") or {}
        if mt:
            L += ["## Structure", "",
                  f"{mt['lines']} code lines · {mt['statements']} DML statements ({mt['writes']} writes) · "
                  f"branches: {mt['branches']} · loops: {mt['loops']} · cursors: {mt['cursors']} · "
                  f"explicit transactions: {mt['transactions']} · TRY/CATCH: {'yes' if mt['try_catch'] else 'no'} · "
                  f"raises errors: {mt['raises_errors']} · temp tables: {mt['temp_tables']}", ""]

        flags = []
        if m["encrypted"]:
            flags.append("Encrypted: source not readable; table usage comes from SQL Server dependency metadata only.")
        if m["dynamic_sql"]:
            flags.append(f"Dynamic SQL at line(s) {', '.join(map(str, m['dynamic_lines']))}: tables chosen at runtime are not listed above.")
        if m["unresolved"]:
            flags.append("Names not found in this database: " + ", ".join(m["unresolved"]))
        if m["external"]:
            flags.append("Cross-database references: " + ", ".join(m["external"]))
        if flags:
            L += ["## Review flags", ""] + [f"- {f}" for f in flags] + [""]

        src = self.defs.get(m["key"])
        if self.cfg.include_source and src:
            lines = src.strip("\n").splitlines()
            cut = len(lines) > self.cfg.max_source_lines
            L += ["## Source", "", "```sql"] + lines[: self.cfg.max_source_lines] + ["```"]
            if cut:
                L.append(f"_Truncated: showing {self.cfg.max_source_lines} of {len(lines)} lines._")
            L.append("")
        return "\n".join(L)

    # ------------------------------------------------------------------ jobs
    def job_md(self, j, notes):
        sched = "; ".join(s["text"] + ("" if s["enabled"] else " (disabled)") for s in j["schedules"]) or "no schedule"
        h = j["history"]
        L = [f"# Job: {j['name']}", "",
             f"> Generated {self.stamp}. Everything except the Business notes section is regenerated.", "",
             f"**Schedule:** {sched} · **Enabled:** {'yes' if j['enabled'] else '**no**'} · **Owner:** {j.get('owner') or ''}",
             "",
             f"**Last 30 days:** {h.get('runs_30d') if h.get('runs_30d') is not None else '-'} runs, "
             f"{h.get('failures_30d') if h.get('failures_30d') is not None else '-'} failed · avg {h.get('avg') or '-'} · "
             f"max {h.get('max') or '-'} · last {h.get('last_status') or '-'} {h.get('last_run') or ''}", "",
             "## Business notes",
             _notes_block(notes, "_What business process is this (EOD, settlement, interface)? Upstream/downstream "
                                 "dependencies, expected run time, who to call when it fails, how to rerun safely._"), ""]
        if j["warnings"]:
            L += ["## Test / risk notes", ""] + [f"- {w}" for w in j["warnings"]] + [""]
        L += ["## Steps", ""]
        for st in j["steps"]:
            L += [f"### {st['id']}. {st['name']} ({st['subsystem']}, database {st['database'] or '-'})", "",
                  f"On success: {st['on_success']} · On failure: {st['on_fail']}"
                  + (f" · retries {st['retries']}" if st["retries"] else ""), ""]
            if st["calls"]:
                L.append("Runs: " + ", ".join(f"[{self.n(c)}](../procedures/{_fname(self.n(c))})" for c in st["calls"]))
                L.append("")
            L += ["```sql" if st["subsystem"].upper() == "TSQL" else "```", st["command"].strip(), "```", ""]
        if j["procedures"]:
            L += ["## Procedures executed (including nested calls)", ""]
            L += [f"- [{self.n(p)}](../procedures/{_fname(self.n(p))})" for p in j["procedures"]] + [""]
        if j["writes"]:
            L += ["## Tables changed", "", "| Table | Changed by |", "|---|---|"]
            for k, via in j["writes"].items():
                L.append(f"| [{self.n(k)}](../tables/{_fname(self.n(k))}) | {_cell(', '.join(self.n(v) for v in via))} |")
            L.append("")
        if j["fires_triggers"]:
            L += ["Triggers fired: " + ", ".join(self.n(t) for t in j["fires_triggers"]), ""]
        if j["reads"]:
            L += ["## Tables read", "", ", ".join(self.n(k) for k in j["reads"]), ""]
        return "\n".join(L)

    # ------------------------------------------------------------------ index, prompts, bundle
    def readme_md(self, glossary, notes):
        d = self.doc
        tables = [t for t in d["tables"] if t["kind"] == "table"]
        procs = sorted([m for m in d["modules"] if m["type_code"] == "P"], key=lambda m: -m.get("risk", {}).get("score", 0))
        L = [f"# {d['meta'].get('database', 'Database')}: knowledge base", "",
             f"> Generated {self.stamp} from server {d['meta'].get('server', '')}. Business notes sections in every file "
             "are kept when this is regenerated, so write domain knowledge there.", "",
             f"{len(tables)} tables · {sum(1 for t in d['tables'] if t['kind'] == 'view')} views · {len(procs)} procedures · "
             f"{sum(1 for m in d['modules'] if m['type_code'] == 'TR')} triggers · "
             f"{sum(1 for e in d['edges'] if e['confidence'] >= 70)} high-confidence relationships", "",
             "## Glossary", _notes_block(glossary, GLOSSARY_HINT), "",
             "## How to use with AI", "",
             "Attach the relevant `tables/*.md` and `procedures/*.md` files (or `context.json`) to the chat and use a "
             "template from `prompts/`. Sensitive columns show only their category and format, never real values. "
             "Prefer a local or company-approved model, since the files still describe your schema and business logic.", "",
             ]
        if d.get("jobs"):
            L += ["## Batch jobs (by start time)", "", "| Job | Schedule | Writes | Risk notes |", "|---|---|---|---|"]
            for j in d["jobs"]:
                sched = "; ".join(s["text"] for s in j["schedules"] if s["enabled"]) or "no active schedule"
                L.append(f"| [{j['name']}](jobs/{_fname(j['name'])}){'' if j['enabled'] else ' (disabled)'} | {_cell(sched)} "
                         f"| {_cell(', '.join(self.n(k) for k in j['writes']))} | {len(j['warnings'])} |")
            L.append("")
        elif d["meta"].get("jobs_error"):
            L += ["## Batch jobs", "", f"_SQL Agent jobs could not be read ({d['meta']['jobs_error']}). "
                  "Ask for the SQLAgentReaderRole role in msdb._", ""]
        L += ["## Procedures by test risk", "", "| Procedure | Risk | Why | Writes |", "|---|---|---|---|"]
        for m in procs:
            r = m.get("risk", {})
            L.append(f"| [{m['display']}](procedures/{_fname(m['display'])}) | {r.get('score', '')} | "
                     f"{_cell(', '.join(r.get('reasons', [])))} | {_cell(', '.join(self.n(k) for k in m.get('writes_tables', [])))} |")
        L += ["", "## Tables", "", "| Table | Rows | Purpose | Written by | Read by | Sensitive |", "|---|---|---|---|---|---|"]
        for t in tables:
            uses = self.usage_by_obj[t["key"]]
            w = {u["module"] for u in uses if u["op"] not in ("SELECT", "REFERENCED")}
            r = {u["module"] for u in uses if u["op"] in ("SELECT", "REFERENCED")}
            purpose = (notes.get(t["display"].lower()) or "").strip().splitlines()
            sens = sorted({c["sensitive"] for c in t["columns"] if c.get("sensitive")})
            L.append(f"| [{t['display']}](tables/{_fname(t['display'])}) | {t['rows'] if t['rows'] is not None else ''} | "
                     f"{_cell(purpose[0][:120] if purpose else '')} | {len(w)} | {len(r)} | {_cell(', '.join(sens))} |")
        others = [m for m in d["modules"] if m["type_code"] != "P"]
        if others:
            L += ["", "## Views, triggers and functions", ""]
            for m in sorted(others, key=lambda m: (m["type"], m["display"])):
                L.append(f"- [{m['display']}](objects/{_fname(m['display'])}) ({m['type']})")
        L.append("")
        return "\n".join(L)

    def bundle(self, notes):
        """Compact, masked JSON for AI tools / retrieval."""
        tables = []
        for t in self.doc["tables"]:
            cols = []
            for c in t["columns"]:
                p = dict(c.get("profile") or {})
                if c.get("sensitive"):
                    p = {k: v for k, v in p.items() if k in ("null_pct", "distinct", "formats", "min_len", "max_len")}
                cols.append({k: v for k, v in {"name": c["name"], "meaning": c["logical"], "type": c["type"],
                                               "nullable": c["nullable"], "pk": c["pk"] or None,
                                               "check": c.get("check"), "default": c.get("default"),
                                               "sensitive": c.get("sensitive"), "profile": p or None}.items() if v is not None})
            uses = self.usage_by_obj[t["key"]]
            tables.append({"name": t["display"], "kind": t["kind"], "rows": t["rows"], "notes": notes.get(t["display"].lower()),
                           "primary_key": t["pk"], "columns": cols, "relationships": {
                               k: [{"column": r["column"], "table": r["table"], "other_column": r["other_column"],
                                    "confidence": r["confidence"]} for r in v] for k, v in self.relations(t["key"]).items()},
                           "used_by": [{"object": self.n(u["module"]), "op": u["op"], "writes": u["cols_written"]} for u in uses],
                           "test_considerations": t.get("test_notes", [])})
        modules = []
        for m in self.doc["modules"]:
            modules.append({"name": m["display"], "type": m["type"], "notes": notes.get(m["display"].lower()),
                            "parameters": m.get("parameters", []), "risk": m.get("risk"),
                            "operations": [{"op": u["op"], "table": self.n(u["object"]), "writes": u["cols_written"],
                                            "reads": u["cols_read"], "lines": u["lines"]} for u in self.usage_by_mod[m["key"]]],
                            "calls": [self.n(c["target"]) for c in m["calls"]],
                            "called_by": sorted(self.n(c) for c in self.callers[m["key"]]),
                            "fires_triggers": [self.n(x) for x in m.get("fires_triggers", [])],
                            "dynamic_sql": m["dynamic_sql"],
                            "errors": [{k: e.get(k) for k in ("line", "kind", "number", "severity", "message",
                                                              "condition", "in_catch", "warning")} for e in m.get("errors", [])],
                            "run_by_jobs": m.get("run_by_jobs", []),
                            "source": self.defs.get(m["key"]) if self.cfg.include_source else None})
        return {"database": self.doc["meta"].get("database"), "generated": self.stamp,
                "note": "Sensitive columns carry only category and format; no real values.",
                "tables": tables, "procedures_and_objects": modules,
                "jobs": [{"name": j["name"], "enabled": j["enabled"], "notes": notes.get("job:" + j["name"].lower()),
                          "schedules": [s["text"] for s in j["schedules"] if s["enabled"]],
                          "steps": [{"id": s["id"], "name": s["name"], "subsystem": s["subsystem"],
                                     "runs": [self.n(c) for c in s["calls"]], "on_failure": s["on_fail"]} for s in j["steps"]],
                          "procedures": [self.n(p) for p in j["procedures"]],
                          "writes": [self.n(k) for k in j["writes"]], "warnings": j["warnings"], "history": j["history"]}
                         for j in self.doc.get("jobs", [])]}

    def write(self, out_dir):
        out = Path(out_dir)
        for sub in ("tables", "procedures", "objects", "prompts"):
            (out / sub).mkdir(parents=True, exist_ok=True)
        notes = {}
        written = 0
        for t in self.doc["tables"]:
            f = out / "tables" / _fname(t["display"])
            n = read_notes(f)
            if _has_notes(n, TABLE_NOTES_HINT):
                notes[t["display"].lower()] = n
            f.write_text(self.table_md(t, n if _has_notes(n, TABLE_NOTES_HINT) else None), encoding="utf-8")
            written += 1
        for m in self.doc["modules"]:
            f = out / ("procedures" if m["type_code"] == "P" else "objects") / _fname(m["display"])
            n = read_notes(f)
            if _has_notes(n, MODULE_NOTES_HINT):
                notes[m["display"].lower()] = n
            f.write_text(self.module_md(m, n if _has_notes(n, MODULE_NOTES_HINT) else None), encoding="utf-8")
            written += 1
        (out / "jobs").mkdir(exist_ok=True)
        for j in self.doc.get("jobs", []):
            f = out / "jobs" / _fname(j["name"])
            n = read_notes(f)
            real = n if n and not n.startswith("_What business process") else None
            if real:
                notes["job:" + j["name"].lower()] = real
            f.write_text(self.job_md(j, real), encoding="utf-8")
            written += 1
        g = read_notes(out / "README.md")
        (out / "README.md").write_text(self.readme_md(g if _has_notes(g, GLOSSARY_HINT) else None, notes),
                                       encoding="utf-8")
        for name, text in PROMPTS.items():
            p = out / "prompts" / name
            if not p.exists():          # don't overwrite prompts the team has tuned
                p.write_text(text.strip() + "\n", encoding="utf-8")
        (out / "context.json").write_text(json.dumps(self.bundle(notes), indent=1, default=str), encoding="utf-8")
        return written, len(notes)


PROMPTS = {
    "01-test-cases-for-procedure.md": """
# Generate test cases for a stored procedure

**Attach:** `procedures/<procedure>.md` plus the `tables/*.md` files for every table in its *Data operations*.

---

You are a senior QA engineer testing a stock-broking back office on SQL Server.
Using ONLY the attached context, design test cases for the procedure.

Cover:
1. Every parameter: valid, boundary, NULL/missing (respect defaults), invalid type/format.
2. Every branch (IF/CASE/WHEN, MERGE matched/not matched, @@ROWCOUNT checks).
3. Every table it writes: expected rows inserted/updated/deleted and the exact columns changed.
4. Constraint violations (CHECK, NOT NULL, unique, relationships) and the expected error or rollback.
5. Side effects: triggers fired, procedures called, audit rows.
6. Data states from the profiles: each status/type value listed, NULLs in optional columns, orphan rows.
7. Every entry in *Errors raised*: build the data that makes its "Raised when" condition true and expect exactly
   that message/number. Flag errors with severity <= 10 (the caller never sees them) as defects to confirm.
8. If it is *Run by scheduled jobs*: a test of the batch run itself and of reruns after a failure.

For each test case give: ID, title, preconditions (setup SQL using synthetic data), the EXEC call,
expected result, and verification SQL that checks the database state afterwards.
Never use real client data. For columns marked 🔒, generate synthetic values in the shown format.
List any assumption you had to make separately, so it can be checked with the team.
""",
    "02-test-data-for-table.md": """
# Generate valid test data

**Attach:** `tables/<table>.md` and the files of every table it *References*.

---

Generate INSERT scripts that create a consistent, synthetic data set for the attached tables:
- Insert parent tables first; every child value must exist in its parent (see Relationships).
- Respect NOT NULL, CHECK constraints, defaults, identity columns (do not insert them), data types and lengths.
- Use the value lists in the data profile for code/status columns; include at least one row per value.
- Add edge cases as separate, labelled rows: max lengths, zero/negative quantities where valid, NULLs in nullable columns.
- 🔒 sensitive columns: invent values that match the format only (e.g. `AAAAA9999A` for PAN). Never real data.
- Wrap everything in a transaction, and add a matching cleanup script.
""",
    "03-reconciliation-checks.md": """
# Propose reconciliation / invariant checks

**Attach:** the `tables/*.md` files of a business area (e.g. trade, vaults, ledger, settlement) and the
procedures that write them.

---

From the relationships, columns and procedures provided, propose SQL checks that must always hold
(e.g. holdings = sum of buys - sells per client and stock; ledger total = sum of trade costs; every settled
trade has a settlement row). For each check give: the business rule in one sentence, the SQL returning
violating rows (zero rows = pass), and which procedures could break it. Use NOLOCK and avoid full scans
on very large tables where a date filter is possible.
""",
    "04-explain-procedure.md": """
# Explain a procedure in business terms

**Attach:** `procedures/<procedure>.md` and the related `tables/*.md`.

---

Explain what this procedure does for a tester who is new to the system: business purpose, inputs,
step-by-step data changes (in business words, using column meanings), error handling, and what a tester
should check after running it. Then list open questions to ask a developer or business analyst.
Suggest a short text for its Business notes section.
""",
    "06-batch-job-testing.md": """
# Test a batch job (SQL Agent)

**Attach:** `jobs/<job>.md`, the `procedures/*.md` it executes and the `tables/*.md` it changes.

---

Design tests for this scheduled job in a stock-broking back office:
1. Happy path: input data prepared for one business day, expected rows per table afterwards (verification SQL).
2. Rerun safety: run it twice for the same day. What must NOT be duplicated? Write the check.
3. Failure in each step: what state is left behind, does the job report failure (see on-failure actions),
   and how is it recovered/rerun?
4. Volume and timing: expected duration vs. schedule window; overlap with intraday jobs or users updating
   the same tables.
5. Calendar cases: holidays, month/quarter end, first day after a holiday, partial days.
6. Downstream: which reports, files (SSIS/CmdExec steps) and next jobs depend on its output.
For each test give preconditions, how to trigger the job (msdb.dbo.sp_start_job or the procedure call),
and SQL to verify the outcome. Note any assumption to confirm with operations.
""",
    "05-regression-scope.md": """
# Regression scope for a change

**Attach:** the impact output (`python -m dbexplorer impact <table> --column <col>`, or the Impact tab), the
`tables/*.md` of the changed table, and the `procedures/*.md` of the objects listed in the impact.

---

A change is planned: <describe the change, e.g. "trdcosts becomes decimal(18,4)" or "new status 'P' in trdtype">.

Using the attached impact analysis, list:
1. Objects that must be retested, ordered by risk (risk score, writes before reads), and why each is affected.
2. Specific scenarios per object, focused on the changed column/values.
3. Downstream effects: views, callers, triggers, reports, related tables.
4. Data migration / existing-data checks to run before and after the change.
5. What can safely be left out of the regression run, and why.
""",
}
