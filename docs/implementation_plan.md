# Phase 9: ARIES Undo and Compensation Log Records (CLRs) — Implementation Plan

## 1. Architectural Context & Objective

The objective of Phase 9 is implementing the **ARIES Undo Recovery Phase** and **Compensation Log Records (CLRs)** for the database crash recovery system. Building upon:
- Phase 7: Analysis Phase (reconstructing Transaction Table, DPT, and identifying winners/losers)
- Phase 8: Redo Phase (Repeating history for both winners and losers to bring the buffer/disk up to crash state)

Phase 9 completes the rollback of uncommitted loser transactions and transactions whose abort/rollback was interrupted by a crash.

---

## 2. CLR Log Record Format and Binary Serialization

A Compensation Log Record (CLR) represents the action taken to compensate (undo) an `UPDATE` log record. In ARIES, CLRs are **never undone**; they serve to bound the amount of work required during subsequent recoveries should another crash occur.

### Binary Layout (deterministic, network byte order `>`):
1. **Magic Header (4 bytes)**: `b"WALR"`
2. **Total Record Length (4 bytes)**: `uint32`
3. **LSN (8 bytes)**: `uint64` (strictly monotonic, positive)
4. **Prev LSN (8 bytes)**: `uint64` (0 if None) — points to the current transaction's previous log tail
5. **Transaction ID (8 bytes)**: `uint64` (positive integer)
6. **Record Type (1 byte)**: `uint8 = 7` (`TYPE_CODE_CLR`)
7. **Payload (20 + N bytes)**:
   - `page_id`: `uint32` (4 bytes)
   - `offset`: `uint32` (4 bytes)
   - `undo_next_lsn`: `uint64` (8 bytes, 0 if None) — points to the next earlier log record of the transaction that remains to be undone
   - `compensation_len`: `uint32` (4 bytes)
   - `compensation_image`: `bytes` (N bytes) — the before-image of the original update, used to redo the undo operation during restart Redo
8. **CRC32 Checksum (4 bytes)**: `uint32` computed across header and payload

### Strict Invariants & Validations:
- `page_id` is non-negative and valid.
- `offset >= 0` and `offset + len(compensation_image) <= PAGE_SIZE`.
- `undo_next_lsn` must be strictly less than the record's own LSN (`undo_next_lsn < lsn`). Forward or self references are prohibited.
- `undo_next_lsn` is strictly forbidden on non-CLR record types.

---

## 3. Undo Engine Algorithm

The Undo phase processes loser transactions to safely restore data pages to their pre-transaction states.

### Priority Queue Processing (Highest-LSN-First):
1. Reconstruct the loser transaction set:
   - All transactions marked as `RUNNING` in the Transaction Table.
   - Any transaction marked as `ABORTED` whose physical rollback was incomplete (has uncompensated `UPDATE` records).
2. Populate a max-heap (implemented as min-heap with negative LSNs: `(-last_lsn, tx_id)`):
   - For losers with valid log tails, push `(-last_lsn, tx_id)`.
   - For losers with no log records, immediately append a durable `ABORT` record and mark complete.
3. While heap is non-empty:
   - Pop `(neg_lsn, tx_id)`; `curr_lsn = -neg_lsn`.
   - Retrieve log record at `curr_lsn` from `LogManager`.
   - **Case `UPDATE`**:
     1. Validate bounds: `0 <= offset` and `offset + len(before_image) <= PAGE_SIZE`.
     2. Fetch page from `BufferManager`.
     3. Restore `before_image` at `offset`.
     4. Append `CLR` record with:
        - `prev_lsn` = transaction's current log tail.
        - `undo_next_lsn` = `record.prev_lsn`.
        - `compensation_image` = `record.before_image`.
     5. Flush WAL to disk (durable before page flush).
     6. Set `page_lsn` to the CLR LSN and mark page dirty.
     7. Unpin page and flush to disk.
     8. If `undo_next_lsn` is not None, push `(-undo_next_lsn, tx_id)` to heap; otherwise append durable `ABORT` completion record.
   - **Case `CLR`**:
     - Encountered during restart after partial Undo.
     - Skip re-undoing; follow `record.undo_next_lsn`.
     - If `undo_next_lsn` is None, append durable `ABORT` completion record; otherwise push `(-undo_next_lsn, tx_id)` to heap.
   - **Case `BEGIN`**:
     - Reached transaction start; append durable `ABORT` completion record and mark transaction completed.
   - **Case `ABORT`**:
     - Skip record; follow `record.prev_lsn` to undo prior updates if rollback was incomplete.
   - **Case `COMMIT`**:
     - Committed winner; never undone.

