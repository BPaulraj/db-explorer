"""Detect per-table column prefixes (trdclient -> trd + client) and derive logical column names."""
import math
import re
from collections import Counter

KEY_SUFFIXES = ("code", "cd", "id", "no", "num", "key")


def root(logical):
    """clientcode / clientid / client_id -> client. Used for looser matching."""
    s = logical.rstrip("_")
    for suf in KEY_SUFFIXES:
        if s.endswith(suf) and len(s) - len(suf) >= 3:
            return s[: -len(suf)].rstrip("_")
    return s


def strip_prefix(col, prefix):
    col = col.lower()
    if prefix and col.startswith(prefix) and len(col) > len(prefix) + 1:
        return col[len(prefix):].lstrip("_")
    return col


def _candidates(cols, cfg):
    """Prefixes that cover enough columns, shortest to longest; '' is always a candidate."""
    n = len(cols)
    out = [("", 0)]
    if n < 2:
        return out
    need = max(2, math.ceil(cfg.min_prefix_coverage * n))
    counts = Counter(c[: cfg.min_prefix_length] for c in cols if len(c) > cfg.min_prefix_length + 1)
    if not counts:
        return out
    prefix, count = counts.most_common(1)[0]
    if count < need or not re.match(r"^[a-z]", prefix):
        return out
    out.append((prefix, count))
    # Extend one character at a time while every prefixed column still shares it.
    for length in range(cfg.min_prefix_length + 1, cfg.max_prefix_length + 1):
        ext = Counter(c[:length] for c in cols if c.startswith(prefix) and len(c) > length + 1)
        if not ext:
            break
        nxt, ncount = ext.most_common(1)[0]
        if ncount != count:
            break
        prefix = nxt
        out.append((prefix, count))
    return out


def detect_prefixes(catalog, cfg):
    """Returns {table_key: {"prefix", "coverage", "method"}} for every table and view."""
    keys = [o["key"] for o in catalog.objects.values() if o["type"] in ("U", "V")]
    colnames = {k: [c["name"].lower() for c in catalog.columns.get(k, [])] for k in keys}
    cands = {k: _candidates(colnames[k], cfg) for k in keys}

    # Vocabulary of logical roots each table could produce, for cross-table agreement.
    vocab = {}
    for k in keys:
        roots = set()
        for prefix, _ in cands[k]:
            roots.update(root(strip_prefix(c, prefix)) for c in colnames[k])
        vocab[k] = {r for r in roots if len(r) >= 3}
    seen_in = Counter(r for k in keys for r in vocab[k])

    result = {}
    for k in keys:
        o = catalog.objects[k]
        cols = colnames[k]
        override = cfg.prefix_overrides.get(o["name"].lower(), cfg.prefix_overrides.get(k))
        if override is not None:
            cov = sum(c.startswith(override) for c in cols) / len(cols) if cols and override else 0
            result[k] = {"prefix": override, "coverage": round(cov, 2), "method": "override"}
            continue
        best = None
        for prefix, count in cands[k]:
            roots = {root(strip_prefix(c, prefix)) for c in cols}
            own = vocab[k]
            # Roots also produced by some other table (seen_in counts this table once if present).
            agree = sum(1 for r in roots if len(r) >= 3 and seen_in[r] - (1 if r in own else 0) > 0)
            score = (agree, len(prefix))
            if best is None or score > best[0]:
                best = (score, prefix, count)
        _, prefix, count = best
        result[k] = {"prefix": prefix, "coverage": round(count / len(cols), 2) if cols else 0,
                     "method": "auto" if prefix else "none"}
    return result


def logical_name(col, prefix, cfg):
    name = strip_prefix(col, prefix)
    return cfg.logical_aliases.get(name, name)
