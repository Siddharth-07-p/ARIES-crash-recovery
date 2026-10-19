# Phase 9: ARIES Undo and Compensation Log Records (CLRs) — Walkthrough

## Overview

Phase 9 completes the implementation and verification of the **ARIES Undo Recovery Phase** with **Compensation Log Records (CLRs)**. This walkthrough details the implementation, verification results across all 29 mandatory test scenarios, fault injection experiments, and architectural constraints.

---

## 1. Compensation Log Record (CLR) Format & Semantics

In accordance with ARIES recovery principles:
- An update that is undone generates a **Compensation Log Record (CLR)**.
- A CLR contains the **compensation image** (the original before-image), which allows Redo to replay the undo operation if a second crash occurs.
- A CLR contains an `undo_next_lsn` field that points to the next original log record for that transaction that must still be undone.
- CLRs are **never undone**. When Undo encounters a CLR during backward log traversal, it jumps directly to `undo_next_lsn`.
- The CLR is part of the transaction's WAL chain; its `prev_lsn` points to the current transaction log tail.

### Serialization Byte Layout:
```
+--------------+------------------+---------------+----------------+----------------+---------------+-----------------------------------------------+----------------+
| Magic (4B)   | Total Length(4B) | LSN (8B)      | Prev LSN (8B)  | Tx ID (8B)     | Type (1B=7)   | Payload: page_id(4B), offset(4B),             | CRC32 (4B)     |
| "WALR"       | uint32           | uint64        | uint64         | uint64         | TYPE_CODE_CLR | undo_next_lsn(8B), comp_len(4B), comp_data(NB)| uint32         |
+--------------+------------------+---------------+----------------+----------------+---------------+-----------------------------------------------+----------------+
```

---

## 2. Undo Engine Architecture

The Undo implementation resides in [`backend/app/recovery/undo.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/recovery/undo.py).

### Global Max-LSN Priority Queue:
To support interleaved transactions and deterministic recovery, Undo maintains a priority queue of `(-last_lsn, tx_id)`:
1. Active losers in state `RUNNING` from Analysis are queued.
2. Incomplete aborted transactions in state `ABORTED` with uncompensated updates are queued.
3. At each step, the record with the globally largest LSN across all loser transactions is popped and processed.
4. If the popped record is an `UPDATE`:
   - Page before-image is applied at `record.offset`.
   - CLR is appended to WAL with `undo_next_lsn = record.prev_lsn`.
   - WAL is flushed and made durable.
   - `page_lsn` is updated to the CLR LSN and the dirty page is flushed through `BufferManager`.
   - If `undo_next_lsn` is non-null, `(-undo_next_lsn, tx_id)` is queued; otherwise an `ABORT` completion record is appended.
5. If the popped record is a `CLR`:
   - Skip undoing.
   - If `record.undo_next_lsn` is non-null, queue `(-record.undo_next_lsn, tx_id)`; otherwise append an `ABORT` completion record.
6. If the popped record is a `BEGIN`:
   - Append an `ABORT` completion record and mark the transaction completed.
7. If the popped record is an `ABORT`:
   - If `record.prev_lsn` is non-null, follow `record.prev_lsn` to undo prior updates.

---

## 3. Handling Aborted Transactions

A critical requirement of ARIES is that an `ABORT` record does **not** prove physical rollback was completed:
- If a transaction aborts (e.g. via `tm.abort_transaction()`), an `ABORT` record is written. If the database crashes immediately thereafter, the transaction's updates remain on disk/pages without having been rolled back.
- Phase 7 Analysis classifies transactions as:
  - `winners`: `tx.state == TransactionState.COMMITTED`
  - `losers`: `tx.state == TransactionState.RUNNING`
- Undo inspects the transaction table and applies `_is_transaction_rollback_incomplete(tx_id, tx.last_lsn)`:
  - It traces the log chain backward from `tx.last_lsn`.
  - If an uncompensated `UPDATE` is found, the transaction is added to the Undo queue.
  - If all updates were already compensated (or if no updates were ever logged), the transaction is recognized as fully completed and is **not** re-undone.

---

## 4. WAL-Before-Page-Write & Safe Pinning

1. **Ordering Invariant**:
   - Before any dirty page modified by Undo is written to disk, the corresponding CLR is appended and flushed to disk (`lm.flush()`).
   - `BufferManager.flush_page()` enforces `lm.flushed_lsn >= page_lsn`.
2. **Safe Pinning**:
   - In `undo.py`, page fetching and writes are wrapped in a `try...finally` block.
   - If bounds validation, CLR appending, or fault injection raises an exception, the page is safely unpinned and not leaked in the buffer pool.

---

## 5. Fault Injection & Restart Safety

Three recovery-specific fault points were integrated into [`backend/app/fault/constants.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/fault/constants.py):
1. `DURING_UNDO_BEFORE_CLR_FLUSH`: Simulates crash before CLR durability.
2. `DURING_UNDO_AFTER_CLR_FLUSH_BEFORE_PAGE_FLUSH`: Simulates crash when CLR is durable in WAL but page is not yet flushed.
3. `DURING_UNDO_AFTER_PAGE_FLUSH`: Simulates crash immediately after the page is flushed.