---

## 4. Aborted Transactions and Completion Handling

In ARIES, an `ABORT` record in the log indicates the decision or initiation of an abort, but does **not** prove that physical rollback finished.
- An aborted transaction whose rollback was interrupted by a crash still contains dirty updates on disk/pages.
- **Rule**: If backward traversal from `tx.last_lsn` encounters an `UPDATE` record (or a CLR whose `undo_next_lsn` points to an `UPDATE`), physical rollback was incomplete.
- Undo adds incomplete aborted transactions to the rollback queue, undoes remaining updates, and appends a durable completion `ABORT` record.
- Once all updates are compensated by CLRs and terminated with a completion record, subsequent recovery recognizes the transaction as completed and never undoes it again.

---

## 5. Page Restoration & WAL Durability Invariant

To satisfy Write-Ahead Logging requirements:
1. Dirty page data restored in memory cannot reach disk before the corresponding CLR log record is written and fsynced.
2. In `Undo._execute_internal`, `self._log_manager.flush()` is invoked **before** `self._buffer_manager.flush_page(page_id)`.
3. In `BufferManager.flush_page()`, `_ensure_wal_flushed_for_frame()` verifies `lm.flushed_lsn >= page_lsn`.
4. Page pin counts are protected with `try...finally` to ensure frames are never left pinned if validation or fault injection crashes occur.

---

## 6. Restart Safety & `undo_next_lsn` Mechanics

When a crash occurs during Undo:
- Any CLR that reached disk is permanent.
- On subsequent restart:
  1. Analysis reconstructs the transaction table and DPT.
  2. Redo replays CLRs along with UPDATEs ("repeating history").
  3. Undo reads the CLR at the transaction's tail and follows its `undo_next_lsn`.
  4. The already-compensated update is never undone a second time.
  5. Consecutive crashes during Undo are fully supported and monotonic.

---

## 7. Fault Injection Points

Three fault injection points are supported:
- `DURING_UNDO_BEFORE_CLR_FLUSH`: Triggered prior to making CLR durable.
- `DURING_UNDO_AFTER_CLR_FLUSH_BEFORE_PAGE_FLUSH`: Triggered after CLR is durable in WAL but before restored page is persisted.
- `DURING_UNDO_AFTER_PAGE_FLUSH`: Triggered after the restored page is persisted to disk.

Subprocess tests execute real process terminations (`sys.exit(42)`) to verify that the OS process dies and persistent files can be safely recovered by a new process.

---

## 8. UndoResult Immutability and Error Handling

`UndoResult` encapsulates:
- `loser_transactions`: Total losers identified
- `transactions_undone`: Total transactions rolled back
- `clrs_generated`: Count of CLR records written
- `updates_undone`: Count of UPDATE records undone
- `pages_modified`: Sorted list of unique modified page IDs
- `records_skipped`: Non-update records processed
- `last_processed_lsn`: Highest LSN processed
- `completed_transactions`: Sorted list of completed transaction IDs
- `remaining_losers`: Sorted list of incomplete transactions (empty on completion)
- `is_complete`: Boolean flag indicating full recovery completion
- Immutable slots and defensively copied lists.

---

# Phase 10: Complete ARIES Crash-Recovery Orchestration — Implementation Plan

## 1. Architectural Context & Objective

