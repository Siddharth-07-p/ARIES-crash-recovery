# ARIES Crash Recovery System — Backend

This directory contains the Python backend foundation for the ARIES-inspired crash recovery database internals project.

## Requirements

- Python 3.11+
- Virtual environment (`venv`)

## Setup

1. Create a virtual environment:
   ```bash
   python -m venv .venv
   ```

2. Activate the virtual environment:
   - **Windows (PowerShell):**
     ```powershell
     .venv\Scripts\Activate.ps1
     ```
   - **Windows (Command Prompt):**
     ```cmd
     .venv\Scripts\activate.bat
     ```
   - **Linux / macOS:**
     ```bash
     source .venv/bin/activate
     ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Running the Server

Start the FastAPI development server:
```bash
uvicorn app.main:app --reload --port 8000
```

The API will be accessible at:
- Root: `http://localhost:8000/`
- Health Check: `http://localhost:8000/api/health`
- Monitoring Status: `http://localhost:8000/api/monitoring/status`
- Swagger Docs: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Database Engine Lifespan & Persistence (Phase 16A)

On application startup, the FastAPI server automatically initializes a live database engine via ASGI `lifespan`:
- **Default Storage Location**: `data/database.db` and `data/database.wal` (located under the project root).
- **Environment Overrides**:
  - `ARIES_DATA_DIR`: Base directory for storage and WAL files (default: `<project_root>/data`).
  - `ARIES_DB_PATH`: Explicit path to database binary file (default: `<data_dir>/database.db`).
  - `ARIES_WAL_PATH`: Explicit path to WAL binary file (default: `<data_dir>/database.wal`).
  - `ARIES_BUFFER_POOL_SIZE`: Buffer pool frame capacity (default: `10`).
  - `ARIES_FORCE_RECOVERY`: Force recovery execution on startup (`true` / `false`).
- **Data Protection**: Existing binary database and WAL files are never overwritten or truncated.
- **Automatic Crash Recovery**: If existing WAL log records are detected on startup, `RecoveryManager` automatically executes ARIES crash recovery (Analysis, Redo, Undo with CLRs) to restore database consistency.
- **Subsystem Registration**: Active `TransactionManager`, `LogManager`, `BufferManager`, `CheckpointManager`, and `RecoveryManager` instances are registered with `monitoring_state` so the monitoring dashboard reflects live metrics.
- **Graceful Shutdown**: On server termination, resources are safely flushed and closed in reverse dependency order (`CheckpointManager` -> `TransactionManager` -> `BufferManager` (flushes dirty pages and closes `DiskManager`) -> `LogManager` (flushes WAL)).

### Safe Synthetic Workload Execution (Phase 16B)

A dedicated, safe endpoint executes bounded synthetic transaction workloads directly against the **same live database instance** initialized at startup:

- **Endpoint**: `POST /api/workload/run` (alias: `POST /api/workload/execute`)
- **Request Parameters & Bounds**:
  - `num_transactions`: `1` to `100` (default: `10`)
  - `num_pages`: `1` to `32` (default: `4`)
  - `operations_per_transaction`: `1` to `20` (default: `5`)
  - `read_ratio`: `0.0` to `1.0` (default: `0.3`)
  - `commit_ratio`: `0.0` to `1.0` (default: `0.7`)
  - `abort_ratio`: `0.0` to `1.0` (default: `0.2`)
  - `incomplete_ratio`: `0.0` to `1.0` (default: `0.1`)
  - `same_page_probability`: `0.0` to `1.0` (default: `0.2`)
  - `interleave_probability`: `0.0` to `1.0` (default: `0.5`)
  - `seed`: Optional deterministic integer random seed (e.g. `42`)
  - `checkpoint_interval`: Optional operation interval for checkpoints (`1` to `500`)