### Subprocess Crash Test Results:
- Tests 21, 22, and 23 spawn isolated Python subprocesses with `fault_injector.enable(point, exit_code=42)`.
- The parent process verifies process death (`exit_code == 42`), inspects persistent `.wal` and `.db` files, and successfully executes restart recovery.
- In Test 22 (crash between CLR flush and page flush), restart recovery runs Redo (which replays the CLR compensation image onto the page), and Undo skips the already-compensated update via `undo_next_lsn`, avoiding double compensation.

---

## 6. Test Suite Verification (All 29 Scenarios)

The test suite in [`backend/tests/test_undo.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_undo.py) covers all 29 mandatory scenarios:

| # | Test Name | Purpose / Assertion | Status |
|---|-----------|---------------------|--------|
| 1 | `test_basic_single_update_undo` | Single update loser rolled back, CLR generated, ABORT appended | PASSED |
| 2 | `test_multiple_updates_in_one_transaction` | 3 updates in 1 tx undone in reverse order (LSN 4 -> 3 -> 2) | PASSED |
| 3 | `test_multiple_loser_transactions` | T1 and T2 losers undone across separate pages | PASSED |
| 4 | `test_interleaved_transactions` | Global max-LSN priority queue undoes interleaved updates | PASSED |
| 5 | `test_committed_winner_never_undone` | T1 committed preserved; T2 loser undone | PASSED |
| 6 | `test_abort_record_with_incomplete_rollback` | Aborted tx with uncompensated updates is detected and undone | PASSED |
| 7 | `test_fully_completed_aborted_transaction_is_not_undone_again` | Fully rolled back aborted tx is not classified as loser | PASSED |
| 8 | `test_correct_before_image_restoration` | Before-image restored at exact offset without disturbing page | PASSED |
| 9 | `test_clr_creation_and_serialization` | Deterministic binary serialization, properties, CRC validation | PASSED |
| 10 | `test_correct_clr_prev_lsn_chain` | CLR prev_lsn points to tx log tail, not undone record's prev_lsn | PASSED |
| 11 | `test_correct_undo_next_lsn` | Validates undo_next_lsn < lsn, rejects non-CLR assignment | PASSED |
| 12 | `test_multiple_updates_to_same_page` | Multiple losers updating page 0 restored cleanly | PASSED |
| 13 | `test_updates_spanning_multiple_pages` | 4 distinct pages modified and restored | PASSED |
| 14 | `test_loser_transaction_with_no_update_records` | Loser with no updates completes with ABORT record | PASSED |
| 15 | `test_empty_loser_set` | Clean UndoResult returned immediately when 0 losers | PASSED |
| 16 | `test_invalid_update_bounds` | Offset + length > PAGE_SIZE fails safely without mutating page | PASSED |
| 17 | `test_wal_before_page_write_ordering` | Verifies flushed_lsn >= page_lsn before page write | PASSED |
| 18 | `test_clr_durability` | CLR is verified durable on disk before page flush | PASSED |
| 19 | `test_idempotent_or_safely_resumable_undo` | Second recovery run performs 0 undo steps | PASSED |
| 20 | `test_restart_after_partial_undo` | Crash midway through Undo resumes from CLR undo_next_lsn | PASSED |
| 21 | `test_crash_before_clr_flush` | Subprocess crash before CLR flush; recovery re-executes cleanly | PASSED |
| 22 | `test_crash_after_clr_flush_before_page_flush` | Subprocess crash after CLR flush; Redo replays CLR, Undo jumps | PASSED |
| 23 | `test_crash_after_page_flush` | Subprocess crash after page flush; disk verified, recovery completes | PASSED |
| 24 | `test_recovery_continuing_from_clr_undo_next_lsn` | Encounters existing CLR, jumps to undo_next_lsn | PASSED |
| 25 | `test_multiple_consecutive_crashes_during_undo` | Crash -> CLR 1 -> Crash -> CLR 2 -> Restart -> Complete | PASSED |
| 26 | `test_final_loser_transaction_completion` | Appends durable ABORT completion record, tracked in completed_txs | PASSED |
| 27 | `test_undo_result_serialization_and_immutability` | to_dict(), __repr__(), equality, immutability of result | PASSED |
| 28 | `test_existing_wal_compatibility` | All record types (BEGIN, COMMIT, ABORT, UPDATE, CHECKPOINT) work | PASSED |
| 29 | `test_missing_log_records_and_invalid_chains` | Missing records and cycles raise UndoError without hangs | PASSED |

---

## 7. Known Limitations & Phase 10 Readiness

1. **Automatic Restart Manager Orchestration**:
   - Completed in Phase 10 via `RecoveryManager.recover()`.
2. **Phase 10 Readiness**:
   - Analysis (Phase 7), Redo (Phase 8), and Undo (Phase 9) are fully integrated into the complete ARIES crash recovery orchestrator in Phase 10.

---

# Phase 10: Complete ARIES Crash-Recovery Orchestration — Walkthrough

## Overview

Phase 10 completes the implementation and verification of the **RecoveryManager**, which coordinates the existing ARIES recovery components (Analysis, Redo, and Undo) into a unified, restart-safe, and crash-resilient recovery workflow.

---

## 1. Architecture of RecoveryManager

The `RecoveryManager` resides in [`backend/app/recovery/recovery_manager.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/recovery/recovery_manager.py) and coordinates:
```
           +---------------------------------------------+
           |               RecoveryManager               |
           +---------------------------------------------+
                                  |
                                  v
           +---------------------------------------------+
           |                   STAGE 1                   |
           |                  Analysis                   |
           |  (Scans WAL forward, builds tx table & DPT) |
           +---------------------------------------------+
                                  |
                   Fault: AFTER_ANALYSIS_BEFORE_REDO
                                  v
           +---------------------------------------------+
           |                   STAGE 2                   |
           |                    Redo                     |
           |      (Repeats history from min recLSN)      |
           +---------------------------------------------+
                                  |
                    Fault: AFTER_REDO_BEFORE_UNDO
                                  v
           +---------------------------------------------+
           |                   STAGE 3                   |
           |                    Undo                     |
           | (Rolls back active losers in max-LSN order) |
           +---------------------------------------------+
                                  |
                  Fault: AFTER_UNDO_BEFORE_COMPLETE
                                  v
           +---------------------------------------------+
           |                   STAGE 4                   |
           |          Verification & Persistence         |
           |  (flush_all_pages, build RecoveryResult)    |
           +---------------------------------------------+
```