The objective of Phase 10 is implementing a **RecoveryManager** that coordinates the existing ARIES recovery components (Analysis, Redo, and Undo) into a restart-safe, complete crash-recovery workflow.
- **Analysis**: Scans WAL forward from the latest valid checkpoint (or beginning of log) to reconstruct the Transaction Table (identifying winners and losers) and Dirty Page Table (DPT) with initial recLSNs.
- **Redo**: Repeats history by scanning forward from `redo_start_lsn` (minimum recLSN in DPT) and replaying all eligible `UPDATE` and `CLR` records.
- **Undo**: Rolls back active uncommitted (loser) transactions backward in global descending-LSN order, applying before-images, generating CLRs, enforcing WAL-before-page durability, and appending completion `ABORT` records.
- **Final Verification & Result Construction**: Persists all modified pages via `BufferManager.flush_all_pages()`, checks full completion, and produces an immutable `RecoveryResult`.

## 2. RecoveryManager Architecture & Public API

The `RecoveryManager` class is implemented in [`backend/app/recovery/recovery_manager.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/recovery/recovery_manager.py):

### Public APIs:
1. **Instance Method**:
   ```python
   manager = RecoveryManager(log_manager=lm, buffer_manager=bm, checkpoint_manager=cm)
   result = manager.recover(raise_on_error=True)
   ```
2. **Dual Class/Instance Method**:
   ```python
   result = RecoveryManager.recover(log_manager=lm, buffer_manager=bm, checkpoint_manager=cm, raise_on_error=True)
   ```
3. **Convenience File-Based Recovery**:
   ```python
   result = RecoveryManager.recover_from_files(db_path=db_path, wal_path=wal_path, pool_size=10, raise_on_error=True)
   ```

### RecoveryResult Data Model:
An immutable data class in [`backend/app/recovery/recovery_state.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/recovery/recovery_state.py) containing:
- `is_success`: bool indicating overall recovery success
- `stage`: Stage reached or completed (`COMPLETE`, `FAILED_AT_ANALYSIS`, `FAILED_AT_REDO`, `FAILED_AT_UNDO`)
- `error_message`: Optional failure description if recovery failed
- `analysis_result`: Reconstructed `AnalysisResult`
- `redo_result`: Reconstructed `RedoResult`
- `undo_result`: Reconstructed `UndoResult`
- Aggregated properties: `winners`, `losers`, `pages_modified`, `records_redone`, `clrs_generated`, `updates_undone`, `completed_transactions`
- Serialization via `to_dict()`, equality check `__eq__`, and human-readable `__repr__`.

## 3. Strict Recovery Stage Ordering

Recovery execution adheres strictly to canonical ARIES stage sequencing:
1. **Stage 1 (Analysis)**: Reconstructs state without modifying pages or WAL.
   - Fault Point: `AFTER_ANALYSIS_BEFORE_REDO`
2. **Stage 2 (Redo)**: Replays updates and CLRs for winners and losers from `redo_start_lsn`.
   - Fault Point: `AFTER_REDO_BEFORE_UNDO`
3. **Stage 3 (Undo)**: Undoes uncommitted losers backward, logging CLRs and completion records.
   - Fault Point: `AFTER_UNDO_BEFORE_COMPLETE`
4. **Stage 4 (Verification & Flush)**: Persists all buffer pool pages to disk and validates `undo_result.is_complete`.

## 4. Startup & Restart Safety

- Uses existing persistent `.wal` and `.db` files without truncation or overwrite.
- Re-running recovery on an already-recovered database is idempotent (0 losers, 0 CLRs, 0 page writes).
- Crashes during Redo or Undo are safely resumable on restart using durable CLRs and `undo_next_lsn`.

---

# Phase 11: ARIES Integration and Correctness Testing — Implementation Plan

## 1. Objective & Scope

Perform comprehensive integration and correctness validation across all ARIES components (Storage, Buffer, WAL, Checkpointing, Analysis, Redo, Undo, and RecoveryManager).
- Validate all 20 required integration scenarios against the unified RecoveryManager.
- Verify core ARIES invariants including durability, redo repeatability, undo backward-chaining, buffer pool pin safety, and idempotence.
- Maintain full compatibility with all existing Phase 0–10 APIs and test suites.

