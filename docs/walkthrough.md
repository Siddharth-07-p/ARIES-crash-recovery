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

---

# Phase 14: Database Monitoring and Statistics APIs Walkthrough

## 1. Overview & Architecture

Phase 14 delivers read-only monitoring and statistics REST APIs under the `/api/monitoring/*` namespace:
- [`backend/app/monitoring/models.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/monitoring/models.py): Pydantic response models providing strongly-typed JSON serialization with explicit units.
- [`backend/app/monitoring/state.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/monitoring/state.py): `MonitoringState` thread-safe registry that extracts read-only metrics directly from active subsystem managers without mutating buffer frames, transaction state, or WAL logs.
- [`backend/app/monitoring/router.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/monitoring/router.py): Dedicated FastAPI APIRouter hosting all 7 monitoring endpoints.
- [`backend/app/main.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/main.py): Registers the monitoring router alongside existing `/` and `/api/health` routes.

## 2. Implemented Endpoints & Sample JSON Responses

### 1. `GET /api/monitoring/status`
Returns general service status, database subsystem readiness, and crash recovery status.
```json
{
  "service": "ARIES Crash Recovery System",
  "status": "operational",
  "database_initialized": true,
  "recovery_status": "recovered",
  "latest_recovery_success": true,
  "timestamp": "2026-10-19T18:08:42Z"
}
```

### 2. `GET /api/monitoring/transactions`
Returns transaction manager metrics including started, committed, aborted, and active counts and active IDs.
```json
{
  "status": "available",
  "total_started": 3,
  "total_committed": 1,
  "total_aborted": 1,
  "active_count": 1,
  "active_transaction_ids": [3]
}
```

### 3. `GET /api/monitoring/wal`
Returns Write-Ahead Logging statistics without rescanning log records or mutating disk state.
```json
{
  "status": "available",
  "current_lsn": 14,
  "flushed_lsn": 14,
  "total_records": 14,
  "wal_file_size_bytes": 1024,
  "is_closed": false
}
```

### 4. `GET /api/monitoring/buffer-pool`
Returns buffer frame allocation, resident pages, pinned pages, and dirty frame counts.
```json
{
  "status": "available",
  "pool_size_frames": 5,
  "resident_pages_count": 2,
  "pinned_frames_count": 1,
  "dirty_pages_count": 1,
  "available_frames_count": 4,
  "is_closed": false
}
```

### 5. `GET /api/monitoring/recovery`
Returns the outcome and stage breakdown of the latest recovery operation (`not_run` if recovery has not run in the current application lifecycle).
```json
{
  "status": "available",
  "is_success": true,
  "stage": "complete",
  "error_message": null,
  "winners_count": 1,
  "losers_count": 1,
  "records_redone": 3,
  "clrs_generated": 1,
  "updates_undone": 1,
  "pages_modified_count": 1,
  "analysis_duration_s": 0.0012,
  "redo_duration_s": 0.0021,
  "undo_duration_s": 0.0018,
  "total_duration_s": 0.0051
}
```

### 6. `GET /api/monitoring/workload`
Returns summary statistics of the latest executed synthetic transaction workload (`not_available` if none has run).
```json
{
  "status": "available",
  "planned_operations": 20,
  "executed_operations": 20,
  "transactions_started": 5,
  "transactions_committed": 4,
  "transactions_aborted": 1,
  "transactions_incomplete": 0,
  "reads_executed": 10,
  "updates_executed": 10,
  "is_valid": true,
  "crash_configured": false,
  "crash_observed": false,
  "fault_point": null
}
```

### 7. `GET /api/monitoring/benchmark`
Returns aggregate statistics of the latest executed benchmark scenario (`not_available` if none has run).
```json
{
  "status": "available",
  "scenario_name": "small_workload",
  "is_success": true,
  "total_runs": 4,
  "measured_runs_count": 3,
  "warmup_runs_count": 1,
  "valid_runs_count": 3,
  "failed_runs_count": 0,
  "mean_workload_duration_s": 0.0452,
  "mean_tx_throughput_tps": 221.24,
  "mean_op_throughput_ops": 884.96,
  "mean_recovery_duration_s": null
}
```

## 3. Metric Mapping, Units, and Data Sources

| Endpoint | Field | Type / Unit | Underlying Source |
|---|---|---|---|
| `/status` | `status` | string (`operational`/`degraded`/`uninitialized`) | Evaluated from registered managers, closed flags, and recovery success |
| `/status` | `recovery_status` | string (`not_run`/`recovered`/`failed`) | `RecoveryManager.last_result` or recorded result |
| `/transactions` | `total_started`, `total_committed`, `total_aborted` | integer (count) | `TransactionManager` lifecycle totals (`get_transaction_table()`) |
| `/transactions` | `active_count`, `active_transaction_ids` | integer, list[int] | `TransactionManager.get_active_transactions()` |
| `/wal` | `current_lsn`, `flushed_lsn` | uint64 | `LogManager.next_lsn - 1`, `LogManager.flushed_lsn` |
| `/wal` | `total_records`, `wal_file_size_bytes` | int, bytes | `len(LogManager._records)`, `os.path.getsize(wal_path)` |
| `/buffer-pool` | `pool_size_frames`, `resident_pages_count` | count | `BufferManager.pool_size`, `len(bm.get_resident_pages())` |
| `/buffer-pool` | `pinned_frames_count`, `dirty_pages_count` | count | `bm.get_pin_count(pid) > 0`, `bm.is_dirty(pid)` |
| `/buffer-pool` | `available_frames_count` | count | `len(bm._free_frames) + bm._replacer.size()` (0 when closed) |
| `/recovery` | `records_redone`, `clrs_generated`, `updates_undone` | count | `RecoveryResult` fields |
| `/recovery` | `analysis_duration_s`, `redo_duration_s`, `undo_duration_s` | float (seconds) | Monotonic timer measurements recorded during recovery |
| `/workload` | `planned_operations`, `executed_operations` | count | `WorkloadResult` fields |
| `/benchmark` | `mean_tx_throughput_tps`, `mean_op_throughput_ops` | tx/sec, ops/sec | `BenchmarkScenarioResult.aggregates` |

