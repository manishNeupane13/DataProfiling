# Samsara fleet monitoring: runnable backend project

Python 3.11+; standard library and SQLite only. Real Samsara REST integration,
authenticated local dashboard APIs, telemetry history, signed webhook ingestion,
incident processing, and a durable local notification sink.

## Quick start — no Samsara credentials

```bash
python3 -m fleet --demo
```

The local API requires `Authorization: Bearer demo-only-key` in demo mode:

```bash
curl -H 'Authorization: Bearer demo-only-key' http://127.0.0.1:8000/analytics
curl -H 'Authorization: Bearer demo-only-key' 'http://127.0.0.1:8000/vehicles?state=moving&sort=speed_desc&limit=20'
curl -H 'Authorization: Bearer demo-only-key' http://127.0.0.1:8000/analytics/history
curl -H 'Authorization: Bearer demo-only-key' http://127.0.0.1:8000/incidents
curl -H 'Authorization: Bearer demo-only-key' http://127.0.0.1:8000/notifications
curl -H 'Authorization: Bearer demo-only-key' http://127.0.0.1:8000/operations
```

Three synthetic trucks update every five seconds. Every 30 seconds a synthetic
harsh-braking webhook is submitted twice to demonstrate duplicate-safe processing.
A background worker writes one incident and one local notification. Nothing is
sent to a driver, email service, or external application. Ctrl-C stops the app.

## Real-world situations covered

| Situation | Implementation |
|---|---|
| Truck sends a GPS-only update | Preserve its last-known engine state |
| Old reading arrives late | Retain it in history without overwriting newer state |
| Application crashes during a page | State, telemetry history and feed cursor commit together |
| Dashboard refreshes repeatedly | Read stored state; do not call Samsara per dashboard request |
| Driver switches after a braking incident | Match assignment at incident time |
| Assignment is later corrected | Incident reads use the latest fetched assignment window |
| Webhook is delivered twice | Deduplicate event ID; incident key also collapses repeated deliveries |
| Old alert arrives after an outage | Record it, but suppress urgent notification after five minutes |
| Notification worker fails | Durable outbox remains pending and retries; five failures become `failed` |
| Unsupported webhook arrives | Retain it as `unsupported`, visible in operational counts |
| Invalid supported alert arrives | Quarantine instead of inventing missing fields |
| Vehicle data becomes stale | Display unknown state and exclude it from live speed aggregates |

## System design and code reading order

```text
Samsara REST stats ─→ validation ─→ telemetry history + current state + cursor
Samsara assignments ─→ complete paginated 24-hour window ─→ interval lookup
Signed webhook ─→ durable inbox ─→ incident + transactional outbox
Outbox worker ─→ idempotent local notification table
Authenticated HTTP API ─→ current state / historical analytics / incidents
```

1. `fleet/client.py`: API adapter, token handling, timeouts, retries, pagination.
2. `fleet/store.py`: validation, out-of-order merge, SQLite transactions, latest state.
3. `fleet/operations.py`: history schema, inbox, deduplication, temporal join, outbox.
4. `fleet/security.py`: verify HMAC over raw bytes and reject stale delivery timestamps.
5. `fleet/__main__.py`: worker lifecycle, demo, authenticated HTTP API.
6. `tests/test_pipeline.py`, `tests/test_operations.py`: correctness and recovery cases.
7. `tests/smoke_http.py`: actual HTTP authentication/webhook/analytics integration test.

## Live mode

Set these environment variables using your secret manager or a private shell prompt:

- `SAMSARA_API_TOKEN`: token with Read Vehicle Statistics and Read Assignments.
- `DASHBOARD_API_KEY`: a separate strong random secret for your application's API.
- `SAMSARA_WEBHOOK_SECRET`: optional base64 secret supplied by Samsara; required
  to accept webhook requests.

Then run:

```bash
python3 -m fleet --assignment-type driverApp
```

Choose the driver assignment source used by your fleet (`driverApp`, `HOS`, etc.).
This sample targets the US host `https://api.samsara.com`. It reads all vehicles
visible to the token; use appropriate token scope. Live and demo modes cannot
share a database. Only run one ingestion process per database. Keep the database
associated with one organization/token scope and assignment source.

Live mode polls `/fleet/vehicles/stats/feed?types=gps,engineStates`. It continues
pagination and waits five seconds when caught up. Assignment fetching refreshes
an authoritative rolling 24-hour window every 30 seconds after completion; this
simple backfill strategy favors clarity over minimizing assignment API traffic.
For larger fleets use a measured refresh/backfill strategy. No live credentials
were used during validation of this project.