## 2. Integration Test Matrix (20 Scenarios)

Implemented in [`backend/tests/test_integration_matrix.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_integration_matrix.py):
1. Clean database with no recovery work required.
2. Committed transaction whose updates must survive restart.
3. Incomplete transaction whose updates must be undone.
4. Multiple interleaved transactions with different commit/abort outcomes.
5. Multiple updates by the same transaction to the same page.
6. Multiple transactions updating different pages.
7. A transaction updating multiple pages before a crash.
8. Recovery with a valid checkpoint present.
9. Recovery when the checkpoint is absent or incomplete.
10. A crash during Redo followed by another recovery attempt.
11. A crash during Undo followed by another recovery attempt.
12. A crash after a CLR is flushed but before the corresponding page update is flushed.
13. A crash after Undo has completed but before recovery completion is recorded.
14. Repeated recovery on an already recovered database (idempotence).
15. Corrupted or invalid WAL records and invalid LSN chains.
16. Invalid transaction or page references.
17. Recovery failures at each orchestration stage.
18. File-based recovery using the same database and WAL files across process restarts.
19. Buffer-pool pin/unpin safety and page consistency after recovery.
20. Compatibility with all existing public APIs and tests.

## 3. Verified Correctness Invariants

- Committed transactions are never undone during recovery.
- Incomplete transactions are completely rolled back to before-images.
- Redo repeats history idempotently and respects pageLSN ordering.
- Undo strictly follows transaction backward chains and logs CLRs prior to page writes.
- Real subprocess crashes at fault points resume safely on restart.
- Buffer pool pins are guaranteed 0 after recovery (no frame leaks).

---

# Phase 12: Synthetic Transaction Workload Generator — Implementation Plan

## 1. Objective & Scope

Implement a configurable, reproducible synthetic transaction workload generator exercising transaction control, page storage, buffer pool management, WAL logging, fuzzy checkpointing, crash injection, and ARIES recovery without bypassing existing database APIs.

- Configurable parameters: transaction count, page count, operations per transaction, read/update mix, commit/abort/incomplete ratios, hotspot probability, interleaving probability, deterministic random seed.
- Deterministic operation-plan generation separated from execution outcomes.
- Execution on real database subsystems with strict pin safety and expected-state validation.
- Subprocess crash experiments and restart recovery verification.
- Immutable, encapsulated `WorkloadResult` serialization without benchmarking/monitoring clutter.

## 2. Workload Architecture

- [`backend/app/workload/config.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/config.py): `WorkloadConfig` with strict validation.
- [`backend/app/workload/models.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/models.py): `OperationType`, `TransactionOutcome`, `WorkloadOperation`, `WorkloadPlan`, `WorkloadResult`.
- [`backend/app/workload/generator.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/generator.py): `WorkloadGenerator` for deterministic plan generation.
- [`backend/app/workload/executor.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/executor.py): `WorkloadExecutor` for execution, validation, and crash experimentation.
- [`backend/app/workload/exceptions.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/exceptions.py): Domain-specific exception hierarchy.

## 3. Workload Test Suite (17 Scenarios)

