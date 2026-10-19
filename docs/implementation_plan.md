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
