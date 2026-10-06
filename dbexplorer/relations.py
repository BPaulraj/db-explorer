"""Combine declared FKs, joins found in code, name inference and data checks into scored relationships."""
from collections import defaultdict

from .prefix import logical_name, root

INT_TYPES = {"tinyint", "smallint", "int", "bigint"}
CHAR_TYPES = {"char", "varchar", "nchar", "nvarchar"}
NON_KEY_TYPES = {"bit", "float", "real", "money", "smallmoney", "datetime", "datetime2", "date", "time",
                 "smalldatetime", "datetimeoffset", "text", "ntext", "image", "xml", "varbinary",
                 "binary", "timestamp", "rowversion", "geography", "geometry", "hierarchyid", "sql_variant"}
# Each table's own surrogate key ends up with one of these logical names (trdid -> id); they never
# match across tables by name alone.
SURROGATE = {"id", "code", "cd", "no", "num", "key", "sno", "srno", "slno", "seqno", "rowid", "pk", "uid", "guid"}
MASTER_SUFFIXES =("master", "mst", "mast", "details", "detail", "dtl", "info", "s", "es")
MASTER_PREFIXES = ("tbl_", "tbl", "m_", "mst_", "t_")


def type_family(c):
    t = c["type"].lower()
    if t in NON_KEY_TYPES:
        return None
    if t in INT_TYPES or (t in ("decimal", "numeric") and (c.get("scale") or 0) == 0):
        return "int"
    if t in CHAR_TYPES:
        # max_length is in bytes; -1 means MAX. Very long text columns are not keys.
        if c["max_length"] == -1 or c["max_length"] > 200:
            return None
        return "char"
    if t == "uniqueidentifier":
        return "guid"
    return t


def _bare_table_name(name):
    n = name.lower()
    for p in MASTER_PREFIXES:
        if n.startswith(p) and len(n) > len(p) + 2:
            n = n[len(p):]
            break
    n = n.strip("_")
    for s in MASTER_SUFFIXES:
        if n.endswith(s) and len(n) - len(s) >= 3:
            return {n, n[: -len(s)].rstrip("_")}
    return {n}


class EdgeSet:
    def __init__(self):
        self.edges = {}

    def get(self, a, b):
        """a, b = (table_key, column). Stored unordered; direction recorded separately."""
        k = (a, b) if a <= b else (b, a)
        if k not in self.edges:
            self.edges[k] = {"a": {"table": k[0][0], "column": k[0][1]},
                             "b": {"table": k[1][0], "column": k[1][1]},
                             "parent": None, "sources": {}}
        return self.edges[k]

    @staticmethod
    def set_parent(edge, side_key):
        if edge["parent"] is None:
            edge["parent"] = side_key