- **Validation Rules**:
  - Total planned operations (`num_transactions * operations_per_transaction`) must not exceed `1000`.
  - Outcome ratios (`commit_ratio + abort_ratio + incomplete_ratio`) must strictly sum to `1.0` (within `0.001` tolerance).
  - Out-of-bounds parameters return clear `422 Unprocessable Entity` responses.
- **Concurrency & Failure Protections**:
  - **Single Active Workload**: An atomic lock immediately rejects concurrent workload execution requests with `409 Conflict`.
  - **No Resource Leaks or Teardown**: Reuses the active `TransactionManager`, `BufferManager`, `LogManager`, and `CheckpointManager` without closing underlying file descriptors or buffer frames.
  - **Dynamic Transaction ID Mapping**: Remaps plan-relative IDs to monotonically increasing live engine transaction IDs across successive runs to prevent ID collisions.
  - **Atomic Result Registration**: Results are registered with `monitoring_state` only when database state and frame pin validation pass. Failed workloads return `422` or `500` without exposing internal stack traces.
- **Dashboard Observation**:
  - Completed workloads automatically populate the **Synthetic Workload Summary** card on the React dashboard (`/api/monitoring/workload`), displaying operation counts, commit/abort/active distributions, and validation status.

#### Example Usage (PowerShell):
```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/workload/run" -ContentType "application/json" -Body (@{
    num_transactions = 10
    num_pages = 4
    operations_per_transaction = 5
    read_ratio = 0.3
    commit_ratio = 0.7
    abort_ratio = 0.2
    incomplete_ratio = 0.1
    seed = 42
} | ConvertTo-Json)
```

### Performance Benchmark Execution (Phase 16C)

An explicit, bounded endpoint allows triggering standard benchmark scenarios and recording metrics to the monitoring dashboard:

- **Endpoints**: `POST /api/benchmark/run` (alias: `POST /api/benchmark/execute`)
- **Discovery Endpoint**: `GET /api/benchmark/scenarios` (lists all 10 registered preset scenarios)
- **Supported Presets**:
  - `small_workload` (default): Fast sanity scenario (5 tx, 2 pages, 3 ops/tx).
  - `medium_workload`: Balanced workload (20 tx, 8 pages, 5 ops/tx).
  - `large_workload`: Heavier load (50 tx, 16 pages, 8 ops/tx).
  - `read_heavy`: 85% reads, 15% updates (20 tx, 8 pages, 6 ops/tx).
  - `update_heavy`: 10% reads, 90% updates (20 tx, 8 pages, 6 ops/tx).
  - `hotspot_contention`: 80% Page 0 contention (20 tx, 8 pages, 5 ops/tx).
  - `interleaved_concurrent`: 100% interleaved scheduling (15 tx, 4 pages, 4 ops/tx).
  - `high_abort`: 70% aborts measuring rollback/CLR overhead (20 tx, 4 pages, 5 ops/tx).
  - `recovery_only`: Prepares uncheckpointed crash state and measures ARIES recovery stages.
  - `crash_recovery_commit_fault`: Subprocess crash experiment with restart recovery.
- **Request Parameters & Bounds**:
  - `scenario`: Preset name (default: `"small_workload"`). Must be registered in `list_preset_scenarios()`.
  - `repetitions`: `1` to `10` measured repetitions (default: `3`).
  - `warmup_runs`: `0` to `5` unmeasured warmup runs (default: `1`).
  - `total_runs`: `repetitions + warmup_runs <= 12`.
  - `seed`: Optional deterministic integer random seed (default: `42`).
  - `timeout_per_run`: `1.0` to `60.0` seconds per run (default: `30.0`).
- **Storage Safety & Baseline Isolation**:
  - Benchmark repetitions execute in isolated temporary databases per-run. This guarantees reproducible baselines across warmup and measured iterations and completely prevents multi-run benchmark traffic from deleting, truncating, or corrupting live database files (`data/database.db` and `data/database.wal`).
  - Responses explicitly include `execution_mode: "isolated_temporary_storage"` and `isolation_note`.