Implemented in [`backend/tests/test_workload_generator.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_workload_generator.py):
1. Configuration validation and rejection of invalid inputs.
2. Deterministic operation-plan generation using a fixed seed.
3. Different seeds producing appropriately different plans.
4. Valid mixed read/update workload execution.
5. Committed transaction durability across restarts.
6. Aborted transaction rollback with CLR logging.
7. Incomplete transactions and subsequent restart recovery.
8. Interleaved transaction scheduling and execution.
9. Workloads operating across multiple pages without non-interference bugs.
10. Zero-operation and minimum-size workloads.
11. Reproducibility across independent test runs.
12. Crash injection and restart recovery using persistent files.
13. Result counts and immutability serialization.
14. Invalid execution parameters failing safely.
15. Compatibility with existing tests and public APIs.
16. Expected-state validator detects disk mismatches, unallocated pages, and recovery discrepancies.
17. Repeated updates by the same transaction to the same page and slot with full rollback.

---

# Phase 13: ARIES Performance Benchmarking Framework — Implementation Plan

## 1. Objective & Scope

Implement a modular, reproducible performance benchmarking framework for the ARIES database engine and synthetic workload generator.
- Measure workload execution duration, transaction throughput (TPS), and operation throughput (ops/sec).
- Measure recovery duration and stage-level breakdown (Analysis, Redo, Undo) without invasive changes to core recovery logic.
- Distinguish setup and preparation from timed workload/recovery intervals.
- Ensure strict correctness validation prior to recording benchmark results (failed validation = failed run).
- Provide multi-run repetitions, warmup runs, and statistical distributions (mean, median, min, max, std_dev, p95).
- Provide export facilities to JSON and CSV, and a CLI entry point.
- Maintain full compatibility with all existing Phase 0–12 implementations without introducing monitoring APIs or frontend dashboard features.

## 2. Benchmark Architecture

- [`backend/app/benchmark/config.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/config.py): `BenchmarkConfig` validation, repetitions, warmup, seeds, and scenario parameters.
- [`backend/app/benchmark/models.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/models.py): `EnvironmentMetadata`, `WorkloadRunMetrics`, `RecoveryRunMetrics`, `BenchmarkRunResult`, `MetricDistribution`, `BenchmarkScenarioResult`.
- [`backend/app/benchmark/scenarios.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/scenarios.py): Standard preset scenarios (`small_workload`, `medium_workload`, `large_workload`, `read_heavy`, `update_heavy`, `hotspot_contention`, `interleaved_concurrent`, `high_abort`, `recovery_only`, `crash_recovery_commit_fault`).
- [`backend/app/benchmark/runner.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/runner.py): `BenchmarkRunner` isolated execution engine, timing intervals, and post-run validation.
- [`backend/app/benchmark/exporter.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/exporter.py): `BenchmarkExporter` for structured JSON and CSV formats.
- [`backend/app/benchmark/cli.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/cli.py): CLI interface and console summary reporting (`python -m app.benchmark`).
- [`backend/app/benchmark/exceptions.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/exceptions.py): Domain-specific exception hierarchy (`BenchmarkError`, `BenchmarkConfigError`, etc.).

## 3. Benchmark Test Suite (17 Scenarios)

