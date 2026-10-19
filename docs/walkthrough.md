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
