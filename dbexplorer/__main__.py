"""
python -m dbexplorer all [--validate] [--profile]   extract + analyze (+ checks) + reports + knowledge base
python -m dbexplorer extract            read the catalog from SQL Server into cache/catalog.json
python -m dbexplorer analyze            build output/analysis.json from the cache
python -m dbexplorer validate [--fresh] sample real data to confirm inferred relationships
python -m dbexplorer profile [TABLE] [--fresh]   profile column data (nulls, values, formats, sensitive data)
python -m dbexplorer report             write output/db_explorer.html and .xlsx
python -m dbexplorer export             write the knowledge base / AI context to knowledge/
python -m dbexplorer impact TABLE [--column COL]   print the impact of changing a table/column
"""
import argparse
import json
import sys
from pathlib import Path

from .config import load_config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dbexplorer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["all", "extract", "analyze", "validate", "profile", "report", "export", "impact"])
    ap.add_argument("table", nargs="?")
    ap.add_argument("--column")
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--catalog", default="cache/catalog.json")
    ap.add_argument("--out", default="output")
    ap.add_argument("--kb", default="knowledge", help="knowledge base folder (keep it under version control)")
    ap.add_argument("--validate", action="store_true", help="with 'all': also run the data check")
    ap.add_argument("--profile", action="store_true", help="with 'all': also profile column data")
    ap.add_argument("--fresh", action="store_true", help="with 'validate'/'profile': discard earlier results")
    ap.add_argument("--min-confidence", type=int, default=40)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    analysis_path = out / "analysis.json"
    validation_path = Path(args.catalog).parent / "validation.json"
    profile_path = Path(args.catalog).parent / "profile.json"
    kb_dir = Path(args.kb)

    def do_extract():
        from .extract import extract
        print(f"Reading catalog from {cfg.connection.get('server')} / {cfg.connection.get('database')} ...")
        _, counts = extract(cfg, args.catalog)
        print("  " + ", ".join(f"{v} {k.lower().replace('_', ' ')}" for k, v in sorted(counts.items())))
        print(f"  saved {args.catalog}")

    def do_analyze():
        from .analysis import run_analysis
        from .model import Catalog
        cat = Catalog.load(args.catalog)
        validation = json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else None
        profile = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else None
        doc = run_analysis(cat, cfg, validation, profile)
        analysis_path.write_text(json.dumps(doc, indent=1, default=sorted), encoding="utf-8")
        e = doc["edges"]
        print(f"Analyzed {len(doc['modules'])} objects: {len(doc['usage'])} table usages, {len(e)} candidate "
              f"relationships ({sum(1 for x in e if x['confidence'] >= 70)} high, "
              f"{sum(1 for x in e if 40 <= x['confidence'] < 70)} medium, {sum(1 for x in e if x['confidence'] < 40)} low)")
        if doc["errors"]:
            print(f"  {len(doc['errors'])} objects could not be analyzed (see Review tab)")
        return doc

    def load_doc():
        if not analysis_path.exists():
            return do_analyze()
        return json.loads(analysis_path.read_text(encoding="utf-8"))

    def do_validate():
        from .validate import run_validation
        if args.fresh and validation_path.exists():
            validation_path.unlink()
        run_validation(load_doc(), cfg, validation_path)
        return do_analyze()

    def do_profile():
        from .model import Catalog
        from .profile import run_profile
        only = {args.table.lower()} if args.table else None
        run_profile(Catalog.load(args.catalog), cfg, profile_path, only=only, fresh=args.fresh)
        return do_analyze()

    def do_export(doc=None):
        from .kb import Exporter
        from .model import Catalog
        doc = doc or load_doc()
        n, with_notes = Exporter(doc, cfg, Catalog.load(args.catalog)).write(kb_dir)
        print(f"Knowledge base: {kb_dir.resolve()} ({n} files, {with_notes} with business notes)")

    def do_report(doc=None):
        from .kb import collect_notes
        from .report_excel import write_excel
        from .report_html import write_html
        doc = doc or load_doc()
        notes = collect_notes(kb_dir)
        for item in doc["tables"] + doc["modules"]:
            if item["display"].lower() in notes:
                item["notes"] = notes[item["display"].lower()]
        write_html(doc, out / "db_explorer.html")
        write_excel(doc, out / "db_explorer.xlsx")
        print(f"Reports: {(out / 'db_explorer.html').resolve()}\n         {(out / 'db_explorer.xlsx').resolve()}")

    if args.command == "extract":
        do_extract()
    elif args.command == "analyze":
        do_analyze()
    elif args.command == "validate":
        do_validate()
    elif args.command == "profile":
        do_profile()
    elif args.command == "report":
        do_report()
    elif args.command == "export":
        do_export()
    elif args.command == "all":
        do_extract()
        doc = do_analyze()
        if args.validate:
            doc = do_validate()
        if args.profile:
            doc = do_profile()
        do_export(doc)
        do_report(doc)
    elif args.command == "impact":
        if not args.table:
            ap.error("impact needs a table name")
        print_impact(load_doc(), args.table, args.column, args.min_confidence)


