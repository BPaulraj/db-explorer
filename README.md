# db-explorer

Maps an undocumented SQL Server database:

- **Relationships between tables**, even where no foreign keys are declared. It understands per-table column prefixes such as `trdclient` / `vlclient` → `client`.
- **Which procedures, views, triggers and functions touch each table**, and how (Insert / Update / Delete / Merge / Truncate / Select), down to column level, for regression impact analysis.

Everything is read-only. The tool queries system catalog views, plus (optionally) small `NOLOCK` sampled queries to confirm relationships against real data.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy config.example.ini config.ini     # then edit server / database / auth
```

You need a SQL Server ODBC driver. "ODBC Driver 17/18 for SQL Server" is best, but the built-in legacy "SQL Server" driver also works. Your login needs `VIEW DEFINITION` to read procedure source.

## Run

```powershell
.\.venv\Scripts\python -m dbexplorer all                     # extract + analyze + knowledge base + reports
.\.venv\Scripts\python -m dbexplorer all --profile --validate  # also profile column data and check relationships
.\.venv\Scripts\python -m dbexplorer profile [TABLE]         # (re)profile all tables, or one
.\.venv\Scripts\python -m dbexplorer validate                # check inferred relationships against data
.\.venv\Scripts\python -m dbexplorer export                  # regenerate knowledge/ (keeps your notes)
.\.venv\Scripts\python -m dbexplorer report                  # rebuild HTML/Excel after any of the above
.\.venv\Scripts\python -m dbexplorer impact trade --column trdcosts
```

`profile` and `validate` save progress after every table, so they can be interrupted and resumed. Use `--fresh` to start over.

Outputs, in `output/`:

| File | What |
|---|---|
| `db_explorer.html` | Interactive explorer (single file, works offline). It has five tabs: relationship graph, impact analysis, CRUD matrix, procedure details, and a review list. Deep links work, e.g. `db_explorer.html#table=dbo.trade&view=impact&column=trdcosts`. |
| `db_explorer.xlsx` | The same data as sheets: Relationships, CRUD Matrix, Table Usage, Calls, Columns (with logical names), Review Needed, Logical Keys. |
| `analysis.json` | Everything, machine-readable. |

After the first `extract`, `analyze` and `report` work offline from `cache/catalog.json`. Re-run `extract` when the database changes.

## How relationships are found

Each candidate relationship gets a confidence score from 0 to 100, plus the evidence behind it:

| Evidence | Score |
|---|---|
| Declared foreign key | 100 |
| The join appears in procedure/view code (`ON t.trdclient = v.vlclient`, `WHERE a.x = b.y`, `x IN (SELECT y …)`) | 55–85, more uses → higher |
| Same logical name after stripping each table's prefix, pointing to a table where that column is the PK/unique key, or the table named after it (`clients`, `clientmaster`) | ~50–58 |
| Logical name equals another table's key column (`ldgtrdid` → `trdid` = `trade.trdid`) | ~58 |
| Same logical name, no owning table ("peer") | ~30–38 |
| Multiple kinds of evidence | bonus |
| `validate`: % of sampled child values found in the parent | up to +15 or −25 |

The arrow in the graph points from child to parent (the lookup table).

## How the procedure analysis works

Each module's T-SQL is tokenized and split into statements; semicolons are not required. For every table reference it records the operation, the columns written and read (resolving aliases and unqualified columns against the catalog), and the line numbers. It handles:

- CTEs, derived tables, `UPDATE alias … FROM`, `DELETE alias FROM … JOIN`, MERGE (with its INSERT/UPDATE/DELETE actions) and `OUTPUT … INTO`;
- `SELECT … INTO`, comma joins, table hints, cursors, temp tables and table variables;
- synonyms, cross-database names, `EXEC` calls and scalar/table function calls.

For impact, the tool also follows:

- views (and views of views) and table functions;
- callers of affected procedures, transitively;
- triggers on the table.

Known limits (listed in the **Review** tab):

- **Dynamic SQL** (`EXEC(@sql)`, `sp_executesql`): table names written inside string literals are picked up and marked "dynamic". Names built at runtime can't be detected.
- **Encrypted modules**: only `sys.sql_expression_dependencies` is available, shown as `?` (referenced, operation unknown).
- **Column-level impact** errs on the safe side. `SELECT *`, `INSERT` without a column list and catalog-only references are always included.

## SQL Agent jobs (Batch jobs tab, `knowledge/jobs/`)

`extract` reads jobs, steps, schedules and the last 30 days of run history from `msdb`. This needs the **SQLAgentReaderRole** role in msdb; without it, everything else still works and the reason is shown. Only jobs that touch this database are kept. For each job you get:

- **schedules in plain English** ("Every weekday (Mon-Fri), every 15 minutes between 09:15 and 15:30") and a day timeline;
- **step flow** (on success / on failure actions, retries) and each step's command;
- **what the job changes**: the procedures it runs, followed through nested `EXEC` calls, then the tables written and read, and the triggers fired;
- **batch risk notes**:
  - a step whose failure is reported as success (hidden failures);
  - a step that continues after failure;
  - steps that can never run;
  - recent failures;
  - runs longer than the repeat interval (overlap);
  - SSIS/CmdExec/PowerShell steps (integration points);
  - dynamic SQL;
  - disabled jobs or schedules, or no schedule at all.

The jobs also show up in other views: a table's impact analysis lists the jobs that change it, a procedure shows "Run by scheduled jobs", and procedures run in batch get a higher risk score.

## Error catalog (procedure pages, Excel "Error Catalog")

Every `RAISERROR`, `THROW`, rethrow and non-zero `RETURN` code in every procedure, trigger and function is listed with:

- **message**: literal text, resolved from a variable (`SET @msg = '...'`), or looked up in `sys.messages` for numbered messages;
- **number, severity and line**;
- **the condition that raises it**, e.g. `@qty <= 0`, `NOT EXISTS (SELECT … clstatus = 'A')`, an ELSE branch shown as `NOT (…)`, or "inside CATCH".

This gives you the expected result for every negative test. It also flags **suspicious severities**: severity ≤ 10 is informational, so the application never sees the error, and severity ≥ 20 without `WITH LOG` fails.

## Data profiling (`profile`)

One aggregate query per table runs over a sample (`TABLESAMPLE` + `TOP`, `NOLOCK`, timeout; small tables are read whole). It collects:

- null %, distinct count, min/max (numbers, dates), lengths and blanks (text), zeros and negatives (numbers);
- the **full value list with frequencies** for low-cardinality columns. These are your status/type codes and equivalence classes;
- **value formats** as shapes, e.g. `ABCPX1234X` becomes `AAAAA9999A`;
- a few example values for non-sensitive columns, for generating test data.

**Sensitive data.** Columns are flagged by name (PAN, Aadhaar, mobile, email, bank account, IFSC, DOB, address, client name, demat ID, credentials…) and by the format of their values. For a flagged column, only the category and format are stored. Values, examples and min/max are dropped before anything is written to disk. Review the list in the Review tab and adjust `[sensitive] include / exclude` in config.ini. Name-based rules are re-applied on every `analyze`, without re-profiling.

## Knowledge base and AI context (`export` → `knowledge/`)

```
knowledge/
  README.md            index: glossary (yours), procedures ranked by test risk, tables with purpose
  tables/<table>.md    columns + meaning + rules + data profile, relationships, who reads/writes it,
                       triggers, test considerations, handy queries (joins, orphan checks)
  procedures/<p>.md    parameters + defaults, data operations by line, call chain, triggers fired,
                       structure/risk, review flags, source
  objects/<v|tr|fn>.md views, triggers, functions
  jobs/<job>.md        schedule, steps, procedures run (nested), tables changed, batch risk notes
  prompts/*.md         prompt templates: test cases, test data, reconciliation checks, explain, regression scope,
                       batch job testing
  context.json         everything above in one masked JSON (for AI tools / retrieval)
```

- **Business notes**: every file has a section between `NOTES:START` / `NOTES:END` markers. Write domain knowledge there: meaning of codes, which screen or batch uses it, known issues. It is **kept when the files are regenerated**, and it also shows in the HTML explorer and in `context.json`. Commit `knowledge/` to your team's repository.
- **Test considerations** are generated per table from the schema and data: CHECK constraints, mandatory columns, defaults, unique keys, code values to cover, negative values, nullable references, orphans, triggers, write hotspots, sensitive data.
- **Risk score** per procedure combines complexity (branches, loops, cursors), number of tables written, dynamic SQL, missing TRY/CATCH, callers, triggers fired and recent modification. Use it to order regression testing.
- **With AI**: attach the relevant `tables/*.md` and `procedures/*.md` files and use a template from `prompts/`. The files contain no sensitive values, but they do describe your schema and business logic. Use a local model (e.g. Ollama) or one your company has approved.

## Tuning (config.ini)

- `[prefix_overrides]`: fix a table whose prefix was detected wrong. Check the prefix list in the Review tab first.
- `[logical_aliases]`: e.g. `client = clientcode, clientid, cliid`, when different tables name the same key differently.
- `generic_columns`: logical names that should never be treated as join keys (quantity, status, audit columns…).

## Tests

```powershell
.\.venv\Scripts\python -m pytest tests
```

`tests/make_fixture.py` builds a synthetic stock-broker catalog, and `tests/make_profile.py` profiles it through a fake cursor (`tests/fake_db.py`). With these, the whole pipeline runs without a database:

```powershell
python tests/make_fixture.py; python tests/make_profile.py
python -m dbexplorer analyze --catalog tests/fixture/catalog.json --out tests/fixture/out
python -m dbexplorer export  --catalog tests/fixture/catalog.json --out tests/fixture/out --kb tests/fixture/knowledge
python -m dbexplorer report  --catalog tests/fixture/catalog.json --out tests/fixture/out --kb tests/fixture/knowledge
```
