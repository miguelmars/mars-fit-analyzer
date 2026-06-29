# EPOCH P0 — Canonical Athlete Timeline + Activity Ingestion (implementation report)

Date: 2026-06-27 · Updated: 2026-06-28 · Status: **P0 foundation built, mounted in app, tested locally, NOT deployed by Codex.**
Scope: build the multi-event timeline foundation and activity ingestion as its first
event type (`endurance_workout`). Future layers are intentionally **not** built.

> This is additive, isolated code. It does **not** modify the `/app` PWA or the existing
> `clean_sessions`/`sessions` schema. The timeline router is now mounted in `main.py`, so
> the HTTP endpoints are present once these files are deployed.

---

## 0. Corrected-spec review (2026-06-27)
Re-checked the build against the corrected task spec. All required-read files exist
(none missing). No conflicts between older docs and the current index/specs. Deltas applied:
- **Added `unavailable_fields`** to confidence so imported / derived / estimated /
  unavailable are all distinguishable (summary CSV marks GPS *unavailable*, not missing).
- **Uncertain dedup**: probable matches now get status `duplicate_uncertain` (kept, never
  deleted), not just a flag.
- **Partial-data dedup guard**: a close start time cannot become an exact duplicate when
  distance or duration is missing; it becomes reviewable instead.
- **Event-level availability**: events now expose `availability_state` as a summary of
  the detailed per-signal confidence map.
- **Raw reference clarity**: file imports record `raw_import_reference=sha256:<file_hash>`
  when no raw-file store exists yet.
- **Router mounted**: `main.py` now registers `routers.timeline`.
- **Tests**: added valid-FIT pipeline import, unknown-source fallback, unavailable-vs-missing,
  and uncertain-duplicate cases (16 → 20).

Already satisfied by the existing build (no change needed): audit+plan-before-code (done
in the bitácora and approved incrementally); modular separation (parsing / normalization /
lineage / confidence / dedup / persistence are separate modules); existing stack & no
rewrite; safe failure with preserved failed-import log; raw metadata incl. file fingerprint;
"do not build" list (no UI/onboarding/dashboards/write-back/connectors touched). Per the
v2 timeline progress report, **P0 is kept small** — no scope expansion, no new specs.

## 1. Files created (none modified)
| File | Role |
|---|---|
| `timeline_model.py` | Canonical Athlete Timeline **core** model (framework-free). |
| `ingest_parsers.py` | FIT / GPX / TCX / CSV parsers → neutral `ParsedActivity`. |
| `ingest_pipeline.py` | Normalize → confidence → dedup → import log → orchestrator (`ingest_file`). |
| `timeline_store.py` | Storage **port** + in-memory repo + additive Postgres schema/repo. |
| `tests/test_timeline_ingestion.py` | 20 ingestion validation cases (all pass). |
| `docs/EPOCH_P0_TIMELINE_INGESTION_IMPLEMENTATION.md` | This report. |
| `docs/BITACORA_CODEX.md` | Running, auditable work log. |
| **Phase 3** `routers/timeline.py` | HTTP endpoints (`POST /timeline/import`, `GET /timeline`, `GET /timeline/import-logs`) mounted in `main.py`. |
| **Phase 3** `migrations/007_timeline_events.sql` | Additive DDL (also created at runtime). |
| **Phase 3** `tools/backfill_timeline_from_clean_sessions.py` | Idempotent backfill of existing history (dry-run default). |
| **Phase 3** `tests/test_timeline_endpoint.py` | 5 endpoint tests (isolated app, in-memory repo). |

## 2. Data models created
- **`TimelineEvent`** — one common schema for ALL event types + a per-type `payload`
  dict. Base fields: `event_id, athlete_id, event_type, start_time, end_time,
  duration_sec, timezone, sport_category, source (lineage), raw_import_reference,
  availability_state, normalized_summary, confidence, payload, linked_event_ids, notes, status,
  created_at, updated_at`. Serializable (`to_dict`/`from_dict`, roundtrip-tested).