Implemented in [`backend/tests/test_benchmark.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_benchmark.py):
1. Benchmark configuration validation and invalid input rejection.
2. Small benchmark scenario execution and status reporting.
3. Correct metric calculations, throughput formulas, and units.
4. Zero-operation and zero-duration division safeguards.
5. Multiple repetitions and aggregate statistics (mean, median, std_dev, p95).
6. Fixed-seed reproducibility across independent executions.
7. Isolation of database and WAL files across runs (no file leaks).
8. Recovery-only timing and stage-level result collection.
9. Optional crash-recovery scenario correctness with fault injection.
10. Failed workload validation correctly marked as failed run.
11. CSV export schema, units, and contents verification.
12. JSON export schema, metadata, and contents verification.
13. Hardware/platform environment metadata serialization.
14. Compatibility with existing workload generator and recovery manager.
15. Error handling and cleanup of temporary directories and export targets.
16. p95 linear interpolation and monotonic percentile ordering (min <= median <= p95 <= max).
17. Crash benchmark separation of child crash duration from parent recovery duration.

---

# Phase 14: Database Monitoring and Statistics APIs — Implementation Plan

## 1. Objective & Scope

Implement read-only monitoring and statistics APIs for the existing ARIES-inspired database recovery project under a dedicated FastAPI router (`/api/monitoring/*`).
- Expose system status, transaction metrics, WAL statistics, buffer pool state, latest recovery outcome, synthetic workload summary, and benchmark results.
- Keep monitoring completely decoupled from transaction execution and recovery logic.
- Strictly read-only: monitoring queries must never mutate database or WAL state, allocate frames, or trigger recovery/transactions merely to answer a request.
- Clear handling of uninitialized subsystems, closed managers, and absent results using documented states (`"uninitialized"`, `"not_run"`, `"not_available"`).
- Preserve existing `/` and `/api/health` endpoints without regressions.

## 2. Architecture & Subsystem Inspection

- [`backend/app/monitoring/models.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/monitoring/models.py): Pydantic response models:
  - `MonitoringStatusResponse`: service name, status (`operational`, `degraded`, `uninitialized`), database initialization flag, recovery status (`not_run`, `recovered`, `unrecovered`, `failed`), and ISO-8601 timestamp.
  - `TransactionStatisticsResponse`: status (`available`, `uninitialized`), total started, committed, aborted, active count, and sorted list of active transaction IDs.
  - `WALStatisticsResponse`: status, current LSN, flushed LSN, total log records count, WAL file size in bytes, and closed status.
  - `BufferPoolStatisticsResponse`: status, frame capacity, resident pages count, pinned frames count, dirty pages count, available frame count, and closed status.
  - `RecoveryStatisticsResponse`: status (`not_run`, `available`, `failed`), stage, success flag, winner/loser counts, records redone, CLRs generated, updates undone, pages modified count, and stage durations (seconds).
  - `WorkloadSummaryResponse`: status (`available`, `not_available`), operation counts (planned, executed, reads, updates), transaction outcomes (started, committed, aborted, incomplete), validation flag, crash configuration, and fault point.
  - `BenchmarkSummaryResponse`: status (`available`, `not_available`), scenario name, success flag, run counts (total, measured, warmup, valid, failed), and mean duration and throughput metrics.
- [`backend/app/monitoring/state.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/monitoring/state.py): `MonitoringState` thread-safe registry with non-mutating metric extraction and safe handling of closed or missing components.
- [`backend/app/monitoring/router.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/monitoring/router.py): FastAPI APIRouter mounted at `/api/monitoring` with standard HTTP GET endpoints.
- [`backend/app/main.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/main.py): Integration of `monitoring_router` into the primary FastAPI app.

## 3. Monitoring Test Suite (17 Scenarios)

Implemented in [`backend/tests/test_monitoring_api.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_monitoring_api.py):
1. `test_01_monitoring_status_response`: Uninitialized, operational, and degraded service status transitions.
2. `test_02_transaction_statistics_when_transactions_exist`: Started, committed, aborted, and active counts and IDs.
3. `test_03_wal_statistics_and_unavailable_states`: Current LSN, flushed LSN, total records, and file size tracking.
4. `test_04_buffer_pool_statistics_and_unavailable_states`: Pool size, resident, pinned, dirty, and available frames calculation.
5. `test_05_recovery_statistics_before_and_after_recovery`: `not_run` state transitions to `available` with redo/undo metrics.
6. `test_06_recovery_failure_reporting`: Recovery failure reporting with error message and stage indicator.
7. `test_07_workload_summary_when_no_workload_has_run`: Safe `not_available` response prior to workload run.
8. `test_08_workload_summary_when_results_are_available`: Operation counts, transaction outcomes, and validity status.
9. `test_09_benchmark_summary_when_no_benchmark_has_run`: Safe `not_available` response prior to benchmark run.
10. `test_10_benchmark_summary_when_results_are_available`: Scenario metadata, run breakdowns, and mean throughputs.
11. `test_11_response_schemas_and_data_types`: Strict Pydantic response model validation for all 7 endpoints.
12. `test_12_requests_not_mutating_database_or_wal_state`: Repeated monitoring requests verify zero side-effects on LSN, WAL records, buffer state, or disk.
13. `test_13_existing_api_health_compatibility`: Unaltered behavior and response schema for `/api/health`.
14. `test_14_missing_or_uninitialized_components`: Graceful degradation when individual subsystems are unregistered.
15. `test_15_error_handling_for_closed_components`: Safe metric extraction without raising `ClosedError` exceptions.
16. `test_16_regression_coverage_for_existing_api_routes`: Root endpoint `/` and `/api/health` backward compatibility.
17. `test_17_concurrency_and_closed_race_safety`: Thread-safe concurrent queries, exact eviction/free frame availability matching, and degraded status reporting upon recovery failure.

---

# Phase 15: React Monitoring Dashboard — Implementation Plan

## 1. Objective & Scope

Build a responsive, accessible React + TypeScript monitoring dashboard that consumes the Phase 14 monitoring REST endpoints (`/api/monitoring/*` and `/api/health`).
- Expose engine health, transaction metrics, WAL progress, buffer frame allocation, crash recovery stages, synthetic workload summaries, and benchmark throughput.
- Keep the dashboard strictly read-only; never imply that GET requests trigger transactions, recovery, workloads, or benchmarks.
- Cleanly handle unavailable states, uninitialized managers, and partial network/endpoint failures without crashing or fabricating zeros.
- Provide automatic periodic polling (with configurable intervals) and on-demand manual refresh with cancellation and overlapping request protection.

## 2. Frontend Architecture & Component Structure

- [`frontend/src/types.ts`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/types.ts): TypeScript interfaces matching backend models (`MonitoringStatusResponse`, `TransactionStatisticsResponse`, `WALStatisticsResponse`, `BufferPoolStatisticsResponse`, `RecoveryStatisticsResponse`, `WorkloadSummaryResponse`, `BenchmarkSummaryResponse`, `HealthResponse`).
- [`frontend/src/hooks/useMonitoringData.ts`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/hooks/useMonitoringData.ts): Custom React hook using `Promise.allSettled`, `AbortController` cancellation, `isFetchingRef` overlap guards, and configurable polling timers.
- [`frontend/src/components/StatusBadge.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/components/StatusBadge.tsx): Semantic status pill with color-coded dot and labels for operational, degraded, uninitialized, recovered, failed, not-run, and closed conditions.
- [`frontend/src/components/MetricCard.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/components/MetricCard.tsx): Reusable metric display card with label, value, unit, subtext, icon, shimmer loading, and explicit `—` representation for missing values.
- [`frontend/src/components/OverviewSection.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/components/OverviewSection.tsx): Engine header, service status, initialization flag, recovery lifecycle badge, poll interval selector, and manual refresh controls.
- [`frontend/src/components/TransactionSection.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/components/TransactionSection.tsx): Started, committed, aborted, and active transaction counts, plus active transaction ID chips.
- [`frontend/src/components/WalSection.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/components/WalSection.tsx): Current LSN, flushed LSN, sync lag, total record count, formatted WAL file size (B, KB, MB), and open/closed status.
- [`frontend/src/components/BufferPoolSection.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/components/BufferPoolSection.tsx): Frame capacity, resident pages, pinned frames, dirty pages, available frames, and visual memory footprint bar.
- [`frontend/src/components/RecoverySection.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/components/RecoverySection.tsx): Recovery status (Not Run, Recovered, Failed), stage, winner/loser counts, redo/undo counts, CLRs generated, pages modified, and monotonic stage timing breakdown.
- [`frontend/src/components/WorkloadSection.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/components/WorkloadSection.tsx): Operations executed/planned, read/update mix, transaction outcomes, correctness validation badge, and fault point status.
- [`frontend/src/components/BenchmarkSection.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/components/BenchmarkSection.tsx): Scenario name, pass/fail status, run counts, transaction throughput (TPS), operation throughput (ops/sec), and mean durations.
- [`frontend/src/App.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/App.tsx): Main dashboard layout assembling header, overview, 2-column responsive dashboard grid, and footer.
- [`frontend/src/App.css`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/App.css): Dark theme styles, glassmorphism cards, responsive layouts, and accessibility focus rings.

## 3. Verification & Quality Gates

- Production build verification via `npm run build` (`tsc && vite build`).
- Full backend regression verification via `pytest` (all 276 tests passing).
