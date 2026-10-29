# Local Data Workbench

A locally hosted Streamlit frontend and FastAPI backend for profiling, validating,
mapping, matching, and reconciling CSV and Excel files. Workbench storage uses files
and JSON only, with no database. The existing independent Samsara example is
preserved under `fleet/`; its original instructions are in `fleet/README.md`.

## Start

Use Python 3.11 or newer from this repository directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m workbench.sample_data
```

Start the backend in one terminal:

```bash
source .venv/bin/activate
python -m uvicorn workbench.api:app --host 127.0.0.1 --port 8000 --no-access-log
```

Start the frontend in a second terminal, also from this directory:

```bash
source .venv/bin/activate
python -m streamlit run workbench/ui.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
```

Open [the workbench](http://localhost:8501). Interactive API documentation is at
[localhost:8000/docs](http://localhost:8000/docs). Stop each process with Ctrl-C.
The existing fleet example also uses port 8000; stop it before starting this backend.

`WORKBENCH_DATA` changes the backend storage directory (default `local_data/`).
`WORKBENCH_API` changes the frontend API URL (default `http://127.0.0.1:8000`).
Run one backend worker: review writes are atomic and serialized within that process.
Bind to loopback; this local application has no authentication or multiuser isolation.

## Workflow

1. **Upload & Datasets:** upload multiple CSV/XLSX files. Each worksheet becomes
   its own dataset. Preview up to 100 rows; processing uses the entire sheet.
2. **Data Profiling:** inspect nulls, duplicates, inferred types, value frequencies,
   mixed numeric/text values, date-shape variations, identifiers and numeric summaries.
3. **Column Mapping:** choose two sheets, inspect column-name suggestions, override
   every mapping as needed, and save. Saved mappings reload after restart.
4. **Data Validation:** use the rule form or advanced JSON to run multiple rules.
   Reports retain the source filename, sheet, Excel-style row number, column,
   offending value, and rule. Reports are saved for export.
5. **Matching Configuration:** choose mapped identity attributes and weights.
   Use Name plus License for stronger evidence; exclude Truck and Safety Officer.
6. **Reconciliation Results:** inspect matches, pending candidates, unmatched rows,
   duplicate candidates, invalid values, assignment conflicts and potential outdated values.
7. **Conflict Review:** compare candidates side by side, approve/reject or return
   them to pending, and record a rationale. Rejected matches can be reopened.
8. **Export Reports:** generate an Excel workbook or ZIP of CSV reports.

Try the `Drivers` sheets in `samples/historical_2024.xlsx` and
`samples/current_2025.xlsx`. Map Driver Name → Full Name, Truck → Vehicle Number,
License → License Number, and Start Date → Effective Date. Fuzzy matching on names
at 70–80 produces reviewable name variants; duplicate Aisha records demonstrate
ambiguity. Vehicle assignment changes are field conflicts, never identity evidence.

Example advanced validation:

```json
[
  {"column":"Driver Name","required":true,"unique":true},
  {"column":"License","regex":"^IL[0-9]{5}$"},
  {"column":"Start Date","type":"date"}
]
```

Rules support `type` string/integer/float/boolean/date, `required`, `unique`,
`regex`, `allowed`, `min`, `max`, and `compare_to` with `operator`
`eq/ne/lt/le/gt/ge`. Ordered comparisons use numeric conversion. Nulls are accepted
unless required. CSV values retain their source text so invalid formats remain visible.

## Matching and interpretation

Exact compares source values; normalized matching uses Unicode normalization,
case folding, punctuation removal, and whitespace collapse. Fuzzy matching uses
RapidFuzz ratio and a weighted average. Missing identity values score zero.
All fuzzy candidates require manual approval, even at 100. Exact/normalized
matches auto-approve only at 100 with no other threshold-qualified candidate for
either record. Ambiguity is flagged when a competing candidate shares either row
and scores within the configured margin. Approval enforces one-to-one matching.
Review decisions are append-only audit entries in each run's atomically replaced JSON.

Missing/extra records mean no **approved** match yet. Pending candidates remain
unmatched until reviewed. A reference dataset is a comparison role, not a source
of truth. Upload order is never used to resolve conflicts. Outdated-record reports
are hypotheses attached to differing fields; they do not assert which source is
correct. Review notes capture the human resolution; the application does not
rewrite datasets or produce an authoritative merged master file.

## Storage and retention

`local_data/uploads/` holds byte-for-byte original uploads under generated IDs;
`datasets/` holds per-sheet JSON snapshots; `mappings/` holds mappings and validation
reports; `runs/` holds results and decision history; `reports/` holds exports.
Source files are never modified. No row contents are intentionally logged.
Exported spreadsheet cells escape formula prefixes.

The upload page has explicit retention controls, including zero days to clear all
workbench files. Retention is destructive and based on file modification time;
partial retention may remove data referenced by newer runs. Export needed reports
before applying it. There is no automatic cleanup or external data transmission.

## Tests

```bash
source .venv/bin/activate
python -m pytest workbench_tests -q
python -m unittest discover -s tests -v
```

Tests cover ingestion, profiling, validation, weighted/normalized/fuzzy matching,
ambiguity, reconciliation, decision uniqueness, Excel/CSV export and a real HTTP
server used by the frontend API client and Streamlit AppTest.

## Current limits

- Designed for small local datasets: matching evaluates every row pair in memory.
  Large datasets need candidate blocking and pagination before practical use.
- Uploads are capped at 50 MB per file; expanded XLSX size and worksheet row counts
  are not bounded. Use trusted local input files.
- CSV assumes UTF-8 and comma delimiters; XLSX is supported, not legacy XLS.
- Profiling detects mixed numeric/text and differing date shapes heuristically;
  it does not establish a locale-specific date format or business meaning.
- Assignment identity protection checks column names; users must still choose
  stable attributes responsibly when columns use other names.
- Conflict review approves record links and records notes; it does not merge or
  edit field values. Possible outdated records require human source verification.
- UI end-to-end testing covers upload listing and profiling via HTTP; the full
  matching/review/export workflow is tested through FastAPI integration tests.