- **`SourceLineage`** — `source, source_event_id, upload_method, original_filename,
  file_type, file_hash, parser, parser_version, raw_payload_ref, detected_source,
  merged_from[], field_origins{}`.
- **`Confidence`** — `score (0..1), level, source_confidence, parsing_confidence,
  signals{signal→AvailabilityState}, imported_fields[], derived_fields[],
  estimated_fields[], missing_key_fields[], data_flags[]`.
- **`EndurancePayload`** — typed payload for `endurance_workout` (all fields optional).
- **`ParsedActivity`** — neutral parser output (intermediate, pre-canonical).
- **`ImportLog`** — per-import record for traceability + safe failure.
- Enums: `EventType, Source, AvailabilityState, ConfidenceLevel, EventStatus,
  ImportStatus, FileType, SportCategory`.

## 3. How the Canonical Athlete Timeline works
The timeline is the athlete's history as a list of **typed events**. Every event shares
the same base schema; what differs per type is the `payload`. Ingestion (and later
layers) write into this single timeline; "Athlete History" = reading the timeline,
not a separate table per type. Adding a new event type is just a new `EventType` value
+ a payload shape — **no migration, no new table** (validated by a test that stores a
`strength_session` next to an `endurance_workout`).

Storage is a **port** (`TimelineRepository`): the pipeline depends on the interface,
not on a database. P0 ships an in-memory implementation (the tested reference) and an
additive Postgres implementation for Phase 3.

## 4. Event types
- **Supported now:** `endurance_workout`.
- **Prepared for later (declared, not implemented):** `strength_session`,
  `mobility_session`, `recovery_therapy`, `nutrition_fueling`, `subjective_note`,
  `sleep_recovery_signal`, `biomarker_upload`, `planned_workout`, `unknown`.