- **Mutual Exclusion & Concurrency Protection**:
  - Benchmark and workload executions share an atomic concurrency lock (`workload_lock`).
  - Concurrent benchmark runs, concurrent workload runs, or interleaved benchmark/workload calls are immediately rejected with `409 Conflict`.
- **Dashboard Observation**:
  - Successful results are registered with `monitoring_state` and update the **Performance Benchmarks** card on the React dashboard (`/api/monitoring/benchmark`), displaying TPS, OPS, mean duration, and recovery duration with an **Isolated Storage** indicator badge.

#### Example Usage (PowerShell):
```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/benchmark/run" -ContentType "application/json" -Body (@{
    scenario = "small_workload"
    repetitions = 3
    warmup_runs = 1
    seed = 42
} | ConvertTo-Json)
```

### End-to-End Integration Verification (Phase 16D)

The complete ARIES stack has been verified end-to-end across backend services, live storage, monitoring state, synthetic workloads, benchmark suites, and the React frontend:

- **E2E Integration Test Suite**: Dedicated suite in `backend/tests/test_e2e_integration.py` validates the complete integration matrix:
  - Startup lifespan and clean subsystem initialization.
  - Health check (`/api/health`) and operational status (`/api/monitoring/status`).
  - Scenario discovery (`/api/benchmark/scenarios`).
  - Workload execution (`/api/workload/run`) with real-time metric updates on `/api/monitoring/workload`, `/api/monitoring/transactions`, and `/api/monitoring/wal`.
  - Benchmark execution (`/api/benchmark/run`) with metric aggregation on `/api/monitoring/benchmark`.
  - Non-destructive storage isolation: live `data/database.db` and `data/database.wal` binary contents remain intact during benchmark runs.
  - Concurrency guards: mutual exclusion between workloads and benchmarks via `workload_lock` (HTTP 409 Conflict).
  - Input validation: bounds checking and schema verification (HTTP 422 Unprocessable Entity).
  - Uninitialized state handling: clean degradation when database services are unavailable (HTTP 503).
- **Verified Endpoints**:
  - `GET /api/health`: Basic process health (`status: ok`).
  - `GET /api/monitoring/status`: Operational state (`database_initialized: true`).
  - `GET /api/monitoring/transactions`: Live active/committed/aborted transaction counters.
  - `GET /api/monitoring/wal`: Current WAL record count and flushed LSN.
  - `GET /api/monitoring/buffer-pool`: Resident page count, dirty pages, hit ratio, frame capacity.
  - `GET /api/monitoring/workload`: Latest synthetic workload summary and status.
  - `GET /api/monitoring/benchmark`: Aggregated benchmark performance distributions and isolation flag.
  - `GET /api/monitoring/recovery`: Recovery status and stage details.
  - `GET /api/benchmark/scenarios`: Registered preset scenario descriptions.
  - `POST /api/workload/run`: Execute bounded workload against live database.
  - `POST /api/benchmark/run`: Execute statistical benchmark suite in isolated storage.

#### Verified Verification Commands:

```powershell
# Run full backend test suite (312 tests)
backend\.venv\Scripts\python -m pytest -v

# Run E2E integration test suite
backend\.venv\Scripts\python -m pytest tests/test_e2e_integration.py -v

# Run frontend production build
cd frontend; npm run build; cd ..

# Query live monitoring status
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/monitoring/status"

# Trigger synthetic workload
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/workload/run" -ContentType "application/json" -Body '{"num_transactions": 10, "num_pages": 4, "operations_per_transaction": 5}'

# Trigger benchmark run
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/benchmark/run" -ContentType "application/json" -Body '{"scenario": "small_workload", "repetitions": 3, "warmup_runs": 1}'
```

## Running Tests

Execute the test suite using `pytest`:
```bash
pytest
```