---

## 2. RecoveryResult Structure

`RecoveryResult` (defined in [`backend/app/recovery/recovery_state.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/recovery/recovery_state.py)) encapsulates the entire recovery process:
- **`is_success` / `success`**: Boolean indicating full recovery success.
- **`stage`**: String indicating completion status (`COMPLETE`, `FAILED_AT_ANALYSIS`, `FAILED_AT_REDO`, `FAILED_AT_UNDO`).
- **`error_message`**: Descriptive error message if failure occurred.
- **`analysis_result`**: Subordinate `AnalysisResult`.
- **`redo_result`**: Subordinate `RedoResult`.
- **`undo_result`**: Subordinate `UndoResult`.
- **`pages_modified`**: Sorted unique list of all pages modified during Redo and Undo.
- **`winners` & `losers`**: Transaction IDs identified and resolved.
- **`clrs_generated` & `updates_undone`**: Undo operations recorded.

---

## 3. Test Suite Verification (All 19 Scenarios)

The test suite in [`backend/tests/test_recovery_manager.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_recovery_manager.py) validates all required scenarios:

| # | Test Scenario | Verified Behavior | Status |
|---|---------------|-------------------|--------|
| 1 | `test_recovery_with_no_transactions_requiring_undo` | Winner-only or clean database; 0 undo operations, complete result | PASSED |
| 2 | `test_recovery_of_committed_transaction` | Unflushed dirty page of committed transaction replayed by Redo | PASSED |
| 3 | `test_recovery_of_single_loser_transaction` | Single loser rolled back with CLR and completion ABORT record | PASSED |
| 4 | `test_recovery_of_multiple_interleaved_loser_transactions` | Interleaved losers across multiple pages rolled back via priority queue | PASSED |
| 5 | `test_recovery_from_valid_checkpoint` | Checkpoint snapshot loaded, analysis scans from checkpoint begin | PASSED |
| 6 | `test_recovery_with_incomplete_abort` | Incomplete aborted transaction recognized and fully rolled back | PASSED |
| 7 | `test_recovery_after_durable_clr_before_page_flush` | Crash after CLR flush: Redo replays CLR, Undo skips duplicate | PASSED |
| 8 | `test_recovery_after_crash_during_undo` | Crash midway through Undo resumes from CLR undo_next_lsn | PASSED |
| 9 | `test_repeated_crash_and_recovery_cycles` | Subprocess crash at AFTER_REDO_BEFORE_UNDO followed by restart | PASSED |
| 10 | `test_idempotent_recovery_after_completion` | Running recovery twice on completed state does 0 undo steps | PASSED |
| 11 | `test_invalid_or_corrupted_wal_causes_clear_failure` | Corrupted magic/CRC raises LogCorruptionError; recovery fails | PASSED |
| 12 | `test_stage_ordering_is_analysis_redo_undo` | Proves strictly sequential Analysis -> Redo -> Undo execution | PASSED |
| 13 | `test_failed_stage_prevents_later_stages_from_reporting_success` | Failure at Redo prevents Undo and reports FAILED_AT_REDO | PASSED |
| 14 | `test_recovery_result_structure_and_immutability` | Properties, immutability, to_dict, repr, and eq validation | PASSED |
| 15 | `test_existing_phase_0_to_9_behavior_compatibility` | Standalone Analysis, Redo, Undo, and LogManager APIs work | PASSED |
| 16 | `test_loser_transaction_with_no_update_records` | Loser with no updates completes with ABORT record cleanly | PASSED |
| 17 | `test_recover_from_files_convenience_api` | RecoveryManager.recover_from_files opens, recovers, and closes | PASSED |
| 18 | `test_constructor_validations_and_closed_managers` | Closed or None managers rejected with RecoveryManagerError | PASSED |
| 19 | `test_crash_at_after_undo_before_complete_and_restart_recovery` | Subprocess crash at AFTER_UNDO_BEFORE_COMPLETE followed by restart | PASSED |

---

# Phase 11: ARIES Integration and Correctness Testing Walkthrough

## 1. Overview & Objectives

Phase 11 established a dedicated integration and correctness test matrix ([`backend/tests/test_integration_matrix.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_integration_matrix.py)) verifying end-to-end interactions across the entire database engine:
- Storage (`DiskManager`)
- Buffer pool (`BufferManager` with LRU eviction and pin safety)
- Write-Ahead Logging (`LogManager` with LSNs and CRC32 checks)
- Transaction control (`TransactionManager`)
- Checkpointing (`CheckpointManager` with fuzzy snapshots)
- Recovery phases (`Analysis`, `Redo`, `Undo` with CLRs)
- Orchestration (`RecoveryManager` and `RecoveryResult`)
- Crash fault injection (`FaultInjector`)

## 2. Integration Test Matrix (20 Scenarios)

All 20 required integration scenarios were executed and validated:

| # | Integration Scenario | Test Name | Invariants Verified | Status |
|---|----------------------|-----------|---------------------|--------|
| 1 | Clean database | `test_scenario_01_clean_database_no_recovery_required` | Zero spurious work, clean state preserved | PASSED |
| 2 | Committed transaction restart | `test_scenario_02_committed_transaction_survives_restart` | Committed updates never undone; replayed to disk | PASSED |
| 3 | Incomplete transaction undo | `test_scenario_03_incomplete_transaction_undone` | Active uncommitted loser rolled back to before-image | PASSED |
| 4 | Interleaved mixed transactions | `test_scenario_04_multiple_interleaved_transactions_mixed_outcomes` | Winners preserved, losers undone via priority queue | PASSED |
| 5 | Multiple updates on same page | `test_scenario_05_multiple_updates_same_transaction_same_page` | Reverse-order rollback, valid CLR undoNextLSN chains | PASSED |
| 6 | Disjoint pages isolation | `test_scenario_06_multiple_transactions_different_pages` | Non-interference across multiple pages | PASSED |
| 7 | Multi-page transaction crash | `test_scenario_07_transaction_updating_multiple_pages` | Complete rollback across pages 0, 1, 2 | PASSED |
| 8 | Valid checkpoint recovery | `test_scenario_08_recovery_with_valid_checkpoint` | Snapshot state loaded, scan begins from checkpoint begin | PASSED |
| 9 | Incomplete checkpoint crash | `test_scenario_09_recovery_checkpoint_absent_or_incomplete` | Crash at CHECKPOINT_BEGIN ignored; log head scan used | PASSED |
| 10 | Crash during Redo | `test_scenario_10_crash_during_redo_subprocess_restart` | Subprocess crash at AFTER_REDO_BEFORE_UNDO resumes | PASSED |
| 11 | Crash during Undo | `test_scenario_11_crash_during_undo_subprocess_restart` | Subprocess crash mid-Undo resumes from durable CLR | PASSED |
| 12 | Crash after CLR flush before page | `test_scenario_12_crash_after_clr_flush_before_page_flush_subprocess` | Redo replays CLR, Undo jumps to avoid double undo | PASSED |
| 13 | Crash after Undo complete | `test_scenario_13_crash_after_undo_before_complete_subprocess` | Subprocess crash at AFTER_UNDO_BEFORE_COMPLETE resumes | PASSED |
| 14 | Repeated recovery idempotence | `test_scenario_14_repeated_recovery_idempotence` | Subsequent runs perform 0 redo/undo, state identical | PASSED |
| 15 | Corrupted WAL & cyclic chains | `test_scenario_15_corrupted_wal_and_invalid_lsn_chains` | LogCorruptionError & cyclic UndoError reported | PASSED |
| 16 | Invalid bounds / page refs | `test_scenario_16_invalid_transaction_or_page_references` | Bounds overflow & non-existent page fail safely | PASSED |
| 17 | Failures at each stage | `test_scenario_17_recovery_failures_at_each_stage` | FAILED_AT_ANALYSIS, REDO, UNDO precisely recorded | PASSED |
| 18 | File-based restart recovery | `test_scenario_18_file_based_recovery_across_restarts` | `recover_from_files` lifecycle across processes | PASSED |
| 19 | Buffer pin safety | `test_scenario_19_buffer_pool_pin_safety_and_page_consistency` | Pin count == 0 and is_dirty == False for all frames | PASSED |
| 20 | Public API compatibility | `test_scenario_20_public_apis_and_backward_compatibility` | Standalone and classmethods fully operational | PASSED |

## 3. Verified ARIES Invariants

1. **Committed Transactions Durability**: Committed transactions are never undone during recovery, and their unflushed updates are guaranteed persisted to disk by Redo.
2. **Incomplete Transactions Atomicity**: Loser transactions are completely rolled back using recorded before-images, terminating with durable `ABORT` records.
3. **Repeatable Redo**: Redo scans from `redo_start_lsn` and applies all eligible updates idempotently, skipping pages where `page_lsn >= record.lsn`.
4. **Compensation Log Records (CLRs)**: CLRs are written and flushed before compensating page writes; subsequent recovery runs follow `undo_next_lsn` to avoid duplicate compensation.
5. **Process Crash Resiliency**: Genuine subprocess termination via `FaultInjector` confirms recovery restarts cleanly and completes from persistent `.wal` and `.db` files.
6. **Buffer Pool Pin Safety**: All frames in the buffer pool are released (`pin_count == 0`) and clean (`is_dirty == False`) upon recovery completion.

---

# Phase 12: Synthetic Transaction Workload Generator Walkthrough

## 1. Overview & Objectives

Phase 12 established a configurable, reproducible synthetic transaction workload generator under `backend/app/workload/`:
- [`backend/app/workload/config.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/config.py): `WorkloadConfig` parameters and distribution validation.
- [`backend/app/workload/models.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/models.py): `OperationType`, `TransactionOutcome`, `WorkloadOperation`, `WorkloadPlan`, `WorkloadResult`.
- [`backend/app/workload/generator.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/generator.py): `WorkloadGenerator` for deterministic plan generation with random seeds.
- [`backend/app/workload/executor.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/executor.py): `WorkloadExecutor` exercising real database transactions, buffer frames, WAL logging, checkpoints, and crash-recovery experiments.
- [`backend/app/workload/exceptions.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/workload/exceptions.py): Domain-specific exception hierarchy.

## 2. Workload Test Suite (17 Scenarios)

All 17 verification scenarios in [`backend/tests/test_workload_generator.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_workload_generator.py) passed:

| # | Scenario | Test Name | Key Invariants Verified | Status |
|---|----------|-----------|--------------------------|--------|
| 1 | Config validation | `test_01_config_validation_and_invalid_inputs` | Rejection of negative counts, out-of-bounds ratios, ratio sums != 1.0, bad types | PASSED |
| 2 | Deterministic plan generation | `test_02_deterministic_plan_generation_fixed_seed` | Fixed config and seed produce identical operation plans | PASSED |
| 3 | Seed differentiation | `test_03_different_seeds_produce_different_plans` | Distinct seeds generate distinct operations and targets | PASSED |
| 4 | Valid mixed workload | `test_04_valid_read_update_workload` | Both reads and updates executed cleanly against database | PASSED |
| 5 | Committed durability | `test_05_committed_transaction_durability` | 100% committed transaction updates durable on disk across restart | PASSED |
| 6 | Aborted rollback | `test_06_aborted_transaction_rollback` | Aborted transactions roll back via CLRs restoring initial page patterns | PASSED |
| 7 | Incomplete recovery | `test_07_incomplete_transactions_and_subsequent_recovery` | Uncommitted loser transactions rolled back by RecoveryManager | PASSED |
| 8 | Interleaved execution | `test_08_interleaved_transactions` | Concurrent transaction operations interleaved safely without stuck pins | PASSED |
| 9 | Multi-page operation | `test_09_workloads_operating_on_multiple_pages` | Uniform operation spread across 8 distinct pages with no cross-interference | PASSED |
| 10 | Boundary workload sizes | `test_10_zero_operation_and_minimum_size_workloads` | Zero-transaction (0 ops) and minimum single-transaction (2 ops) execute safely | PASSED |
| 11 | Multi-run reproducibility | `test_11_reproducibility_across_independent_runs` | Separate runs with identical seed generate byte-for-byte identical database files | PASSED |
| 12 | Subprocess crash experiment | `test_12_crash_injection_and_restart_recovery` | Subprocess terminates at DURING_TRANSACTION_COMMIT and recovers from files | PASSED |
| 13 | Result serialization | `test_13_result_counts_and_serialization` | WorkloadResult serialization, property immutability, and dict formatting | PASSED |
| 14 | Execution failure safety | `test_14_invalid_execution_parameters_fail_safely` | Missing paths or unconfigured crash points raise clear errors without file corruption | PASSED |
| 15 | API compatibility | `test_15_compatibility_with_existing_apis` | Seamless interoperability with CheckpointManager and RecoveryManager | PASSED |
| 16 | Validator error detection | `test_16_validator_detects_disk_mismatch_and_recovery_failures` | Persistent disk byte mismatch, unallocated page, recovery failure, winner/loser discrepancy | PASSED |
| 17 | Repeated updates rollback | `test_17_repeated_updates_same_page_and_rollback` | Multiple repeated updates on same page/slot rolled back via CLRs restoring baseline data | PASSED |

## 3. Verified Correctness Invariants

1. **Deterministic Separation**: Operation plan generation is purely deterministic and completely decoupled from execution side effects.
2. **Durability & Atomicity**: Committed transactions permanently persist; aborted transactions roll back cleanly via CLRs at runtime; incomplete transactions roll back upon crash restart recovery.
3. **No Uncommitted Overwrite Contention**: Slot-based page offset partitioning guarantees that concurrent uncommitted transactions do not create uncommitted dirty-write dependencies.
4. **Buffer Pool Pin Safety**: Pin counts are strictly 0 and frames clean after execution across both normal and recovery paths.

---

# Phase 13: ARIES Performance Benchmarking Framework Walkthrough

## 1. Overview & Architecture

Phase 13 establishes a reproducible, modular performance benchmarking framework under `backend/app/benchmark/`:
- [`backend/app/benchmark/config.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/config.py): `BenchmarkConfig` validation, repetitions, warmup, seeds, and scenario parameters.
- [`backend/app/benchmark/models.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/models.py): Structured models for `EnvironmentMetadata`, `WorkloadRunMetrics`, `RecoveryRunMetrics`, `BenchmarkRunResult`, `MetricDistribution`, and `BenchmarkScenarioResult`.
- [`backend/app/benchmark/scenarios.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/scenarios.py): Built-in preset scenario catalogue covering varying sizes, read/write ratios, contention hotspots, abort rates, recovery-only tests, and crash experiments.
- [`backend/app/benchmark/runner.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/runner.py): Execution engine managing isolated temporary directories, separate setup vs. execution timing intervals, and post-run database correctness validation.
- [`backend/app/benchmark/exporter.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/exporter.py): Exporter generating structured JSON files and CSV files with explicit units.
- [`backend/app/benchmark/cli.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/cli.py): CLI interface (`python -m app.benchmark`) with summary console tables.
- [`backend/app/benchmark/exceptions.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/benchmark/exceptions.py): Domain-specific benchmarking exception hierarchy.

## 2. Benchmark Test Suite (17 Scenarios)

All 17 verification scenarios in [`backend/tests/test_benchmark.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_benchmark.py) passed:

| # | Scenario | Test Name | Key Invariants Verified | Status |
|---|----------|-----------|--------------------------|--------|
| 1 | Config validation | `test_01_benchmark_config_validation` | Rejection of empty names, non-positive repetitions, negative warmup, bad types | PASSED |
| 2 | Small benchmark execution | `test_02_small_benchmark_scenario_execution` | Full execution of small_workload producing valid measured & warmup runs | PASSED |
| 3 | Metric calculation & units | `test_03_correct_metric_calculations_and_units` | TPS, ops/sec, completed txs, and recovery metrics calculated with exact arithmetic | PASSED |
| 4 | Zero-duration division safeguards | `test_04_zero_operation_and_zero_duration_safeguards` | Zero duration, negative durations, and empty lists handled without ZeroDivisionError | PASSED |
| 5 | Repetitions & aggregate statistics | `test_05_multiple_repetitions_and_aggregate_statistics` | Mean, median, min, max, std_dev, and p95 distributions calculated correctly | PASSED |
| 6 | Fixed-seed reproducibility | `test_06_fixed_seed_reproducibility` | Identical seeds generate identical operation counts and outcomes across runs | PASSED |
| 7 | File isolation across runs | `test_07_isolation_of_database_and_wal_files_across_runs` | Per-run temp directories cleaned up safely without leaking files | PASSED |
| 8 | Recovery-only timing | `test_08_recovery_only_timing_and_result_collection` | Crash state prepared outside timing; Analysis, Redo, Undo timed separately | PASSED |
| 9 | Crash-recovery scenario | `test_09_optional_crash_recovery_scenario_correctness` | Subprocess fault injection terminates cleanly and parent recovers with metrics | PASSED |
| 10 | Failed validation handling | `test_10_failed_workload_validation_marked_as_failed` | State mismatch correctly marks run and scenario as failed, not successful | PASSED |
| 11 | CSV export schema | `test_11_csv_export_schema_and_contents` | CSV file exported with standard headers, units, and matching row counts | PASSED |
| 12 | JSON export schema | `test_12_json_export_schema_and_contents` | JSON file exported with config, environment, aggregates, and run details | PASSED |
| 13 | Metadata serialization | `test_13_metadata_serialization` | Hardware/platform metadata captured and serialized into JSON-safe dict | PASSED |
| 14 | API & scenario compatibility | `test_14_compatibility_with_workload_and_recovery_manager` | Preset scenarios (read_heavy, update_heavy, etc.) execute seamlessly | PASSED |
| 15 | Error handling & cleanup | `test_15_error_handling_and_cleanup_of_temporary_files` | Invalid paths raise BenchmarkExportError; unknown scenarios raise ConfigError | PASSED |
| 16 | p95 interpolation & monotonicity | `test_16_p95_linear_interpolation_monotonicity` | Linear interpolation preserves monotonic percentile ordering min <= median <= p95 <= max | PASSED |
| 17 | Crash timing separation | `test_17_crash_benchmark_separates_workload_and_recovery_timing` | Subprocess crash execution and parent restart recovery timed as distinct intervals | PASSED |

## 3. Verified Correctness & Measurement Invariants

1. **Separation of Setup and Timing**: Database page initialization and crash-state preparation are executed outside the measured intervals; timing is captured strictly around the workload execution loop or recovery operations.
2. **Correctness Before Timing**: Every run is validated via `WorkloadExecutor.validate_database_state()`. Runs with state discrepancies are flagged as failed and excluded from valid statistical distributions.
3. **Stage-Level Recovery Timing**: When enabled, Analysis, Redo, and Undo durations are measured using high-resolution monotonic timers (`time.perf_counter()`), without altering core recovery abstractions.
4. **Environment Transparency**: Machine platform, CPU count, Python version, and execution timestamps are recorded with every benchmark result for reproducibility.
5. **Division-Safe Metrics**: Zero elapsed time and zero operation edge cases safely produce `0.0` throughput instead of throwing exceptions.
