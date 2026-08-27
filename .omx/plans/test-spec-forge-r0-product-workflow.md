# Test Spec — FORGE R0 Local Product Workflow

## Contract matrix

| Area | Required proof | Expected failure behavior |
| --- | --- | --- |
| Project persistence | create/get/delete/reopen | missing ID=`not_found`; corrupt row=`integrity_error` |
| Spec lifecycle | draft versions, one approved immutable snapshot | stale version=`conflict`; mutation=`immutable_record` |
| Idempotency | same key+hash returns same resource | same key+different hash=`idempotency_conflict` |
| Analysis | approved spec creates stored run/evidence | rejected preflight stores evidence, creates no run |
| Engine staging | prepare persists before accepted run creation | prepare/spec/revision/plugin hash mismatch blocks execution |
| Replay | retry creates new run ID, same deterministic hashes | unavailable plugin/version=`replay_unavailable` |
| Revision impact | only dependency-matched evidence retained | unknown dependency conservatively stale |
| Cost | source/time/MOQ/scope required | missing/expired/unknown=`INDETERMINATE` |
| Maturity | sequential evidence classes | analysis/SIL cannot substitute HIL |
| HTTP input | JSON type/size/schema enforced | 400/413/415/422 stable envelope |
| Browser boundary | explicit Origin/Host/CSRF | hostile mutation rejected before service call |
| Export | versioned data+hashes only | path/secret/corrupt hash rejected |
| UI | create→approve→run→evidence→retry | no false bench/commissioned label |
| Crash recovery | `prepared→queued→running→failed` and `system-recovery/process_interrupted` appended once | no silent success or mutable overwrite |
| Storage lock | 100 ms timeout, 3 retries at 25/50/100 ms | stable `storage_busy`, no partial/idempotency duplicate |
| Architecture | application service is sole transition owner | API/UI direct repository or verdict/maturity calls fail check |

## Required fixtures

- in-memory SQLite for focused unit tests
- temporary file SQLite for reopen/rollback/integration tests
- fixed approved and draft fake specs
- stale `expected_version`, duplicated idempotency keys and corrupt JSON rows
- prepared token with stale spec/revision/plugin hashes
- allowed/disallowed Origin and Host tables
- missing input, plugin mismatch and deterministic replay cases

## Coverage requirements

- Every state transition has success, invalid predecessor and duplicate request coverage.
- Spec and run transition tables are tested independently; invalid transitions map to conflict/409.
- Existing `AnalysisEngine.execute()` behavior remains identical after two-stage refactor.
- Every mutation endpoint has missing/invalid Origin, Host, CSRF, Content-Type and oversized body coverage.
- Every persisted Pydantic payload has write/read round-trip and corrupt-read coverage.
- Every UI maturity label has positive and forbidden-state assertions.
- Engine execution is instrumented in tests to prove no SQLite write transaction remains open during plugin callbacks.
- Same project version export is asserted byte-for-byte equal with filename `project-{id}-v{version}.forge.json` and equal SHA-256.
- Browser E2E includes stale approval recovery, double-submit deduplication and failed-run retry with prior evidence preserved.
- Overall core branch coverage remains at least 90%.

## Stop conditions

- No implementation handoff until Architect then Critic approve this test shape.
- No milestone completion until root verify, clean code review and hostile QA pass.
- Any actual device I/O code, credential handling or external network call is an immediate scope failure.

## External evidence

- FastAPI CORS: https://fastapi.tiangolo.com/tutorial/cors/
- FastAPI strict Content-Type and local CSRF risk: https://fastapi.tiangolo.com/advanced/strict-content-type/
- FastAPI TrustedHost middleware: https://fastapi.tiangolo.com/advanced/middleware/
- FastAPI TestClient: https://fastapi.tiangolo.com/tutorial/testing/
- Python 3 sqlite3 transaction control: https://docs.python.org/3/library/sqlite3.html#transaction-control-via-the-autocommit-attribute
- Uvicorn settings: https://www.uvicorn.org/settings/
