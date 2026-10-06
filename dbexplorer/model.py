"""In-memory view of the extracted catalog with name resolution helpers."""
import json
from pathlib import Path

MODULE_TYPES = {"P": "Procedure", "V": "View", "TR": "Trigger", "FN": "Function", "IF": "Function", "TF": "Function"}


def split_name(name):
    """'[db]..[t]' -> ['db', '', 't']"""
    parts, cur, i = [], "", 0
    while i < len(name):
        ch = name[i]
        if ch in "[\"":
            close = "]" if ch == "[" else '"'
            j = name.find(close, i + 1)
            j = len(name) if j < 0 else j
            cur += name[i + 1:j]
            i = j + 1
        elif ch == ".":
            parts.append(cur.strip())
            cur, i = "", i + 1
        else:
            cur += ch
            i += 1
    parts.append(cur.strip())
    return parts


class Catalog:
    def __init__(self, data):
        self.data = data
        self.database = (data["meta"].get("database") or "").lower()
        self.objects = {}      # key -> object dict
        self.by_id = {}
        self.by_name = {}      # bare lowercase name -> [keys]
        for o in data["objects"]:
            key = f"{o['schema']}.{o['name']}".lower()
            o["key"] = key
            o["display"] = f"{o['schema']}.{o['name']}"
            self.objects[key] = o
            self.by_id[o["id"]] = o
            self.by_name.setdefault(o["name"].lower(), []).append(key)

        self.columns = {}      # key -> [column dicts] in column order
        self.colmap = {}       # key -> {lower name: column dict}
        for c in data["columns"]:
            o = self.by_id.get(c["id"])
            if not o:
                continue
            self.columns.setdefault(o["key"], []).append(c)
            self.colmap.setdefault(o["key"], {})[c["name"].lower()] = c

        self.pk = {}           # key -> [lower column names]
        self.unique = {}       # key -> [[lower column names], ...] including the PK
        for ix in data["indexes"]:
            o = self.by_id.get(ix["id"])
            if not o:
                continue
            cols = [c.lower() for c in ix["columns"]]
            if ix["is_pk"]:
                self.pk[o["key"]] = cols
            if ix["is_unique"] or ix["is_pk"]:
                self.unique.setdefault(o["key"], []).append(cols)

        for o in self.objects.values():
            if o["type"] == "TR" and o.get("parent_id") in self.by_id:
                o["parent_key"] = self.by_id[o["parent_id"]]["key"]

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def tables(self):
        return [o for o in self.objects.values() if o["type"] == "U"]

    def modules(self):
        return [o for o in self.objects.values() if o["type"] in MODULE_TYPES]

    def display(self, key):
        o = self.objects.get(key)
        return o["display"] if o else key

    def is_single_unique(self, key, col):
        return [col.lower()] in self.unique.get(key, [])

    def resolve(self, parts, default_schema="dbo", _depth=0):
        """Resolve a multi-part name. Returns (key, None) or (None, external_name)."""
        parts = [p.lower() for p in parts]
        if not parts or not parts[-1]:
            return None, None
        if len(parts) >= 4:
            return None, ".".join(parts)
        if len(parts) == 3:
            if parts[0] and parts[0] != self.database:
                return None, ".".join(parts)
            parts = parts[1:]
        if len(parts) == 2:
            schema, name = parts[0] or "dbo", parts[1]
            key = self.objects.get(f"{schema}.{name}") and f"{schema}.{name}"
        else:
            name = parts[0]
            key = None
            for schema in (default_schema, "dbo"):
                if f"{schema}.{name}" in self.objects:
                    key = f"{schema}.{name}"
                    break
            if not key and len(self.by_name.get(name, [])) == 1:
                key = self.by_name[name][0]
        if not key:
            return None, None
        o = self.objects[key]
        if o["type"] == "SN" and o.get("base") and _depth < 3:
            target, ext = self.resolve(split_name(o["base"]), o["schema"].lower(), _depth + 1)
            return (target, ext) if (target or ext) else (key, None)
        return key, None
