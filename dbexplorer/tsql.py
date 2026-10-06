"""Lightweight T-SQL analyzer for stored procedures, views, triggers and functions.

It does not build a full AST. It tokenizes the module, splits it into statements, and in
each statement finds object references (with the operation applied to them), column
references (resolved through aliases and the catalog), equality joins, procedure calls and
dynamic SQL. That is enough for impact analysis and is tolerant of syntax it does not know.
"""
import re
from dataclasses import dataclass, field

KEYWORDS = set("""
ADD ALL ALTER AND ANY APPLY AS ASC AUTHORIZATION BACKUP BEGIN BETWEEN BREAK BROWSE BULK BY
CASCADE CASE CATCH CHECK CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT COMPUTE
CONSTRAINT CONTAINS CONTINUE CONVERT CREATE CROSS CURRENT CURSOR DEALLOCATE DECLARE DEFAULT
DELETE DENY DESC DISTINCT DROP ELSE END ERRLVL ESCAPE EXCEPT EXEC EXECUTE EXISTS EXIT FETCH
FOR FOREIGN FROM FULL FUNCTION GO GOTO GRANT GROUP HAVING HOLDLOCK IDENTITY IF IN INDEX INNER
INSERT INTERSECT INTO IS JOIN KEY KILL LEFT LIKE MERGE NOCHECK NOLOCK NONCLUSTERED NOT NULL
OF OFF OFFSET ON OPEN OPTION OR ORDER OUTER OUTPUT OVER PERCENT PIVOT PRIMARY PRINT PROC
PROCEDURE RAISERROR READ RETURN RETURNS REVOKE RIGHT ROLLBACK ROWCOUNT ROWS ROW RULE SAVE
SCHEMA SELECT SET SOME TABLE TABLESAMPLE THEN THROW TIES TO TOP TRAN TRANSACTION TRIGGER
TRUNCATE TRY UNION UNIQUE UNPIVOT UPDATE USE USING VALUES VIEW WAITFOR WHEN WHERE WHILE WITH
MATCHED TARGET SOURCE NEXT ONLY READUNCOMMITTED ROWLOCK UPDLOCK PAGLOCK TABLOCK TABLOCKX XLOCK
READPAST NOWAIT INSTEAD AFTER RECOMPILE NOCOUNT XACT_ABORT
""".split())

STMT_START = set("""
INSERT UPDATE DELETE MERGE TRUNCATE IF ELSE WHILE BEGIN END DECLARE RETURN EXEC EXECUTE PRINT
RAISERROR THROW OPEN CLOSE FETCH DEALLOCATE COMMIT ROLLBACK SAVE GO SET SELECT WITH CREATE
ALTER DROP BREAK CONTINUE GOTO USE WAITFOR
""".split())