## Webhooks

Expose `POST /webhooks/samsara` through your own HTTPS reverse proxy. The sample
itself binds only to localhost. Configure the webhook in Samsara and set its
base64 secret in the environment. The receiver checks the HMAC signature, checks
a five-minute delivery-timestamp tolerance, and persists the raw JSON before 202.
Retries outside that tolerance require a newly signed timestamp or reconciliation;
this sample does not implement an API-based safety-event backfill.

**Explicit supported payload:** legacy Webhooks 1.0 `Alert` with
`event.device.id`, `event.alertConditionId`, and `event.startMs`; `Ping` is accepted.
`AlertIncident` v2 and other event types are durably retained as unsupported.
Add an adapter against the exact event schema before using v2 for analytics.
Signature verification alone does not make different webhook versions compatible.

The incident key is vehicle + condition + start timestamp. This assumes that tuple
identifies one legacy incident; migrate to a provider incident ID when a supported
schema offers one. The HarshEvent category is not automatically split into braking
versus acceleration by parsing human-readable text.

## API contract

All GET routes except `/health` require the dashboard bearer token.

| Route | Result |
|---|---|
| `/health` | Feed readiness (503 when stale), assignment observation time |
| `/vehicles?state=moving&sort=speed_desc&limit=20` | Filtered current fleet; sort also supports `vehicle_id` |
| `/analytics` | Counts by state, average fresh vehicle speed, speed-threshold count |
| `/analytics/history?from=...&to=...` | Unix-second half-open interval; default past hour; maximum seven days |
| `/incidents?limit=100&offset=0` | Event-time driver attribution; latest incidents first |
| `/notifications?limit=100&offset=0` | Delivered local notifications |
| `/operations` | Inbox/outbox status counts, history size, assignment coverage |

Historical speed is **sample-weighted**, not time-weighted or a trip average.
Missing speed is excluded; zero speed is included. Current analytics average the
latest fresh speed per vehicle. Odometer-based trip metrics are not implemented.

## Rules and honest limitations

- Current GPS must be within 60 seconds and the feed must have succeeded within
  60 seconds. This intentionally conservative teaching rule can classify normally
  idle/off vehicles as unknown when their GPS updates less often. Tune freshness
  per vehicle state and signal for your actual fleet before relying on idle counts.
- Speed >3 mph is moving; >1 through 3 mph is slow-moving. At <=1 mph, last-known
  engine state determines the stationary label. This is not trip detection, dwell
  time, or hysteresis. Engine-on stationary does not prove prolonged idling.
- The speed flag is a configured threshold (default 65 mph), not a road-limit
  violation. Use `--speed-threshold` to change it.
- Historical readings are deduplicated by vehicle/metric/time. Same-time corrections
  are first-write-wins. Add source revision semantics if corrections are required.
- Assignment coverage is the latest rolling window only. Outside it, incident
  attribution is explicitly `outside_coverage`; this is not a permanent assignment
  archive. Current assignments expire after 90 seconds without a successful refresh.
- Notifications deliberately omit driver identity; later driver corrections are
  visible on incident reads and cannot silently blame a different current driver.
- Local notification delivery is transactional and idempotent. A remote provider
  would need receiver-side idempotency keys; this does not promise exactly-once
  email/SMS. No external messages or Samsara tag changes are implemented.
- Telemetry, inbox and incident history are retained without automatic deletion.
  Define retention, disk monitoring and backup policy before continuous deployment.
- SQLite and the standard-library HTTP server keep this example easy to inspect.
  For public deployment add a production server/proxy, TLS, request limits, access
  controls, centralized observability and load tests. This is single-organization,
  not a tenant-isolated commercial platform.

## Validation

```bash
python3 -m unittest discover -s tests -v
python3 tests/smoke_http.py
```

Smoke testing temporarily binds localhost port 18761 and uses synthetic data.
Tests cover duplicate/late readings, transactional rollback, driver handovers and
corrections, webhook tampering/replay, unsupported events, delayed alerts, and retry
recovery. No tests prove actual Samsara connectivity or performance for your fleet.

## Official contracts

- https://developers.samsara.com/reference/getvehiclestatsfeed
- https://developers.samsara.com/reference/getdrivervehicleassignments
- https://developers.samsara.com/docs/webhooks
- https://developers.samsara.com/docs/webhooks-v1-reference
