import pyodbc

DRIVER_PREFERENCE = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
]


def pick_driver(requested):
    if requested and requested.lower() != "auto":
        return requested
    installed = set(pyodbc.drivers())
    for name in DRIVER_PREFERENCE:
        if name in installed:
            return name
    raise RuntimeError(f"No SQL Server ODBC driver found. Installed drivers: {sorted(installed)}")


def connect(conn_cfg, timeout=None):
    if not conn_cfg.get("server") or not conn_cfg.get("database"):
        raise RuntimeError("Set server and database in config.ini ([connection] section).")
    driver = pick_driver(conn_cfg.get("driver", "auto"))
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={conn_cfg['server']}",
        f"DATABASE={conn_cfg['database']}",
    ]
    if driver != "SQL Server":
        parts.append("ApplicationIntent=ReadOnly")
    if conn_cfg.get("auth", "windows").lower() == "sql":
        parts += [f"UID={conn_cfg.get('username', '')}", f"PWD={conn_cfg.get('password', '')}"]
    else:
        parts.append("Trusted_Connection=yes")
    if driver.startswith("ODBC Driver"):
        parts.append(f"Encrypt={conn_cfg.get('encrypt', 'no')}")
        if conn_cfg.get("trust_server_certificate", "yes").lower() in ("yes", "true", "1"):
            parts.append("TrustServerCertificate=yes")
    cn = pyodbc.connect(";".join(parts), autocommit=True, timeout=15)
    if timeout:
        cn.timeout = timeout
    return cn, driver


def rows(cursor, sql, *params):
    cursor.execute(sql, *params)
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]