DML_HEADS = {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"}
SET_REGION_END = {"FROM", "WHERE", "OUTPUT", "OPTION", "WHEN"}


@dataclass
class Tok:
    kind: str      # id, var, num, str, op, punct
    text: str
    line: int
    quoted: bool = False

    @property
    def up(self):
        return "" if self.quoted else self.text.upper()

    @property
    def is_name(self):
        return self.kind == "id" and (self.quoted or self.text.upper() not in KEYWORDS)


TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<lc>--[^\n]*)
  | (?P<bc>/\*)
  | (?P<str>N?'(?:[^']|'')*'?)
  | (?P<br>\[(?:[^\]]|\]\])*\]?)
  | (?P<dq>"(?:[^"]|"")*"?)
  | (?P<var>@@?[\w@#$]+)
  | (?P<id>[A-Za-z_#\u0080-￿][\w@#$\u0080-￿]*)
  | (?P<num>0x[0-9A-Fa-f]*|\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+)
  | (?P<op><>|!=|>=|<=|!<|!>|\+=|-=|\*=|/=|[=<>+\-*/%&|^~])
  | (?P<punct>[.,();:])
  | (?P<other>.)
""", re.X | re.S)


def tokenize(sql):
    toks, pos, line = [], 0, 1
    n = len(sql)
    while pos < n:
        m = TOKEN_RE.match(sql, pos)
        kind, text = m.lastgroup, m.group()
        if kind == "bc":
            # Nested block comments are legal in T-SQL.
            depth, j = 1, pos + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            text = sql[pos:j]
        elif kind == "str":
            body = text[2:] if text[0] in "Nn" else text[1:]
            toks.append(Tok("str", body[:-1].replace("''", "'") if body.endswith("'") else body, line))
        elif kind == "br":
            toks.append(Tok("id", text[1:-1].replace("]]", "]"), line, quoted=True))
        elif kind == "dq":
            toks.append(Tok("id", text[1:-1].replace('""', '"'), line, quoted=True))
        elif kind in ("var", "id", "num", "op", "punct"):
            toks.append(Tok(kind, text, line))
        line += text.count("\n")
        pos = m.end()
    return toks


def skip_header(toks):
    """Drop 'CREATE PROCEDURE x @a int ... AS' so the body starts at the first statement."""
    if not toks or toks[0].up not in ("CREATE", "ALTER"):
        return toks
    depth = 0
    for i, t in enumerate(toks):
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        elif depth == 0 and t.up == "AS" and i > 0:
            prev = toks[i - 1]
            if prev.kind == "var" or prev.up in ("EXEC", "EXECUTE"):
                continue
            return toks[i + 1:]
    return toks


def metrics(definition):
    """Complexity and structure indicators used for risk-based test planning."""
    toks = tokenize(definition or "")
    body = skip_header(toks)
    ups = [t.up for t in body if t.kind == "id"]
    count = lambda *words: sum(1 for u in ups if u in words)
    code_lines = sum(1 for ln in (definition or "").splitlines() if ln.strip() and not ln.strip().startswith("--"))
    stmts = split_statements(body)
    m = {
        "lines": code_lines,
        "statements": sum(1 for s in stmts if s.head in DML_HEADS),
        "writes": sum(1 for s in stmts if s.head in ("INSERT", "UPDATE", "DELETE", "MERGE")),
        "branches": count("IF", "WHILE", "WHEN"),
        "loops": count("WHILE"),
        "cursors": sum(1 for i, u in enumerate(ups[:-1]) if u == "CURSOR"),
        "transactions": sum(1 for i, t in enumerate(body[:-1]) if t.up == "BEGIN" and body[i + 1].up in ("TRAN", "TRANSACTION")),
        "try_catch": count("CATCH") > 0,
        "raises_errors": count("RAISERROR", "THROW"),
        "temp_tables": len({t.text.lower() for t in body if t.kind == "id" and t.text.startswith("#")}),
    }
    m["complexity"] = 1 + m["branches"] + m["loops"] * 2 + m["cursors"] * 3
    return m


def header_parameters(definition):
    """{'@client': {'default': "'X'", 'output': False}} from 'CREATE PROC p @client varchar(10) = 'X' ...'."""
    toks = tokenize(definition or "")
    body = skip_header(toks)
    header = toks[: len(toks) - len(body)]
    params, depth, cur = {}, 0, None
    i = 0
    while i < len(header):
        t = header[i]
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        elif t.kind == "var" and depth <= 1 and i and header[i - 1].up != "RETURNS" and \
                (header[i - 1].text in (",", "(") or header[i - 1].kind == "id"):
            cur = params.setdefault(t.text.lower(), {"default": None, "output": False})
        elif cur is not None and t.text == "=" and depth <= 1:
            j, parts = i + 1, []
            while j < len(header) and header[j].text != "," and header[j].up not in ("OUTPUT", "OUT", "READONLY", "AS") \
                    and not (header[j].text == ")" and depth == 1):
                tok = header[j]
                parts.append(f"'{tok.text}'" if tok.kind == "str" else tok.text)
                j += 1
            cur["default"] = " ".join(parts)
            i = j
            continue
        elif cur is not None and t.up in ("OUTPUT", "OUT"):
            cur["output"] = True
        i += 1
    return params


def _split_args(toks, open_idx):
    """Arguments of a parenthesised call starting at toks[open_idx] == '('. Returns (list of token lists, close idx)."""
    args, cur, depth = [], [], 0
    for j in range(open_idx, len(toks)):
        t = toks[j]
        if t.text == "(":
            depth += 1
            if depth == 1:
                continue
        elif t.text == ")":
            depth -= 1
            if depth == 0:
                args.append(cur)
                return args, j
        elif t.text == "," and depth == 1:
            args.append(cur)
            cur = []
            continue
        cur.append(t)
    args.append(cur)
    return args, len(toks) - 1


def _expr_text(toks):
    """Readable text of an expression; string literals quoted, the rest as written."""
    out = ""
    prev = None
    for t in toks:
        text = "'" + t.text.replace("'", "''") + "'" if t.kind == "str" else ("[" + t.text + "]" if t.quoted else t.text)
        glue = prev is None or t.text in (")", ",", ".") or prev.text in ("(", ".") or \
            (t.text == "(" and prev.kind == "id" and prev.up not in ("IN", "EXISTS", "AND", "OR", "NOT", "WHERE"))
        out += ("" if glue else " ") + text
        prev = t
    return out


def _message(toks, assigned):
    """Message text of RAISERROR/THROW: literals joined, variables resolved to their assigned literals."""
    if not toks:
        return None, False
    if len(toks) == 1 and toks[0].kind == "var":
        lits = assigned.get(toks[0].text.lower())
        if lits:
            return " | ".join(dict.fromkeys(lits)), True
        return f"{toks[0].text} (built at runtime)", False
    parts = []
    for t in toks:
        if t.kind == "str":
            parts.append(t.text)
        elif t.kind == "var":
            lits = assigned.get(t.text.lower())
            parts.append(lits[0] if lits and len(lits) == 1 else "{" + t.text + "}")
        elif t.kind == "id" and t.up in ("CAST", "CONVERT", "VARCHAR", "NVARCHAR", "AS", "INT", "FORMATMESSAGE", "CONCAT"):
            continue
    text = "".join(parts).strip()
    return (text or _expr_text(toks)), bool(text)


def error_catalog(definition, user_messages=None):
    """Every RAISERROR / THROW / RETURN <code> in a module, with the condition that triggers it."""
    user_messages = user_messages or {}
    toks = skip_header(tokenize(definition or ""))
    n = len(toks)

    def expr_until_boundary(j, stop_at_comma=True):
        """Tokens from j up to ';', a top-level comma, or the next statement keyword."""
        out, depth = [], 0
        while j < n:
            t = toks[j]
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
                if depth < 0:
                    break
            elif depth == 0 and (t.text == ";" or (stop_at_comma and t.text == ",")
                                 or (t.kind == "id" and t.up in STMT_START)):
                break
            out.append(t)
            j += 1
        return out

    # Messages assigned to variables: SET @m = '...', DECLARE @m nvarchar(200) = '...', SELECT @m = '...'
    assigned = {}
    for i, t in enumerate(toks):
        if t.kind != "var" or not i:
            continue
        j = None
        if i + 1 < n and toks[i + 1].text == "=" and (toks[i - 1].up in ("SET", "SELECT") or toks[i - 1].text == ","):
            j = i + 1
        elif toks[i - 1].up == "DECLARE" or (toks[i - 1].text == "," and any(x.up == "DECLARE" for x in toks[max(0, i - 12):i])):
            for k in range(i + 1, min(n, i + 8)):
                if toks[k].text == "=":
                    j = k
                    break
                if toks[k].text in (",", ";"):
                    break
        if j is None:
            continue
        expr = expr_until_boundary(j + 1)
        if any(x.kind == "str" for x in expr):
            msg, _ = _message(expr, assigned)
            assigned.setdefault(t.text.lower(), []).append(msg)

    # TRY / CATCH ranges.
    catch_ranges, stack = [], []
    for i, t in enumerate(toks):
        if t.up == "BEGIN" and i + 1 < n and toks[i + 1].up == "CATCH":
            stack.append(i)
        elif t.up == "END" and i + 1 < n and toks[i + 1].up == "CATCH" and stack:
            catch_ranges.append((stack.pop(), i))
    in_catch = lambda i: any(a <= i <= b for a, b in catch_ranges)

    # Conditions: walk back over statements from the raising one.
    stmts = split_statements(toks)
    where = {}
    for si, st in enumerate(stmts):
        for t in st.toks:
            where[id(t)] = si

    def if_of(k):
        """Condition text for the statement just before a block / statement at index k+1."""
        if k < 0:
            return None
        st = stmts[k]
        if st.head == "IF":
            return _expr_text(st.toks[1:])[:250]
        if st.head == "ELSE":
            j, depth = k - 1, 0
            while j >= 0:     # the matching IF, skipping nested BEGIN..END blocks
                h = stmts[j].head
                if h == "END":
                    depth += 1
                elif h == "BEGIN" and depth:
                    depth -= 1
                elif h == "IF" and not depth:
                    return "NOT (" + _expr_text(stmts[j].toks[1:])[:240] + ")"
                j -= 1
            return "ELSE branch"
        return None

    def condition(i):
        si = where.get(id(toks[i]))
        if si is None:
            return ""
        direct = if_of(si - 1)        # IF cond RAISERROR(...)
        if direct:
            return direct
        k, depth = si - 1, 0
        while k >= 0:                 # walk up to the enclosing BEGIN; TRY/CATCH blocks are transparent
            h = stmts[k].head
            second = stmts[k].toks[1].up if len(stmts[k].toks) > 1 else ""
            if second in ("TRAN", "TRANSACTION", "DISTRIBUTED"):
                pass                  # BEGIN TRAN is a statement, not a block
            elif h == "END":
                depth += 1
            elif h == "BEGIN":
                if depth:
                    depth -= 1
                elif second not in ("TRY", "CATCH"):
                    return if_of(k - 1) or ""
            k -= 1
        return ""

    out = []
    for i, t in enumerate(toks):
        e = None
        if t.up == "RAISERROR" and i + 1 < n and toks[i + 1].text == "(":
            args, close = _split_args(toks, i + 1)
            first = args[0] if args else []
            e = {"kind": "RAISERROR", "number": None, "severity": None, "state": None}
            if len(first) == 1 and first[0].kind == "num":
                e["number"] = int(float(first[0].text))
                um = user_messages.get(e["number"])
                e["message"], e["literal"] = (um["text"], True) if um else (f"message {e['number']} (not in sys.messages)", False)
            else:
                e["message"], e["literal"] = _message(first, assigned)
                e["number"] = 50000
            if len(args) > 1:
                sev = args[1]
                e["severity"] = int(sev[0].text) if len(sev) == 1 and sev[0].kind == "num" else _expr_text(sev)
            if len(args) > 2 and len(args[2]) == 1 and args[2][0].kind == "num":
                e["state"] = int(args[2][0].text)
            tail = [x.up for x in toks[close + 1: close + 6]]
            e["options"] = [o for o in ("LOG", "NOWAIT", "SETERROR") if o in tail] if "WITH" in tail[:1] else []
        elif t.up == "THROW":
            nxt = toks[i + 1] if i + 1 < n else None
            if nxt is None or nxt.text == ";" or (nxt.kind == "id" and nxt.up in STMT_START):
                e = {"kind": "THROW (rethrow)", "number": None, "severity": None, "state": None,
                     "message": "re-raises the caught error", "literal": False}
            else:
                args, j = [], i + 1
                while j < n and len(args) < 3:
                    part = expr_until_boundary(j)
                    args.append(part)
                    j += len(part)
                    if j < n and toks[j].text == ",":
                        j += 1
                    else:
                        break
                num = args[0]
                e = {"kind": "THROW", "number": int(num[0].text) if len(num) == 1 and num[0].kind == "num" else _expr_text(num),
                     "severity": 16, "state": None}
                e["message"], e["literal"] = _message(args[1] if len(args) > 1 else [], assigned)
        elif t.up == "RETURN" and i + 1 < n:
            j = i + 1
            neg = toks[j].text == "-"
            if neg:
                j += 1
            if j < n and toks[j].kind == "num" and toks[j].text not in ("0",):
                e = {"kind": "RETURN code", "number": int(("-" if neg else "") + toks[j].text.split(".")[0]),
                     "severity": None, "state": None, "message": "", "literal": False}
        if e:
            e["line"] = t.line
            e["in_catch"] = in_catch(i)
            e["condition"] = "" if e["in_catch"] and e["kind"].startswith("THROW (") else condition(i)
            sev = e.get("severity")
            if isinstance(sev, int) and sev <= 10 and e["kind"] == "RAISERROR":
                e["warning"] = "severity <= 10 is informational: the caller and CATCH blocks do not see an error"
            elif isinstance(sev, int) and sev >= 20 and "LOG" not in e.get("options", []):
                e["warning"] = "severity >= 20 requires WITH LOG and terminates the connection"
            out.append(e)
    return out


@dataclass
class Statement:
    head: str
    toks: list = field(default_factory=list)


def split_statements(toks):
    stmts, cur = [], None
    depth = case_depth = 0
    head = None
    main_set = False       # for WITH: has the main DML started; for INSERT: has the SELECT source started

    def start(t):
        nonlocal cur, head, main_set
        if cur and cur.toks:
            stmts.append(cur)
        head = t.up if t is not None else None
        main_set = False
        cur = Statement(head or "")

    start(None)
    for i, t in enumerate(toks):
        up = t.up
        prev = toks[i - 1].up if i else ""
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t.text == ";":
            start(None)
            continue
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth = max(0, depth - 1)
        elif up == "CASE":
            case_depth += 1
        elif up == "END" and case_depth:
            case_depth -= 1
            cur.toks.append(t)
            continue

        split = False
        if depth == 0 and not case_depth and up in STMT_START and t.kind == "id":
            split = True
            if head == "MERGE":
                split = False
            elif prev in ("FOR", "ROWS", "ROW", "UNION", "ALL", "EXCEPT", "INTERSECT"):
                split = False
            elif up == "WITH":
                # A CTE (WITH name AS ( / WITH name (cols) AS) starts a statement; table hints don't.
                after = toks[i + 2] if i + 2 < len(toks) else None
                split = (nxt is not None and nxt.kind == "id" and after is not None
                         and (after.up == "AS" or after.text == "("))
            elif head == "WITH" and up in DML_HEADS and not main_set:
                head, main_set, split = up, up != "INSERT", False
                cur.head = up
            elif head == "INSERT" and up == "SELECT" and not main_set:
                main_set, split = True, False
            elif head == "INSERT" and up in ("EXEC", "EXECUTE") and not main_set:
                main_set = True       # INSERT ... EXEC proc: keep together, the call is still recorded
                split = False
            elif head == "UPDATE" and up == "SET":
                split = False
        if head == "INSERT" and up == "VALUES" and depth == 0:
            main_set = True
        if split:
            start(t)
        cur.toks.append(t)
    start(None)
    return stmts


@dataclass
class Ref:
    key: str
    op: str
    line: int
    dynamic: bool = False
    cols_read: set = field(default_factory=set)
    cols_written: set = field(default_factory=set)
    detail: str = ""


@dataclass
class ModuleAnalysis:
    refs: list = field(default_factory=list)          # Ref
    joins: list = field(default_factory=list)         # ((key, col), (key, col), line, dynamic)
    calls: list = field(default_factory=list)         # (key or name, line, resolved)
    unresolved: list = field(default_factory=list)    # (name, line)
    external: list = field(default_factory=list)      # (name, line, op)
    temp_tables: set = field(default_factory=set)
    dynamic_sql: bool = False
    dynamic_lines: list = field(default_factory=list)


def _read_name(toks, i):
    """Read a multi-part name starting at i. Returns (parts, next_index) or (None, i)."""
    if i >= len(toks) or toks[i].kind != "id":
        return None, i
    parts = [toks[i].text]
    j = i + 1
    while j < len(toks) and toks[j].text == ".":
        if j + 1 < len(toks) and toks[j + 1].text == ".":
            parts.append("")
            j += 1
            continue
        if j + 1 < len(toks) and toks[j + 1].kind == "id":
            parts.append(toks[j + 1].text)
            j += 2
        else:
            break
    return parts, j


def _match_paren(toks, i):
    depth = 0
    for j in range(i, len(toks)):
        if toks[j].text == "(":
            depth += 1
        elif toks[j].text == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(toks) - 1


class Analyzer:
    def __init__(self, catalog):
        self.cat = catalog

    def analyze(self, definition, default_schema="dbo", dynamic=False):
        res = ModuleAnalysis()
        toks = skip_header(tokenize(definition or ""))
        for idx, t in enumerate(toks):
            if t.up in ("EXEC", "EXECUTE") and idx + 1 < len(toks):
                n1 = toks[idx + 1]
                if n1.text == "(" or (n1.kind == "var" and not (idx + 2 < len(toks) and toks[idx + 2].text == "=")):
                    res.dynamic_sql = True
                    res.dynamic_lines.append(t.line)
            if t.kind == "id" and t.text.lower() == "sp_executesql":
                res.dynamic_sql = True
                res.dynamic_lines.append(t.line)
        for st in split_statements(toks):
            self._statement(st, res, default_schema, dynamic)
        if res.dynamic_sql and not dynamic:
            # Best effort: analyze the string literals that build the dynamic SQL.
            text = "\n".join(t.text for t in toks if t.kind == "str" and re.search(
                r"\b(select|insert|update|delete|merge|from|join|truncate)\b", t.text, re.I))
            if text:
                dyn = self.analyze(text, default_schema, dynamic=True)
                res.refs += dyn.refs
                res.joins += dyn.joins
                res.calls += dyn.calls
                res.external += dyn.external
        return res

    # ---------------------------------------------------------------- statement level
    def _statement(self, st, res, schema, dynamic):
        toks = st.toks
        n = len(toks)
        cat = self.cat
        aliases = {}          # lower alias -> set(keys)
        derived = set()       # aliases of derived tables / CTEs / table variables
        consumed = set()
        sources = []          # (key, line)
        targets = []          # (name_parts, op, line, alias_ok)
        cte_names = set()
        writes_cols = []      # (target index, colname)
        merge_actions = set()

        # CTE names: WITH a AS (...), b (x, y) AS (...)
        if toks and toks[0].up == "WITH":
            i = 1
            while i < n and toks[i].kind == "id":
                cte_names.add(toks[i].text.lower())
                consumed.add(i)
                i += 1
                if i < n and toks[i].text == "(":
                    i = _match_paren(toks, i) + 1
                if i < n and toks[i].up == "AS":
                    i += 1
                if i < n and toks[i].text == "(":
                    i = _match_paren(toks, i) + 1
                if i < n and toks[i].text == ",":
                    i += 1
                    continue
                break

        def resolve(parts):
            if len(parts) == 1 and parts[0].lower() in cte_names | {"inserted", "deleted"}:
                return "cte", None
            if parts[-1].startswith("#"):
                res.temp_tables.add(parts[-1].lower())
                return "temp", None
            key, ext = cat.resolve(parts, schema)
            return key, ext

        def read_alias(j):
            """After an object or derived table: optional [AS] alias and table hints."""
            if j < n and toks[j].up == "AS":
                consumed.add(j)
                j += 1
            alias = None
            if j < n and toks[j].is_name:
                alias = toks[j].text.lower()
                consumed.add(j)
                j += 1
            if j < n and toks[j].up == "WITH" and j + 1 < n and toks[j + 1].text == "(":
                end = _match_paren(toks, j + 1)
                consumed.update(range(j, end + 1))
                j = end + 1
            elif j < n and toks[j].text == "(" and j + 1 < n and toks[j + 1].up in KEYWORDS:
                end = _match_paren(toks, j)       # old-style hint: FROM t (NOLOCK)
                consumed.update(range(j, end + 1))
                j = end + 1
            return alias, j

        def add_source(i, line):
            """Row sources after FROM / JOIN / USING / APPLY, including old-style 'FROM a, b'."""
            j = one_source(i, line)
            while j is not None and j < n and toks[j].text == "," and j + 1 < n and \
                    (toks[j + 1].kind in ("id", "var") or toks[j + 1].text == "("):
                j = one_source(j + 1, toks[j + 1].line)

        def one_source(i, line):
            """Returns the index after the source and its alias/hints, or None if nothing was read."""
            if i < n and toks[i].text == "(":
                end = _match_paren(toks, i)
                alias, j = read_alias(end + 1)
                if alias:
                    derived.add(alias)
                return j
            if i < n and toks[i].kind == "var":
                consumed.add(i)
                alias, j = read_alias(i + 1)
                if alias:
                    derived.add(alias)
                return j
            parts, j = _read_name(toks, i)
            if not parts or (not toks[i].quoted and toks[i].up in KEYWORDS):
                return None
            consumed.update(range(i, j))
            if j < n and toks[j].text == "(":            # table-valued function
                end = _match_paren(toks, j)
                key, ext = resolve(parts)
                if key and key not in ("cte", "temp"):
                    sources.append((key, line))
                alias, j = read_alias(end + 1)
                if alias:
                    derived.add(alias)
                return j
            key, ext = resolve(parts)
            alias, j = read_alias(j)
            if key in ("cte", "temp"):
                derived.add(alias or parts[-1].lower())
            elif ext:
                res.external.append((ext, line, "SELECT"))
            elif not key:
                res.unresolved.append((".".join(parts), line))
            else:
                sources.append((key, line))
                aliases.setdefault(parts[-1].lower(), set()).add(key)
                if alias:
                    aliases.setdefault(alias, set()).add(key)
            return j

        def add_target(i, op, line):
            if i < n and toks[i].kind == "var":
                consumed.add(i)
                return i + 1
            parts, j = _read_name(toks, i)
            if not parts or (not toks[i].quoted and toks[i].up in KEYWORDS):
                return i
            consumed.update(range(i, j))
            targets.append((parts, op, line, len(targets)))
            if op in ("MERGE",):
                alias, j = read_alias(j)
                key, _ = resolve(parts)
                if alias and key not in (None, "cte", "temp"):
                    aliases.setdefault(alias, set()).add(key)
            elif op == "INSERT" and j < n and toks[j].text == "(" :
                end = _match_paren(toks, j)
                for k in range(j + 1, end):
                    if toks[k].kind == "id":
                        writes_cols.append((len(targets) - 1, toks[k].text.lower()))
                        consumed.add(k)
                j = end + 1
            elif op == "SELECT INTO":
                pass
            elif j < n and toks[j].up == "WITH" and j + 1 < n and toks[j + 1].text == "(":
                end = _match_paren(toks, j + 1)
                consumed.update(range(j, end + 1))
                j = end + 1
            return j

        set_region = set()
        in_set = False
        seen_output = False
        for i, t in enumerate(toks):
            up = t.up
            prev = toks[i - 1].up if i else ""
            nxt = toks[i + 1] if i + 1 < n else None
            line = t.line

            if in_set:
                if up in SET_REGION_END or t.text == ";":
                    in_set = False
                else:
                    set_region.add(i)
            if up == "SET" and st.head in ("UPDATE", "MERGE"):
                in_set = True
                continue

            if up == "OUTPUT":
                seen_output = True
            if up == "FROM":
                if st.head == "DELETE" and prev == "DELETE":
                    add_target(i + 1, "DELETE", line)
                elif st.head != "FETCH":           # FETCH NEXT FROM cursor
                    add_source(i + 1, line)
            elif up == "JOIN" or (up == "USING" and st.head == "MERGE"):
                add_source(i + 1, line)
            elif up == "APPLY":
                add_source(i + 1, line)
            elif up == "INTO":
                if prev == "INSERT":
                    add_target(i + 1, "INSERT", line)
                elif prev == "MERGE":
                    add_target(i + 1, "MERGE", line)
                elif seen_output:                  # OUTPUT inserted.* INTO audit_table
                    add_target(i + 1, "INSERT", line)
                elif st.head == "SELECT":
                    add_target(i + 1, "SELECT INTO", line)
            elif up == "INSERT" and nxt is not None:
                if st.head == "MERGE":
                    merge_actions.add("INSERT")
                    if nxt.text == "(" and targets:
                        end = _match_paren(toks, i + 1)
                        for k in range(i + 2, end):
                            if toks[k].kind == "id":
                                writes_cols.append((0, toks[k].text.lower()))
                                consumed.add(k)
                elif nxt.up != "INTO":
                    add_target(i + 1, "INSERT", line)
            elif up == "UPDATE" and nxt is not None:
                if st.head == "MERGE":
                    merge_actions.add("UPDATE")
                elif nxt.up != "SET" and nxt.text != "(" and nxt.up != "STATISTICS" and prev != "FOR":
                    add_target(i + 1, "UPDATE", line)
            elif up == "DELETE":
                if st.head == "MERGE":
                    merge_actions.add("DELETE")
                elif nxt is not None and nxt.up != "FROM" and (nxt.is_name or nxt.quoted):
                    add_target(i + 1, "DELETE", line)
            elif up == "MERGE" and nxt is not None and nxt.up != "INTO":
                add_target(i + 1, "MERGE", line)
            elif up == "TRUNCATE" and nxt is not None and nxt.up == "TABLE":
                add_target(i + 2, "TRUNCATE", line)
            elif up in ("EXEC", "EXECUTE") and nxt is not None:
                j = i + 1
                if toks[j].kind == "var" and j + 1 < n and toks[j + 1].text == "=":
                    j += 2
                if j < n and toks[j].kind == "id" and toks[j].up != "AS":
                    parts, k = _read_name(toks, j)
                    consumed.update(range(j, k))
                    if parts[-1].lower() != "sp_executesql":
                        key, ext = cat.resolve(parts, schema)
                        res.calls.append((key or ext or ".".join(parts), line, bool(key)))

        # Resolve targets (may be aliases defined in a later FROM clause).
        target_keys = {}
        for parts, op, line, idx in targets:
            key = None
            if len(parts) == 1 and parts[0].lower() in aliases and parts[0].lower() not in cte_names:
                ks = aliases[parts[0].lower()]
                key = next(iter(ks)) if len(ks) == 1 else None
            if not key:
                key, ext = resolve(parts)
                if key in ("cte", "temp"):
                    continue
                if ext:
                    res.external.append((ext, line, op))
                    continue
                if not key:
                    if not (len(parts) == 1 and parts[0].lower() in derived):
                        res.unresolved.append((".".join(parts), line))
                    continue
            target_keys[idx] = (key, op, line)

        # Column references.
        stmt_keys = {k for k, _ in sources} | {v[0] for v in target_keys.values()}
        colrefs = {}     # start index -> (key, col, end index)
        by_end = {}
        i = 0
        while i < n:
            t = toks[i]
            if i in consumed or t.kind != "id" or not t.is_name or (i and toks[i - 1].text == "."):
                i += 1
                continue
            parts, j = _read_name(toks, i)
            if j < n and toks[j].text == "(":
                if len(parts) >= 2:       # schema.fn(...) - scalar function call
                    key, _ = cat.resolve(parts, schema)
                    if key and cat.objects[key]["type"] in ("FN", "IF", "TF"):
                        res.calls.append((key, t.line, True))
                i = j
                continue
            if i and toks[i - 1].up == "AS":
                i = j
                continue
            ref = self._resolve_col(parts, aliases, derived, stmt_keys)
            if ref:
                colrefs[i] = (ref[0], ref[1], j - 1)
                by_end[j - 1] = (ref[0], ref[1], i)
            i = j

        cols_used = {}
        for key, col, _ in colrefs.values():
            cols_used.setdefault(key, set()).add(col)

        # Columns written by UPDATE ... SET col = / MERGE ... UPDATE SET col =
        def is_assignment(start, end):
            return (start in set_region and end + 1 < n and toks[end + 1].text == "="
                    and (toks[start - 1].up == "SET" or toks[start - 1].text == ","))

        set_written = {}
        for start, (key, col, end) in colrefs.items():
            if is_assignment(start, end):
                set_written.setdefault(key, set()).add(col)
        # Unqualified SET columns that could not be resolved uniquely belong to the target.
        update_targets = [key for key, op, _ in target_keys.values() if op in ("UPDATE", "MERGE")]
        if len(update_targets) == 1:
            key = update_targets[0]
            for k in set_region:
                if k not in colrefs and toks[k].kind == "id" and is_assignment(k, k):
                    name = toks[k].text.lower()
                    if name in cat.colmap.get(key, {}):
                        set_written.setdefault(key, set()).add(name)

        # Joins: colref = colref outside SET assignments; also colref IN (SELECT colref FROM ...)
        for j, t in enumerate(toks):
            if t.text == "=" and (j - 1) not in set_region:
                left = by_end.get(j - 1)
                right = colrefs.get(j + 1)
                if left and right and left[0] != right[0]:
                    res.joins.append(((left[0], left[1]), (right[0], right[1]), t.line, dynamic))
            if t.up == "IN" and (j - 1) in by_end and j + 3 < n and toks[j + 1].text == "(" \
                    and toks[j + 2].up == "SELECT":
                left = by_end[j - 1]
                right = colrefs.get(j + 3)
                if right and left[0] != right[0]:
                    res.joins.append(((left[0], left[1]), (right[0], right[1]), t.line, dynamic))

        # Emit references.
        written_by_target = {}
        for tidx, col in writes_cols:
            if tidx in target_keys:
                written_by_target.setdefault(target_keys[tidx][0], set()).add(col)
        target_set = {}
        for idx, (key, op, line) in target_keys.items():
            if op == "SELECT INTO":
                op = "INSERT"
            r = Ref(key, op, line, dynamic)
            r.cols_written = written_by_target.get(key, set()) | set_written.get(key, set())
            if op == "INSERT" and not r.cols_written and key not in written_by_target:
                r.detail = "all columns"
            if op == "MERGE" and merge_actions:
                r.detail = "/".join(sorted(merge_actions))
            r.cols_read = cols_used.get(key, set()) - r.cols_written
            res.refs.append(r)
            target_set[key] = op
        # SELECT * marks every source; alias.* marks only that table (or all, if the alias is a CTE/subquery).
        star_all, star_keys = False, set()
        for k, t in enumerate(toks):
            if t.text != "*" or not k:
                continue
            if toks[k - 1].text == "." and k > 1:
                qual = toks[k - 2].text.lower()
                if qual in aliases and len(aliases[qual]) == 1:
                    star_keys |= aliases[qual]
                else:
                    star_all = True
            elif toks[k - 1].up in ("SELECT", "DISTINCT") or toks[k - 1].text == "," or (k > 1 and toks[k - 2].up == "TOP"):
                star_all = True
        for key, line in sources:
            if target_set.get(key) in ("UPDATE", "DELETE", "MERGE"):
                continue       # the FROM clause naming the target of UPDATE/DELETE
            res.refs.append(Ref(key, "SELECT", line, dynamic, cols_read=cols_used.get(key, set()),
                                detail="SELECT *" if star_all or key in star_keys else ""))

    def _resolve_col(self, parts, aliases, derived, stmt_keys):
        cat = self.cat
        col = parts[-1].lower()
        if len(parts) >= 2:
            qual = parts[-2].lower()
            if qual in derived:
                return None
            keys = aliases.get(qual)
            if not keys:
                key, _ = cat.resolve(parts[:-1])
                keys = {key} if key else set()
            hits = [k for k in keys if col in cat.colmap.get(k, {})]
            return (hits[0], col) if len(hits) == 1 else None
        hits = [k for k in stmt_keys if col in cat.colmap.get(k, {})]
        return (hits[0], col) if len(hits) == 1 else None
