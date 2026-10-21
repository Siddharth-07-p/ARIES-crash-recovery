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

## Running Tests

Execute the test suite using `pytest`:
```bash
pytest
```