def build_relationships(catalog, prefixes, analyses, cfg, validation=None):
    es = EdgeSet()
    tables = {o["key"] for o in catalog.tables()}

    # 1. Declared foreign keys.
    for fk in catalog.data["foreign_keys"]:
        child = catalog.by_id.get(fk["child_id"])
        parent = catalog.by_id.get(fk["parent_id"])
        if not child or not parent:
            continue
        for cc, pc in fk["columns"]:
            a, b = (child["key"], cc.lower()), (parent["key"], pc.lower())
            if a == b:
                continue
            e = es.get(a, b)
            e["sources"]["fk"] = {"name": fk["name"], "disabled": fk["disabled"],
                                  "composite": len(fk["columns"]) > 1}
            e["parent"] = "a" if e["a"] == {"table": b[0], "column": b[1]} else "b"

    # 2. Joins found in procedures, views, triggers and functions.
    for mkey, an in analyses.items():
        for (ta, ca), (tb, cb), line, dynamic in an.joins:
            if ta not in tables or tb not in tables:
                continue
            e = es.get((ta, ca), (tb, cb))
            src = e["sources"].setdefault("code", {"count": 0, "modules": {}})
            src["count"] += 1
            src["modules"].setdefault(catalog.display(mkey), []).append(line)

    # 3. Name inference from prefix-stripped logical names.
    logical = {}       # (table, col) -> logical
    groups = defaultdict(list)
    ntables = len(tables)
    for tkey in tables:
        prefix = prefixes.get(tkey, {}).get("prefix", "")
        for c in catalog.columns.get(tkey, []):
            fam = type_family(c)
            lname = logical_name(c["name"], prefix, cfg)
            logical[(tkey, c["name"].lower())] = lname
            if fam is None or c.get("computed"):
                continue
            if lname in cfg.generic_columns or root(lname) in cfg.generic_columns or lname in SURROGATE:
                continue
            groups[root(lname)].append((tkey, c["name"].lower(), lname, fam, c))

    # 3a. A logical name that equals another table's raw unique column: ldgtrdid -> 'trdid' = trade.trdid
    raw_unique = defaultdict(list)
    for tkey in tables:
        for cols in catalog.unique.get(tkey, []):
            if len(cols) == 1:
                raw_unique[cols[0]].append(tkey)
    for (tkey, col), lname in logical.items():
        if lname == col or lname in SURROGATE:
            continue
        for owner in raw_unique.get(lname, []):
            if owner == tkey:
                continue
            c1, c2 = catalog.colmap[tkey][col], catalog.colmap[owner][lname]
            if type_family(c1) is None or type_family(c1) != type_family(c2):
                continue
            e = es.get((tkey, col), (owner, lname))
            e["sources"]["name"] = {"logical": lname, "kind": "parent", "exact": True,
                                    "same_type": c1["type"] == c2["type"]}
            EdgeSet.set_parent(e, "a" if e["a"] == {"table": owner, "column": lname} else "b")

    key_groups = []
    for rt, members in groups.items():
        tset = {m[0] for m in members}
        if len(tset) < 2 or rt in SURROGATE:
            continue
        owners = [m for m in members if catalog.is_single_unique(m[0], m[1])]
        owner_kind = "pk"
        if not owners:
            owners = [m for m in members if rt in _bare_table_name(catalog.objects[m[0]]["name"])
                      or m[2] in _bare_table_name(catalog.objects[m[0]]["name"])]
            owner_kind = "table-name"
        if not owners and ntables >= 10 and len(tset) / ntables > cfg.max_table_fraction:
            key_groups.append({"logical": rt, "tables": len(tset), "status": "skipped (too common, no owner table)",
                               "members": sorted({catalog.display(m[0]) + "." + m[1] for m in members})})
            continue
        if owners:
            for o in owners:
                for m in members:
                    if m[0] == o[0] or m[3] != o[3]:
                        continue
                    e = es.get((m[0], m[1]), (o[0], o[1]))
                    exact = m[2] == o[2]
                    e["sources"]["name"] = {"logical": m[2] if exact else f"{m[2]} ~ {o[2]}",
                                            "kind": "parent" if owner_kind == "pk" else "parent-by-table-name",
                                            "exact": exact,
                                            "same_type": m[4]["type"] == o[4]["type"]}
                    EdgeSet.set_parent(e, "a" if e["a"] == {"table": o[0], "column": o[1]} else "b")
            key_groups.append({"logical": rt, "tables": len(tset), "status": f"owner: "
                               + ", ".join(sorted({catalog.display(o[0]) for o in owners}))})
        elif len(tset) <= cfg.max_peer_group:
            for i, m in enumerate(members):
                for o in members[i + 1:]:
                    if m[0] == o[0] or m[3] != o[3]:
                        continue
                    e = es.get((m[0], m[1]), (o[0], o[1]))
                    e["sources"].setdefault("name", {"logical": m[2] if m[2] == o[2] else f"{m[2]} ~ {o[2]}",
                                                     "kind": "peer", "exact": m[2] == o[2],
                                                     "same_type": m[4]["type"] == o[4]["type"]})
            key_groups.append({"logical": rt, "tables": len(tset), "status": "peer (no owner table found)"})
        else:
            key_groups.append({"logical": rt, "tables": len(tset),
                               "status": "no owner table found; too many tables to draw"})
        key_groups[-1]["members"] = sorted({catalog.display(m[0]) + "." + m[1] for m in members})

    # Code-join direction from unique keys when nothing else decided it.
    for e in es.edges.values():
        if e["parent"] is None:
            ua = catalog.is_single_unique(e["a"]["table"], e["a"]["column"])
            ub = catalog.is_single_unique(e["b"]["table"], e["b"]["column"])
            if ua and not ub:
                e["parent"] = "a"
            elif ub and not ua:
                e["parent"] = "b"

    # 4. Data validation results, if any.
    validation = validation or {}
    for e in es.edges.values():
        v = validation.get(edge_id(e))
        if v:
            e["sources"]["data"] = v

    for e in es.edges.values():
        e["id"] = edge_id(e)
        e["confidence"], e["evidence"] = score(e)
        e["logical"] = logical.get((e["a"]["table"], e["a"]["column"]))
    edges = sorted(es.edges.values(), key=lambda e: -e["confidence"])
    return edges, key_groups, logical


def edge_id(e):
    return f"{e['a']['table']}.{e['a']['column']}|{e['b']['table']}.{e['b']['column']}"


def score(e):
    s = e["sources"]
    parts, evidence = [], []
    if "fk" in s:
        parts.append(90 if s["fk"]["disabled"] else 100)
        evidence.append("declared FK" + (" (disabled)" if s["fk"]["disabled"] else ""))
    if "code" in s:
        c = s["code"]
        parts.append(55 + 10 * min(c["count"] - 1, 3))
        evidence.append(f"joined in code {c['count']}x ({len(c['modules'])} objects)")
    if "name" in s:
        n = s["name"]
        base = {"parent": 50, "parent-by-table-name": 40, "peer": 30}[n["kind"]]
        if not n["exact"]:
            base -= 12
        if n["same_type"]:
            base += 8
        parts.append(base)
        evidence.append(f"name match '{n['logical']}' ({n['kind']})")
    conf = max(parts) if parts else 0
    kinds = len({k for k in s if k in ("fk", "code", "name")})
    if kinds >= 2 and conf < 100:
        conf = min(97, conf + 8 * (kinds - 1))
    d = s.get("data")
    if d and conf < 100:
        if d.get("error"):
            evidence.append(f"data check failed: {d['error'][:80]}")
        elif d["sampled"] == 0:
            evidence.append("data check: child column empty")
        else:
            pct = d["orphan_pct"]
            if d["matched"] == 0:
                conf = min(conf, 5)
            elif pct <= 1:
                conf = min(98, conf + 15)
            elif pct <= 10:
                conf = min(98, conf + 5)
            elif pct > 50:
                conf = max(5, conf - 25)
            evidence.append(f"data: {100 - pct:.1f}% of {d['sampled']} sampled values match")
    return conf, evidence
