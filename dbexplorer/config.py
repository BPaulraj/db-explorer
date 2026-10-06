import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path


def _csv(value):
    return [v.strip().lower() for v in value.replace("\n", ",").split(",") if v.strip()]


@dataclass
class Config:
    connection: dict
    min_prefix_coverage: float = 0.6
    min_prefix_length: int = 2
    max_prefix_length: int = 4
    generic_columns: set = field(default_factory=set)
    max_table_fraction: float = 0.5
    max_peer_group: int = 6
    prefix_overrides: dict = field(default_factory=dict)
    logical_aliases: dict = field(default_factory=dict)  # alias -> canonical
    sample_rows: int = 5000
    query_timeout: int = 30
    validate_min: int = 20
    validate_max: int = 95
    profile_sample_rows: int = 20000
    value_list_max: int = 30
    example_values: int = 3
    profile_timeout: int = 60
    skip_tables: list = field(default_factory=list)
    sensitive_include: set = field(default_factory=set)
    sensitive_exclude: set = field(default_factory=set)
    include_source: bool = True
    max_source_lines: int = 600


def load_config(path="config.ini"):
    cp = configparser.ConfigParser(inline_comment_prefixes=(";",))
    path = Path(path)
    if path.exists():
        cp.read(path, encoding="utf-8")
    elif Path("config.example.ini").exists():
        # Analysis-only commands still work (with defaults) before config.ini exists.
        cp.read("config.example.ini", encoding="utf-8")

    conn = dict(cp["connection"]) if cp.has_section("connection") else {}
    if not conn.get("password"):
        conn["password"] = os.environ.get("DBX_PASSWORD", "")

    a = cp["analysis"] if cp.has_section("analysis") else {}
    v = cp["validation"] if cp.has_section("validation") else {}
    p = cp["profiling"] if cp.has_section("profiling") else {}
    s = cp["sensitive"] if cp.has_section("sensitive") else {}
    x = cp["export"] if cp.has_section("export") else {}

    aliases = {}
    if cp.has_section("logical_aliases"):
        for canonical, others in cp["logical_aliases"].items():
            for alias in _csv(others):
                aliases[alias] = canonical.lower()

    overrides = {}
    if cp.has_section("prefix_overrides"):
        for table, prefix in cp["prefix_overrides"].items():
            overrides[table.lower()] = "" if prefix.strip() == "-" else prefix.strip().lower()

    return Config(
        connection=conn,
        min_prefix_coverage=float(a.get("min_prefix_coverage", 0.6)),
        min_prefix_length=int(a.get("min_prefix_length", 2)),
        max_prefix_length=int(a.get("max_prefix_length", 4)),
        generic_columns=set(_csv(a.get("generic_columns", ""))),
        max_table_fraction=float(a.get("max_table_fraction", 0.5)),
        max_peer_group=int(a.get("max_peer_group", 6)),
        prefix_overrides=overrides,
        logical_aliases=aliases,
        sample_rows=int(v.get("sample_rows", 5000)),
        query_timeout=int(v.get("query_timeout", 30)),
        validate_min=int(v.get("min_confidence", 20)),
        validate_max=int(v.get("max_confidence", 95)),
        profile_sample_rows=int(p.get("sample_rows", 20000)),
        value_list_max=int(p.get("value_list_max", 30)),
        example_values=int(p.get("example_values", 3)),
        profile_timeout=int(p.get("query_timeout", 60)),
        skip_tables=_csv(p.get("skip_tables", "")),
        sensitive_include=set(_csv(s.get("include", ""))),
        sensitive_exclude=set(_csv(s.get("exclude", ""))),
        include_source=str(x.get("include_source", "yes")).lower() in ("yes", "true", "1"),
        max_source_lines=int(x.get("max_source_lines", 600)),
    )
