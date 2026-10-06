"""A tiny stand-in for a pyodbc cursor that evaluates the profiler's queries over in-memory rows."""
import re

ROWS = {
    "clients": [{"clclient": f"C{i:04d}", "clname": f"Client {i}", "clbranch": ["CHN", "MUM", "DEL"][i % 3],
                 "clstatus": "A" if i % 10 else "I", "clpan": f"ABCP{chr(65 + i % 26)}{1000 + i}{chr(70 + i % 5)}",
                 "clmobile": f"98{i:08d}"} for i in range(1, 41)],
    "stocks": [{"smstock": s, "smname": n, "smisin": f"INE{i:03d}A01011", "smexchange": "NSE"}
               for i, (s, n) in enumerate([("INFY", "Infosys"), ("TCS", "Tata Consultancy"), ("RELI", "Reliance")])],
    "trade": [{"trdid": i, "trdclient": f"C{1 + i % 40:04d}", "trdstock": ["INFY", "TCS", "RELI"][i % 3],
               "trdquantity": -5 if i == 7 else 10 * (i % 5 + 1), "trdcosts": 100.0 * i, "trddate": None if i % 9 == 0 else f"2026-09-{1 + i % 28:02d}",
               "trdtype": "B" if i % 3 else ("S" if i % 2 else "X"), "trdexchange": "NSE"} for i in range(1, 101)],
}

AGG = re.compile(r"\[(\w+)\][^,]*? AS (nul|dis|mnl|mxl|blk|mn|mx|zer|neg)_(\d+)")
VALUES = re.compile(r"SELECT (\d+) AS col, CAST\(\[(\w+)\] AS nvarchar\(200\)\) AS v, COUNT\(\*\) AS n FROM s GROUP BY")
SAMPLE = re.compile(r"SELECT TOP 50 (\d+) AS col, CAST\(\[(\w+)\] AS nvarchar\(200\)\) AS v FROM s WHERE")


class FakeCursor:
    def __init__(self, data=ROWS):
        self.data, self.executed = data, []

    def execute(self, sql):
        self.executed.append(sql)
        table = re.search(r"FROM \[dbo\]\.\[(\w+)\]", sql).group(1)
        rows = self.data.get(table, [])
        self._all = []
        if VALUES.search(sql):
            for col, name in VALUES.findall(sql):
                counts = {}
                for r in rows:
                    v = None if r[name] is None else str(r[name])
                    counts[v] = counts.get(v, 0) + 1
                self._all += [(int(col), v, n) for v, n in counts.items()]
            self.description = [("col",), ("v",), ("n",)]
            return
        if SAMPLE.search(sql):
            for col, name in SAMPLE.findall(sql):
                vals = sorted({str(r[name]) for r in rows if r[name] is not None})[:50]
                self._all += [(int(col), v) for v in vals]
            self.description = [("col",), ("v",)]
            return
        out = {"n_rows": len(rows)}
        for name, kind, idx in AGG.findall(sql):
            vals = [r[name] for r in rows]
            nn = [v for v in vals if v is not None]
            out[f"{kind}_{idx}"] = {
                "nul": len(vals) - len(nn), "dis": len(set(nn)),
                "mnl": min((len(str(v)) for v in nn), default=None), "mxl": max((len(str(v)) for v in nn), default=None),
                "blk": sum(1 for v in nn if str(v).strip() == ""), "mn": min(nn, default=None), "mx": max(nn, default=None),
                "zer": sum(1 for v in nn if v == 0), "neg": sum(1 for v in nn if isinstance(v, (int, float)) and v < 0),
            }[kind]
        self.description = [(k,) for k in out]
        self._one = tuple(out.values())

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all