## 5. File formats supported
`FIT` (and `.zip` containing a FIT, reusing `decode_fit.py`), `GPX`, `TCX`, `CSV`
(both a summary CSV and a per-record CSV like `decode_fit`'s output). Type is detected
by content first (magic bytes / XML root), then by extension.

## 6. Fields normalized (endurance)
sport/category, start/end time, duration, moving/elapsed time, distance, elevation
gain/loss, avg/max HR, avg/max cadence, avg/max/normalized power, avg/max speed, pace,
calories, device, original name, route/GPS availability, laps. Missing fields stay
`None` and are marked in `confidence.signals` — we never invent data.

## 7. Source lineage stored
Every event records `source` (resolved), `detected_source` (what the file said via
creator/author/manufacturer), `upload_method`, `original_filename`, `file_type`,
`file_hash` (sha256), `parser` + `parser_version`, `raw_payload_ref`. On a merge,
`merged_from` keeps the superseded event ids. Known sources detected today: Garmin,
Strava, Zwift, MyWhoosh, Wahoo; otherwise `file_upload`; never invented → `unknown`.

## 8. Confidence fields
Per-signal `AvailabilityState` for heart_rate / power / gps / elevation / cadence /
distance / duration; the four field classes are kept **distinguishable** —
`imported_fields` / `derived_fields` / `estimated_fields` / `unavailable_fields` —
plus `missing_key_fields`; an overall `score` + `level` (high/medium/low) blending
signal coverage × source trust × parsing confidence; and `data_flags`
(`missing_sensor_data`, `partial_data`, `derived_metrics`, `duplicate`,
`possible_duplicate`, `superseded`).
- **imported**: read straight from the file (e.g. FIT/TCX distance, any present HR).
- **derived**: computed by Epoch (GPX / per-record CSV distance & elevation from points).
- **estimated**: modeled when device data is missing (none in P0 — kept honest/empty).
- **unavailable**: the format/source structurally cannot carry it (e.g. GPS in a
  summary CSV) — different from **missing** (could have been there but wasn't).

## 9. Dedup logic
- **Same file:** identical `file_hash` → duplicate.
- **Same activity, different source:** start-time/distance/duration thresholds aligned
  with `strava/dedup.py` (exact ±5 min / ±2% / ±3%; probable ±30 min / ±5% / ±10%),
  searched within a ±1h window.
- **Source precedence (by data type):** exact duplicates are resolved by precedence
  (Garmin/FIT > generic file/Wahoo > Zwift/MyWhoosh > Strava > manual > unknown). The
  higher-precedence event becomes the primary; the other is **demoted to `duplicate`**
  with lineage links — nothing is deleted, the decision is reversible.
- **Uncertain (probable) matches** are kept in the timeline with status
  `duplicate_uncertain` and flagged `possible_duplicate` (linked to the candidate),
  **never deleted** — mirrors the existing human-approval philosophy and avoids
  false-positive data loss.

## 10. Failed-file behavior (safe failure)
Any parse/normalize error is caught; the timeline is never corrupted. No partial event
is written. A `timeline_import_log` row is recorded with `status=failed` and the error
message. Successful imports also log `received → parsed → normalized → imported/duplicate`.

## 11. Tests / validation
`tests/test_timeline_ingestion.py` — **20 cases, all pass**: GPX/TCX/CSV parsing;
valid FIT import at the pipeline level (faked fitparse/decode_fit) **and** parser unit;
endurance event creation; source lineage; **unknown source** → file_upload fallback;
confidence flags (missing HR, power vs no-HR scoring); **unavailable vs missing**
distinction (summary-CSV GPS); same-file dedup; cross-source precedence (Garmin
supersedes Strava); **uncertain (probable) duplicate** kept-not-deleted; safe failure on
bad/empty files; import-log always written; multi-event extensibility
(strength_session); serialization roundtrip; sport detection.
Phase 3 adds `tests/test_timeline_endpoint.py` — **5 endpoint cases** (import creates event,
list, import-logs, bad-file safe failure, duplicate upload) on an isolated app with an
in-memory repo (no DB, no `main.py` import). New-code total: **25 cases, all pass**.
Full repo suite: **223 passed, 0 failed**. (An earlier run showed 1 flaky/environmental
failure in `test_routes_registered[/gpt/session/.../workout-analysis]`; it passes on a
clean run and is unrelated to this work — no router/`main.py` files were modified here.)

## 12. What is still missing (honest)
- Postgres repo is written but **not exercised against a live DB** here (code-only
  machine). In-memory repo is the tested reference.
- No field-level merge (P0 demotes the duplicate; it does not merge best-of fields).
- No backfill/migration from existing `clean_sessions` into the timeline.
- Only `endurance_workout` is implemented; other event types are scaffolding only.
- Source detection is content-based; richer detection (e.g. per-vendor FIT quirks) later.

## 13. Phase 3 — BUILT + MOUNTED (HTTP wiring + backfill)
Done as **new files** (`routers/timeline.py`, `migrations/007_timeline_events.sql`,
`tools/backfill_timeline_from_clean_sessions.py`, `tests/test_timeline_endpoint.py`).
Endpoints: `POST /timeline/import`, `GET /timeline`, `GET /timeline/import-logs`
(write protection via the app's existing global `X-Epoch-Key` middleware).

The registration block has been applied in `main.py`, next to the other
`app.include_router(...)` blocks:

```python
try:
    from routers.timeline import router as timeline_router
    app.include_router(timeline_router)
    print("✅ Timeline router cargado OK")
except Exception as _tl_err:
    import traceback
    print(f"❌ ERROR Timeline router: {_tl_err}")
    traceback.print_exc()
```

Deploy steps: (1) upload the changed files; (2) apply `migrations/007_timeline_events.sql`
(or it self-creates/updates on first request); (3) optional history import:
`python tools/backfill_timeline_from_clean_sessions.py --execute`.

## 14. Phase 4 (next)
Build the next P0 layer on top of the timeline: **Data Quality + Zone Audit**
(`EPOCH_DATA_QUALITY_ZONE_AUDIT_SPEC.md`) — run before any coaching conclusion.