def print_impact(doc, table, column, min_conf):
    from .analysis import impact
    want = table.lower()
    keys = [t["key"] for t in doc["tables"] if t["key"] == want or t["key"].split(".", 1)[1] == want]
    if not keys:
        sys.exit(f"Table '{table}' not found.")
    names = {t["key"]: t["display"] for t in doc["tables"]} | {m["key"]: m["display"] for m in doc["modules"]}
    mtype = {m["key"]: m["type"] for m in doc["modules"]}
    r = impact(doc, keys[0], column, min_conf)
    title = names[keys[0]] + (f".{column}" if column else "")
    print(f"\nImpact of changing {title}\n" + "=" * (20 + len(title)))

    def show(rows, via=False):
        if not rows:
            print("  (none)")
        for u in sorted(rows, key=lambda u: (u["op"] in ("SELECT", "REFERENCED"), u["op"], names.get(u["module"], ""))):
            cols = ""
            if u["cols_written"]:
                cols += " writes: " + ", ".join(u["cols_written"])
            if u["details"]:
                cols += " [" + "; ".join(u["details"]) + "]"
            extra = f"  via {u['via'] or names.get(u['object'], u['object'])}" if via else ""
            print(f"  {u['op']:<10} {names.get(u['module'], u['module'])} ({mtype.get(u['module'], '')}){extra}{cols}")

    print("\nDirect:")
    show(r["direct"])
    print("\nThrough views / table functions:")
    show(r["via_view"], via=True)
    print("\nCallers:")
    for c in r["callers"] or []:
        print(f"  {names.get(c['caller'], c['caller'])} calls {names.get(c['calls'], c['calls'])}")
    if not r["callers"]:
        print("  (none)")
    print("\nTriggers on this table:")
    for t in r["triggers"]:
        print(f"  {t['display']} ({'INSTEAD OF' if t['instead_of'] else 'AFTER'} {', '.join(t['events'])})")
    if not r["triggers"]:
        print("  (none)")
    print("\nScheduled jobs:")
    for j in r["jobs"]:
        print(f"  {j['job']}{'' if j['enabled'] else ' (disabled)'}: {'; '.join(j['schedules']) or 'no active schedule'}"
              f"  via {', '.join(names.get(v, v) for v in j['via'])}")
    if not r["jobs"]:
        print("  (none)")
    print(f"\nRelated tables (confidence >= {min_conf}):")
    for x in r["related"]:
        print(f"  {x['column']:<20} {x['role']:<16} {names.get(x['other_table'])}.{x['other_column']}"
              f"  [confidence {x['confidence']}]")
    if not r["related"]:
        print("  (none)")


if __name__ == "__main__":
    main()