## 4. Test Suite Verification (All 17 Scenarios)

All 17 verification scenarios in [`backend/tests/test_monitoring_api.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_monitoring_api.py) passed:

| # | Scenario | Test Name | Key Invariants Verified | Status |
|---|----------|-----------|--------------------------|--------|
| 1 | Status transitions | `test_01_monitoring_status_response` | Uninitialized, operational, and degraded statuses correctly reflected | PASSED |
| 2 | Transaction stats | `test_02_transaction_statistics_when_transactions_exist` | Accurately aggregates committed, aborted, active counts and IDs | PASSED |
| 3 | WAL stats | `test_03_wal_statistics_and_unavailable_states` | LSNs, record count, and WAL file size reported cleanly | PASSED |
| 4 | Buffer pool stats | `test_04_buffer_pool_statistics_and_unavailable_states` | Frame capacity, resident, pinned, dirty, and available counts | PASSED |
| 5 | Recovery stats | `test_05_recovery_statistics_before_and_after_recovery` | `not_run` prior to recovery; winners, losers, redos, undos after | PASSED |
| 6 | Recovery failure | `test_06_recovery_failure_reporting` | Failed recovery status, stage, and error message serialized | PASSED |
| 7 | Workload unrun | `test_07_workload_summary_when_no_workload_has_run` | Clean `not_available` response prior to workload run | PASSED |
| 8 | Workload available | `test_08_workload_summary_when_results_are_available` | Operation breakdown and validation flags accurately exposed | PASSED |
| 9 | Benchmark unrun | `test_09_benchmark_summary_when_no_benchmark_has_run` | Clean `not_available` response prior to benchmark run | PASSED |
| 10 | Benchmark available | `test_10_benchmark_summary_when_results_are_available` | Aggregates, throughputs (TPS, ops/s), and run counts exposed | PASSED |
| 11 | Response schemas | `test_11_response_schemas_and_data_types` | Validates Pydantic types across all 7 monitoring endpoints | PASSED |
| 12 | Non-mutation invariant | `test_12_requests_not_mutating_database_or_wal_state` | Repeated GETs cause 0 change to LSNs, log records, buffer, DPT, or disk | PASSED |
| 13 | Health compatibility | `test_13_existing_api_health_compatibility` | `/api/health` returns status: healthy and matching format | PASSED |
| 14 | Missing components | `test_14_missing_or_uninitialized_components` | Unregistered components report safe uninitialized states | PASSED |
| 15 | Closed managers | `test_15_error_handling_for_closed_components` | Closed BufferManager/TransactionManager handled without exceptions | PASSED |
| 16 | API route regression | `test_16_regression_coverage_for_existing_api_routes` | Root `/` and `/api/health` backward compatibility verified | PASSED |
| 17 | Concurrency & Availability | `test_17_concurrency_and_closed_race_safety` | Multi-threaded concurrency safety, exact allocation availability, degraded status on recovery failure | PASSED |

## 5. Security and Non-Mutation Invariants

1. **Strictly Read-Only**: Endpoints only inspect in-memory properties and filesystem file sizes. No database transactions, WAL records, or buffer allocations are performed.
2. **Path Sanitization**: Monitoring endpoints accept no client-supplied file paths or filesystem scan requests, preventing path traversal vulnerabilities.
3. **No Unauthenticated Secret Leakage**: No server host paths, credentials, or internal memory addresses are serialized.
4. **Resilience to Closed Subsystems**: If a subsystem manager is closed or uninitialized, endpoints return documented degraded/uninitialized states rather than throwing unhandled `500 Internal Server Error` exceptions.

---

# Phase 15: React Monitoring Dashboard — Walkthrough

## Overview

Phase 15 implements a responsive, accessible React + TypeScript monitoring dashboard for the ARIES Crash Recovery project. Built on top of React 18, Vite, TypeScript, and Lucide React icons, the dashboard consumes all seven Phase 14 monitoring endpoints (`/api/monitoring/*`) as well as the `/api/health` probe without mutating backend state, fabricating missing metrics, or triggering heavy database operations.

---

## 1. Dashboard Architecture and Component Hierarchy

The frontend architecture separates network polling, data normalization, status evaluation, and UI rendering:

```
App.tsx
├── Header (App title, Live indicator, ARIES Architecture badges)
├── OverviewSection (Engine status, DB init state, Recovery status, Poll interval selector, Manual refresh, Offline banner)
└── Dashboard Grid (2-column responsive layout)
    ├── Left Column
    │   ├── TransactionSection (Active tx list, Started/Committed/Aborted cards)
    │   ├── WalSection (LSN progression, Sync lag, Records count, File size)
    │   └── BufferPoolSection (Pool capacity, Resident/Pinned/Dirty/Free frames, Frame breakdown bar)
    └── Right Column
        ├── RecoverySection (Status banner, Winners/Losers, Redo/Undo stats, Stage durations)
        ├── WorkloadSection (Operation counts, Read/Update breakdown, Validation badge, Fault status)
        └── BenchmarkSection (Scenario info, Throughput [TPS, ops/s], Latency, Run counts)
```

Reusable presentation components:
- **`MetricCard`**: Standardized card rendering metric labels, formatted values, units, supplementary hints, and shimmer skeleton states. Renders `—` when values are nullish.
- **`StatusBadge`**: Semantic pill badges with pulsing/static color-coded dots representing system health (`operational`, `degraded`, `uninitialized`), recovery outcomes (`recovered`, `failed`, `not_run`), availability (`available`, `not_available`), and file handles (`open`, `closed`).

---

## 2. Endpoint-to-Component Mapping

| API Endpoint | Response Type | Target Component | Displayed Metrics |
|---|---|---|---|
| `GET /api/health` | `HealthResponse` | `OverviewSection` | Backend connectivity, service status (`healthy`/`unhealthy`) |
| `GET /api/monitoring/status` | `MonitoringStatusResponse` | `OverviewSection` | Overall status (`operational`, `degraded`, `uninitialized`), database initialized boolean, recovery status |
| `GET /api/monitoring/transactions` | `TransactionStatisticsResponse` | `TransactionSection` | Total started, committed, aborted, active transactions, list of active Tx IDs |
| `GET /api/monitoring/wal` | `WALStatisticsResponse` | `WalSection` | Current LSN, flushed LSN, sync lag, total records, WAL file size (B/KB/MB), file status |
| `GET /api/monitoring/buffer-pool` | `BufferPoolStatisticsResponse` | `BufferPoolSection` | Frame capacity, resident pages, pinned frames, dirty pages, available frames, memory frame bar |
| `GET /api/monitoring/recovery` | `RecoveryStatisticsResponse` | `RecoverySection` | Recovery status (`recovered`, `failed`, `not_run`), winner/loser Tx IDs, records redone, CLRs generated, updates undone, modified pages, stage durations (µs/ms/s), error details |
| `GET /api/monitoring/workload` | `WorkloadSummaryResponse` | `WorkloadSection` | Workload status, planned vs executed ops, reads/updates mix, tx commit/abort/incomplete counts, correctness validation, crash injection status |
| `GET /api/monitoring/benchmark` | `BenchmarkSummaryResponse` | `BenchmarkSection` | Benchmark status, scenario name, success flag, measured/warmup runs, tx throughput (TPS), op throughput (ops/sec), mean workload/recovery durations |

---

## 3. Polling, Resilience, and Lifecycle Invariants

The `useMonitoringData` custom React hook (`frontend/src/hooks/useMonitoringData.ts`) enforces database monitoring guarantees:

1. **Independent Subsystem Isolation**: Each of the 8 endpoints is queried concurrently via `Promise.allSettled`. If one endpoint fails (e.g., WAL manager temporarily uninitialized or closed), its card displays an error/degraded state while the remaining components continue to refresh normally.
2. **Request Overlap Guard**: An `isFetchingRef` guard prevents slow network responses from stacking requests if a refresh cycle takes longer than the configured interval.
3. **In-Flight Cancellation**: An `AbortController` signal is passed to all fetch requests. Component unmount or trigger of a new poll cycle aborts existing pending requests immediately, preventing memory leaks and state updates on unmounted trees.
4. **Interval Customization**: The dashboard supports configurable polling intervals: `Paused`, `5 seconds`, `10 seconds` (default), and `30 seconds`, plus manual refresh controls.
5. **No Data Fabrication**: Uninitialized or unavailable fields (`null` or `undefined`) are rendered as `—` rather than zero, preventing misinterpretation of missing instrumentation as zero values.
6. **Read-Only Invariant**: All requests are strictly HTTP `GET` queries that never trigger recovery, workload execution, or benchmark runs.

---

## 4. Verification and Visual Checks

### 1. Frontend Build Verification
Executed in `frontend/`:
```bash
npm run build
```
- **Result**: `tsc` completed with 0 errors; Vite bundled production distribution into `dist/` (187.97 kB JS, 13.91 kB CSS) in 3.66s.

### 2. Backend Regression Suite Verification
Executed in project root:
```bash
.venv\Scripts\pytest -v
```
- **Result**: 276 passed in 19.49s. Zero regressions across storage, WAL, transactions, buffer pool, checkpoints, analysis, redo, undo, recovery manager, integration matrix, workload generator, benchmarking, and monitoring APIs.

---

# Phase 16A: Live Database Initialization and Monitoring — Walkthrough

## Overview

Phase 16A integrates the persistent database engine into the live FastAPI web application using ASGI `lifespan`. This enables the React monitoring dashboard to observe real, active database subsystems (Disk, Buffer Pool, WAL, Transactions, Recovery) upon server startup without breaking read-only API contracts or fabricating metrics.

---

## 1. Architecture and Lifespan Lifecycle

The database engine lifecycle is implemented in [`backend/app/main.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/app/main.py):

```
                       Uvicorn ASGI Startup
                                |
                                v
               +---------------------------------+
               |       Resolve Storage Paths     |
               |  (data/database.db, .wal)       |
               +---------------------------------+
                                |
                                v
               +---------------------------------+
               |      Initialize Subsystems      |
               | - DiskManager (binary I/O)      |
               | - LogManager (WAL log & LSNs)   |
               | - BufferManager (pool_size=10)  |
               | - TransactionManager            |
               | - CheckpointManager             |
               | - RecoveryManager               |
               +---------------------------------+
                                |
                                v
               +---------------------------------+
               |   Existing WAL Records Check    |
               +---------------------------------+
                     /                     \
       (Records > 0)                        (Records == 0)
             |                                    |
             v                                    v
  +----------------------+               +------------------+
  | ARIES Crash Recovery |               | Clean Baseline   |
  | (Analysis, Redo,     |               | (recovery_status |
  |  Undo with CLRs)     |               |  = "not_run")    |
  +----------------------+               +------------------+
             \                                    /
              +-----------------+----------------+
                                |
                                v
               +---------------------------------+
               |   Register With Monitoring      |
               | monitoring_state.register_db()  |
               +---------------------------------+
                                |
                                v
                    Live Requests Handled
                                |
                       ASGI Shutdown Signal
                                |
                                v
               +---------------------------------+
               |   Ordered Resource Teardown     |
               | 1. cm.close()                   |
               | 2. tm.close()                   |
               | 3. bm.close() (flushes pages    |
               |    and closes dm)               |
               | 4. dm.close() (safety check)    |
               | 5. lm.close() (flushes WAL)     |
               | 6. monitoring_state.reset()     |
               +---------------------------------+
```

---

## 2. Invariants and Correctness Guarantees

1. **Non-Destructive Persistence**: Existing database (`.db`) and WAL (`.wal`) binary files are never overwritten, reset, or truncated on startup.
2. **Deterministic ARIES Recovery on Restart**: If uncheckpointed or active records exist in the WAL from a previous session or crash, `RecoveryManager.recover(raise_on_error=False)` runs before accepting incoming traffic. Redo repeats history and Undo generates CLRs to complete rollbacks.
3. **No Metric Fabrication**: On a clean first start, `recovery_status` is accurately reported as `"not_run"`. If recovery fails, `status` becomes `"degraded"` and `recovery_status` becomes `"failed"` with the exact stage and error message preserved.
4. **Leak-Proof Resource Cleanup**: A `try...finally` block ensures that even if startup fails midway, any file handles already opened are closed in reverse order without descriptor leaks.
5. **Decoupled Monitoring Contract**: The monitoring endpoints remain strictly read-only HTTP `GET` queries.

---

## 3. Verification & Test Suite

The dedicated Phase 16A test suite resides in [`backend/tests/test_lifespan.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_lifespan.py):

| Test Name | Scenario Verified | Status |
|---|---|---|
| `test_01_lifespan_initializes_and_registers_database` | Clean startup initializes and registers all 5 subsystems; status is operational | PASSED |
| `test_02_lifespan_shutdown_closes_resources_and_resets` | Shutdown closes all managers in order and resets monitoring state | PASSED |
| `test_03_lifespan_recovers_existing_database_and_wal` | Startup recovers uncommitted loser transactions with CLRs and reports recovered | PASSED |
| `test_04_lifespan_preserves_committed_data_across_restarts` | Committed data and WAL records survive repeated startup/shutdown cycles | PASSED |
| `test_05_lifespan_handles_recovery_failure_gracefully` | Corrupted WAL causes clean startup failure without file descriptor leaks | PASSED |
- **Full Suite Verification**: 281 tests passing (276 existing + 5 Phase 16A lifespan tests).
- **Frontend Build**: `npm run build` completed with 0 errors (`dist/index.html`, `dist/assets/*`).

---

# Phase 16B: Safe Workload-to-Monitoring Integration — Walkthrough

## Overview

Phase 16B implements a safe, bounded mechanism to trigger synthetic transactional workloads directly against the **same active database instance** managed by FastAPI, and register the outcomes with `monitoring_state` so the live monitoring dashboard immediately displays real execution statistics.

---

## 1. Architecture and Resource Safety

```
  +-------------------------------------------------------------------------+
  |                             FastAPI Server                              |
  |                                                                         |
  |  POST /api/workload/run                                                 |
  |             |                                                           |
  |             v                                                           |
  |     +----------------+     Already running?                             |
  |     | Concurrency    |---------------------------> HTTP 409 Conflict    |
  |     | Guard (Lock)   |                                                  |
  |     +----------------+                                                  |
  |             | Not locked (Acquired)                                     |
  |             v                                                           |
  |     +----------------+     Subsystems closed?                           |
  |     | State Check    |---------------------------> HTTP 503 Unavailable |
  |     +----------------+                                                  |
  |             | Operational                                               |
  |             v                                                           |
  |     +----------------------------------------------------+              |
  |     | WorkloadExecutor.execute_live()                    |              |
  |     |                                                    |              |
  |     |  * Reuses app.state.transaction_manager            |              |
  |     |  * Reuses app.state.buffer_manager                 |              |
  |     |  * Reuses app.state.log_manager                    |              |
  |     |  * Reuses app.state.checkpoint_manager             |              |
  |     |  * Remaps tx IDs dynamically via tx_map            |              |
  |     |  * Wraps fetch_page in try/finally to prevent pins |              |
  |     |  * Flushes buffers via flush_all_pages()           |              |
  |     |  * Leaves managers OPEN for subsequent requests    |              |
  |     +----------------------------------------------------+              |
  |             |                                                           |
  |             +------------ Validation Failure?                           |
  |             |                      |                                    |
  |             |                      v                                    |
  |             |            HTTP 422 (Not Registered in State)             |
  |             v                                                           |
  |     +-----------------------------------------+                         |
  |     | monitoring_state.register_workload(...) |                         |
  |     +-----------------------------------------+                         |
  |             |                                                           |
  |             v                                                           |
  |     HTTP 200 OK (WorkloadRunResponse)                                   |
  |             |                                                           |
  |             v                                                           |
  |     Dashboard fetches /api/monitoring/workload                          |
  |     Displays "Synthetic Workload Summary"                               |
  +-------------------------------------------------------------------------+
```

### Key Technical Decisions:
1. **Zero Resource Duplication**: Avoided instantiating parallel `DiskManager` or `LogManager` instances which would trigger Windows file lock conflicts (`PermissionError` / `Sharing violation`).
2. **Handle Preservation**: `WorkloadExecutor.execute()` originally called `.close()` on all managers. `execute_live()` executes the workload, validates buffer frame pins, flushes modified pages to disk, and leaves all managers open and operational.
3. **Transaction ID Mapping (`tx_map`)**: Plans generated by `WorkloadGenerator` number transactions from `1..N`. Live engines generate monotonically increasing IDs (e.g. `1..10`, then `11..20`). `execute_live` dynamically maps plan IDs to actual live transaction IDs so abort rollbacks and CLRs reference the live engine transactions correctly.
4. **Validation Isolation**: Disk verification compares byte-exact committed state after flushing, while frame pin checking asserts `bm.get_pin_count(page_id) == 0` for all resident frames.
5. **Non-Blocking Concurrency Control**: An atomic `threading.Lock` immediately rejects simultaneous workloads with HTTP 409 Conflict, preventing interleaved corruption or resource exhaustion.

---

## 2. API Contract & Validation Limits

- **Endpoints**: `POST /api/workload/run` and `POST /api/workload/execute`
- **Request Limits**:
  - `num_transactions`: 1 to 100 (default: 10)
  - `num_pages`: 1 to 32 (default: 4)
  - `operations_per_transaction`: 1 to 20 (default: 5)
  - `num_transactions * operations_per_transaction` <= 1000
  - `commit_ratio + abort_ratio + incomplete_ratio` == 1.0 (within 0.001)
- **Response**: `WorkloadRunResponse` containing execution counts, transaction classification, validation status, and elapsed duration.

---

## 3. Test Suite Verification

The Phase 16B test suite in [`backend/tests/test_workload_api.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_workload_api.py) validates all 10 core scenarios:

| Test Name | Scenario Verified | Status |
|---|---|---|
| `test_01_successful_workload_execution_on_live_database` | Workload executes on active lifespan database, leaving managers open | PASSED |
| `test_02_workload_result_registered_and_retrievable_via_monitoring` | Result registers with monitoring_state, visible on /workload, /transactions, /wal, /buffer-pool | PASSED |
| `test_03_repeated_workloads_accumulate_safely` | Consecutive workloads execute without transaction ID collisions; stats accumulate | PASSED |
| `test_04_execute_alias_endpoint` | POST /api/workload/execute acts as a drop-in alias for /run | PASSED |
| `test_05_invalid_parameter_bounds_rejected` | Out-of-bounds transactions, pages, operations, ratio sums, and total ops return 422 | PASSED |
| `test_06_uninitialized_database_returns_503` | Calling endpoint outside lifespan or when database is closed returns 503 | PASSED |
| `test_07_concurrent_execution_returns_409_conflict` | Concurrent requests are rejected with HTTP 409 Conflict | PASSED |
| `test_08_validation_failure_handling` | Validation failures return 422 and prevent false success in monitoring state | PASSED |
| `test_09_internal_execution_failure_handling` | Execution errors return 500 without stack traces and preserve manager health | PASSED |
| `test_10_recovery_guarantee_after_workload_with_incomplete_transactions` | ARIES recovery cleanly recovers committed winners and uncommitted losers after workload | PASSED |

- **Full Suite Verification**: 291 passed, 3 warnings in 22.12s.
- **Frontend Production Build**: `npm run build` passed in 7.34s with 0 errors.

---

# Phase 16C: Benchmark-to-Monitoring Integration — Walkthrough

## Overview

Phase 16C integrates the existing ARIES performance benchmarking framework with the running FastAPI application, exposing a bounded, explicitly invoked benchmark endpoint (`POST /api/benchmark/run`) and registering aggregated performance metrics with `monitoring_state` so the live monitoring dashboard immediately displays real throughput and recovery timing distributions.

---

## 1. Architecture and Storage Safety

```
  +-------------------------------------------------------------------------------+
  |                                FastAPI Server                                 |
  |                                                                               |
  |  POST /api/benchmark/run                                                      |
  |             |                                                                 |
  |             v                                                                 |
  |     +----------------+     Workload or Benchmark active?                      |
  |     | Concurrency    |----------------------------------> HTTP 409 Conflict   |
  |     | Guard (Lock)   |                                                        |
  |     +----------------+                                                        |
  |             | Not locked (Acquired)                                           |
  |             v                                                                 |
  |     +----------------+     Live DB Subsystems closed?                         |
  |     | State Check    |----------------------------------> HTTP 503 Service   |
  |     +----------------+                                    Unavailable         |
  |             | Operational                                                     |
  |             v                                                                 |
  |     +----------------------------------------------------------+              |
  |     | BenchmarkRunner.run()                                    |              |
  |     |                                                          |              |
  |     |  * Validates scenario preset (from 10 registered)        |              |
  |     |  * Enforces repetitions <= 10, warmup <= 5, total <= 12  |              |
  |     |  * Executes each repetition in isolated temporary storage|              |
  |     |  * NEVER mutates, truncates, or touches live database    |              |
  |     |    (database.db & database.wal preserved completely)     |              |
  |     |  * Computes statistical aggregates (mean, p95, std_dev)  |              |
  |     +----------------------------------------------------------+              |
  |             |                                                                 |
  |             +------------ Any Run Failed Validation?                          |
  |             |                      |                                          |
  |             |                      v                                          |
  |             |            HTTP 422 (Not Registered in Monitoring State)        |
  |             v                                                                 |
  |     +------------------------------------------+                              |
  |     | monitoring_state.register_benchmark(...) |                              |
  |     +------------------------------------------+                              |
  |             |                                                                 |
  |             v                                                                 |
  |     HTTP 200 OK (BenchmarkRunResponse)                                        |
  |       - execution_mode: "isolated_temporary_storage"                          |
  |       - isolation_note: "..."                                                 |
  |             |                                                                 |
  |             v                                                                 |
  |     Dashboard fetches /api/monitoring/benchmark                               |
  |     Displays "Performance Benchmarks" with "Isolated Storage" badge           |
  +-------------------------------------------------------------------------------+
```

### Key Technical Decisions:
1. **Isolated Storage per Repetition**: Benchmarks necessarily require multiple runs (warmup + measured repetitions) to generate meaningful statistical distributions (mean, median, min, max, std dev, p95) from a clean baseline. Executing repeated runs on the live database would destroy baseline repeatability and corrupt active application data. Using isolated temporary storage per run preserves live files (`data/database.db` and `data/database.wal`) with byte-exact precision.
2. **Transparent Measurement Scope**: The API response explicitly declares `execution_mode: "isolated_temporary_storage"` with an accompanying `isolation_note`. The frontend displays an **Isolated Storage** badge in the benchmark banner to eliminate any ambiguity.
3. **Mutual Exclusion Concurrency Guard**: Benchmark execution shares `workload_lock` with the Phase 16B synthetic workload endpoint. Any attempt to run a benchmark while a workload is running (or vice versa, or concurrent benchmarks) is rejected with `HTTP 409 Conflict`.
4. **Failure Containment**: If any measured run fails correctness validation, `HTTP 422 Unprocessable Entity` is returned and the result is strictly blocked from registering with `monitoring_state`.
5. **No Stack Trace Exposure**: Execution exceptions are logged with full server-side tracebacks and returned to the client as sanitized `HTTP 500` error details.

---

## 2. API Contract & Validation Limits

- **Endpoints**: `POST /api/benchmark/run` and `POST /api/benchmark/execute`
- **Discovery**: `GET /api/benchmark/scenarios`
- **Supported Scenarios**:
  - `small_workload` (default): 5 tx, 2 pages, 3 ops/tx.
  - `medium_workload`: 20 tx, 8 pages, 5 ops/tx.
  - `large_workload`: 50 tx, 16 pages, 8 ops/tx.
  - `read_heavy`: 85% reads, 15% updates.
  - `update_heavy`: 10% reads, 90% updates.
  - `hotspot_contention`: 80% Page 0 contention.
  - `interleaved_concurrent`: 100% interleaved scheduling.
  - `high_abort`: 70% aborts.
  - `recovery_only`: Prepares uncheckpointed crash state and measures ARIES recovery stages.
  - `crash_recovery_commit_fault`: Subprocess crash experiment with restart recovery.
- **Request Limits**:
  - `scenario`: Must be one of the registered presets.
  - `repetitions`: `1` to `10` (default: `3`).
  - `warmup_runs`: `0` to `5` (default: `1`).
  - `repetitions + warmup_runs` <= `12`.
  - `timeout_per_run`: `1.0` to `60.0` seconds (default: `30.0`).
- **Response**: `BenchmarkRunResponse` containing execution counts, throughput, duration distributions, and isolation metadata.

---

## 3. Test Suite Verification

The Phase 16C test suite in [`backend/tests/test_benchmark_api.py`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/backend/tests/test_benchmark_api.py) validates all 12 core scenarios:

| Test Name | Scenario Verified | Status |
|---|---|---|
| `test_01_successful_benchmark_execution` | Bounded benchmark executes, computes aggregates, leaves managers open | PASSED |
| `test_02_benchmark_result_registered_and_retrievable_via_monitoring` | Outcome registered with monitoring_state, retrievable on /benchmark | PASSED |
| `test_03_benchmark_scenarios_discovery_endpoint` | GET /api/benchmark/scenarios lists all presets and operational bounds | PASSED |
| `test_04_benchmark_execute_alias_endpoint` | POST /api/benchmark/execute acts as drop-in alias for /run | PASSED |
| `test_05_invalid_parameters_bounds_rejected` | Out-of-bounds scenarios, repetitions, warmup runs, and timeouts return 422 | PASSED |
| `test_06_uninitialized_database_returns_503` | Calling endpoint when database is uninitialized or closed returns 503 | PASSED |
| `test_07_concurrent_benchmark_execution_rejected_with_409` | Concurrent benchmark execution requests rejected with 409 Conflict | PASSED |
| `test_08_mutual_exclusion_with_workload_execution` | Workload and benchmark endpoints are mutually exclusive across runs | PASSED |
| `test_09_storage_safety_preserves_live_database_files` | Benchmarks in isolated storage never overwrite, truncate, or pollute live DB/WAL | PASSED |
| `test_10_benchmark_validation_failure_handling` | Validation failures return 422 and prevent false success in monitoring state | PASSED |
| `test_11_internal_execution_failure_handling` | Execution errors return 500 without stack traces and preserve manager health | PASSED |
| `test_12_recovery_scenario_benchmark_metrics` | recovery_only scenario executes and captures mean_recovery_duration_s | PASSED |

- **Full Suite Verification**: 303 passed, 4 warnings in 11.12s.
- **Frontend Production Build**: `npm run build` passed in 5.58s with 0 errors.

---

# Phase 16D: End-to-End Integration Verification — Walkthrough

## Overview

Phase 16D performs comprehensive end-to-end integration verification across all system components developed across Phases 0–16C:
- **FastAPI Core & Lifespan Architecture**: Automatic database engine initialization, non-destructive startup, and graceful resource cleanup.
- **Live Database Engine**: Disk Manager, Buffer Pool Manager (LRU + pin tracking), Log Manager (WAL), Transaction Manager, Checkpoint Manager, and Recovery Manager.
- **Monitoring APIs**: `/api/health`, `/api/monitoring/status`, `/api/monitoring/transactions`, `/api/monitoring/wal`, `/api/monitoring/buffer-pool`, `/api/monitoring/workload`, `/api/monitoring/benchmark`, `/api/monitoring/recovery`, and `/api/benchmark/scenarios`.
- **Synthetic Workload Execution**: `POST /api/workload/run` operating safely on the live active database instance and updating real-time transaction, WAL, and buffer pool metrics.
- **Benchmark Execution**: `POST /api/benchmark/run` executing reproducible statistical suites in isolated storage and updating throughput and recovery distributions.
- **React Monitoring Dashboard**: Real-time polling via `useMonitoringData.ts`, loading, available, empty, and error states across all subsystem cards.

---

## 1. End-to-End System Architecture

```
                                  +-------------------------------------------------------------+
                                  |                 React Frontend (Vite + TS)                  |
                                  |                   http://localhost:5173/                    |
                                  +-------------------------------------------------------------+
                                     |                     |                         |
                           useMonitoringData()     Workload Trigger          Benchmark Trigger
                           (Polls every 3000ms)    (POST /api/workload/run)  (POST /api/benchmark/run)
                                     |                     |                         |
                                     v                     v                         v
  +---------------------------------------------------------------------------------------------+
  |                                        FastAPI Backend                                      |
  |                                     http://127.0.0.1:8000/                                  |
  |                                                                                             |
  |  +--------------------+   +-----------------------+   +----------------------------------+  |
  |  |   /api/health      |   | /api/monitoring/*     |   | Workload & Benchmark Concurrency |  |
  |  |   GET status: ok   |   | Status, Tx, WAL, BP,  |   | workload_lock (Atomic Mutex)     |  |
  |  |                    |   | Workload, Benchmark   |   | -> Rejects overlap with 409      |  |
  |  +--------------------+   +-----------------------+   +----------------------------------+  |
  |                                                                |                            |
  |                                +-------------------------------+                            |
  |                                |                               |                            |
  |                                v                               v                            |
  |                 [Live Database Execution]        [Isolated Benchmark Execution]             |
  |                 WorkloadExecutor.execute_live()  BenchmarkRunner.run()                      |
  |                 Uses active DB instance          Uses temp isolated storage                 |
  |                 Leaves managers OPEN             Leaves live files UNTOUCHED                |
  |                                |                               |                            |
  |                                v                               v                            |
  |                 +-------------------------------------------------------+                   |
  |                 |                   monitoring_state                    |                   |
  |                 | (In-memory thread-safe singleton registered by engine)|                   |
  |                 +-------------------------------------------------------+                   |
  |                                        |                                                    |
  |                         +--------------+--------------+                                     |
  |                         |                             |                                     |
  |                         v                             v                                     |
  |             +-----------------------+     +-----------------------+                         |
  |             |  data/database.db     |     |  data/database.wal    |                         |
  |             |  Persistent Data      |     |  Write-Ahead Log      |                         |
  |             +-----------------------+     +-----------------------+                         |
  +---------------------------------------------------------------------------------------------+
```

---

## 2. Verification Protocol & Results

### 2.1 Backend Test Suites
All 312 backend tests passed with 0 failures:
- **Baseline Suite**: 303 tests covering Storage, Buffer Pool, WAL, Transactions, Checkpointing, Analysis, Redo, Undo (CLRs), Recovery Orchestration, Fault Injection, Integration Matrix, Synthetic Workload Engine, Benchmark Framework, Lifespan, Workload API, and Benchmark API.
- **Dedicated E2E Integration Suite** (`backend/tests/test_e2e_integration.py`): 9 comprehensive integration scenarios:
  1. `test_01_health_and_initial_status`: Verifies `/api/health` and `/api/monitoring/status`.
  2. `test_02_benchmark_scenario_discovery`: Verifies `/api/benchmark/scenarios` preset listing and bounds.
  3. `test_03_workload_execution_and_monitoring_update`: Verifies live workload execution and state reflection on `/workload`, `/transactions`, and `/wal`.
  4. `test_04_workload_validation_and_error_handling`: Verifies parameter rejection (422) for invalid bounds and ratios.
  5. `test_05_benchmark_execution_and_data_preservation`: Verifies benchmark execution and byte-exact SHA-256 hash preservation of live database files.
  6. `test_06_benchmark_validation_and_scenario_not_found`: Verifies rejection of unknown scenarios and invalid run counts (422).
  7. `test_07_concurrent_workload_and_benchmark_locking`: Verifies `workload_lock` mutual exclusion (409 Conflict).
  8. `test_08_uninitialized_database_behavior`: Verifies clean 503 response and empty monitoring state when uninitialized.
  9. `test_09_full_end_to_end_lifecycle`: Verifies sequential multi-run lifecycle: clean start -> workload -> verification -> benchmark -> re-verification.

### 2.2 Frontend Production Build
- Executed `npm run build` in `frontend/`:
  - 1494 modules transformed.
  - Output files generated in `frontend/dist/`:
    - `dist/index.html` (0.47 kB)
    - `dist/assets/index-*.css` (20.30 kB)
    - `dist/assets/index-*.js` (477.73 kB)
  - 0 errors, 0 warnings.

### 2.3 Live Running Service Verification
Live HTTP requests were executed against the active FastAPI server at `http://127.0.0.1:8000`:

| Endpoint | Method | Outcome | Key Observations |
|---|---|---|---|
| `/api/health` | GET | 200 OK | `status: "ok"`, `version: "0.1.0"` |
| `/api/monitoring/status` | GET | 200 OK | `status: "operational"`, `database_initialized: true` |
| `/api/benchmark/scenarios` | GET | 200 OK | Returns all 10 registered presets with transaction, page, and operation parameters |
| `/api/workload/run` | POST | 200 OK | 10 tx, 4 pages, 70 ops; 8 committed, 2 aborted, 0 incomplete, `valid: true` |
| `/api/monitoring/workload` | GET | 200 OK | Transitioned to `status: "available"`, displaying 70 operations and 10 transactions |
| `/api/monitoring/transactions`| GET | 200 OK | Reflects live transaction counters: `total_started: 10`, `total_committed: 8`, `total_aborted: 2` |
| `/api/monitoring/wal` | GET | 200 OK | Reflects live WAL log records: `total_records: 124`, `flushed_lsn: 124` |
| `/api/benchmark/run` | POST | 200 OK | `small_workload` (3 measured + 1 warmup); 100% valid; 600.78 TPS; 1802.34 OPS |
| `/api/monitoring/benchmark` | GET | 200 OK | Transitioned to `status: "available"`; displays 3/3 valid runs, 600.78 TPS, and isolated storage flag |
| `/api/workload/run` (invalid) | POST | 422 Unprocessable | `num_transactions: 0` rejected cleanly |
| `/api/benchmark/run` (invalid)| POST | 422 Unprocessable | Unknown scenario `"unknown_scenario_xyz"` rejected cleanly |
| `/api/benchmark/run` (invalid)| POST | 422 Unprocessable | `repetitions: 20` (exceeds limit 10) rejected cleanly |

### 2.4 Persistent Live Storage Preservation
- **Live File Integrity Check**:
  - Live files `data/database.db` (16,384 bytes) and `data/database.wal` (8,338 bytes) were inspected before and after running the statistical benchmark suite.
  - The live files retained identical byte lengths.
  - In automated test suite `test_05_benchmark_execution_and_data_preservation`, SHA-256 hashes of `data/database.db` and `data/database.wal` were computed before and after benchmark execution and confirmed byte-for-byte identical.

---

## 3. Dashboard Behavior & State Verification

The frontend React dashboard ([`frontend/src/App.tsx`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/App.tsx)) and custom hook ([`frontend/src/hooks/useMonitoringData.ts`](file:///c:/Users/siddh/OneDrive/Desktop/ARIES-crash-recovery/frontend/src/hooks/useMonitoringData.ts)) handle the following UI states:

1. **Initial / Uninitialized State**:
   - If the backend is running without an initialized database, subsystem cards display `"Database Uninitialized"` with empty state guidance.
2. **Operational State**:
   - When the backend initializes during lifespan, cards display active buffer pool frames, transaction counters, and WAL log sequence numbers.
3. **Empty States**:
   - Before any workload is executed, the **Synthetic Workload Summary** card displays an empty state prompt (`"No Workload Executed Yet"`).
   - Before any benchmark is executed, the **Performance Benchmarks** card displays an empty state prompt (`"No Benchmark Executed Yet"`).
4. **Available / Populated States**:
   - Upon executing `POST /api/workload/run`, the Workload card immediately populates with total transactions, total operations, committed/aborted/active counts, and a green `"Passed"` validation badge.
   - Upon executing `POST /api/benchmark/run`, the Benchmark card immediately populates with scenario name, measured runs, TPS, OPS, latency distributions, and an **Isolated Storage** badge.
5. **Degraded / Error State Handling**:
   - If an individual monitoring endpoint fails (e.g. network disconnect or backend restart), `Promise.allSettled()` prevents cascading dashboard crashes, maintaining last known good data or localized card error indicators.

### Manual Browser Verification Procedure:
1. Open Google Chrome, Edge, or Firefox and navigate to `http://localhost:5173/`.
2. Verify that the **System Status** banner displays **Operational** with **Database Initialized**.
3. Verify that the **Transaction Manager** card displays `10 Started`, `8 Committed`, `2 Aborted`.
4. Verify that the **Write-Ahead Logging** card displays `124 Records` and `Flushed LSN 124`.
5. Verify that the **Synthetic Workload Summary** card shows `10 Transactions`, `70 Operations`, and `Valid: Passed`.
6. Verify that the **Performance Benchmarks** card shows `Scenario: small_workload`, `Measured: 3`, `TPS: ~600`, and the **Isolated Storage** badge.

---

## 4. Summary of Bugs Fixed & Invariants Upheld

- **Zero Breaking Integration Bugs**: The integration contracts established in Phases 16A, 16B, and 16C functioned seamlessly without code alterations.
- **Windows File Handle Discipline**: The system strictly prevents concurrent file access conflicts on Windows by routing live workloads through the shared lifespan engine instances and isolating benchmark repetitions in per-run temporary directories.
- **Strict Concurrency Mutual Exclusion**: `workload_lock` successfully guards against simultaneous workload/benchmark execution.
- **ARIES Algorithm Unchanged**: No changes were made to Analysis, Redo, Undo (with CLRs), or Checkpoint internals.

---

## 5. Remaining Limitations & Recommended Next Phase

### Remaining Limitations:
1. **Single-Worker Concurrency**: Workloads and benchmarks currently acquire a global mutex (`workload_lock`), restricting execution to one active workload or benchmark at any given moment.
2. **Ephemeral In-Memory Workload/Benchmark History**: `monitoring_state` stores the *latest* workload and benchmark result in memory. Restarting the backend server resets these to `status: "empty"`, though persistent database tables and WAL survive cleanly.

### Recommended Next Phase — Phase 17: Interactive Control Plane & Scenario Runner
- Add interactive frontend controls to trigger custom workloads and benchmark scenarios directly from the dashboard UI.
- Provide a persistent benchmark experiment run history log with comparative chart visualizations.
- Add live crash fault injection controls to demonstrate real-time ARIES restart recovery in the browser.
