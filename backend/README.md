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

## Running Tests

Execute the test suite using `pytest`:
```bash
pytest
```
