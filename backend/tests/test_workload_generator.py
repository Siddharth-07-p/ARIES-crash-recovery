"""Comprehensive test suite for Phase 12: Synthetic Transaction Workload Generator.

Covers all 15 required verification scenarios:
1. Configuration validation and invalid inputs.
2. Deterministic operation-plan generation using a fixed seed.
3. Different seeds producing appropriately different plans.
4. A valid read/update workload.
5. Committed transaction durability.
6. Aborted transaction rollback.
7. Incomplete transactions and subsequent recovery.
8. Interleaved transactions.
9. Workloads operating on multiple pages.
10. Zero-operation and minimum-size workloads.
11. Reproducibility across independent test runs.
12. Crash injection and restart recovery using persistent files.
13. Correct result counts and serialization.
14. Invalid workload configurations failing safely.
15. Compatibility with existing tests and public APIs.
"""

from pathlib import Path
import pytest

from app.fault.constants import DEFAULT_CRASH_EXIT_CODE, FaultPoint
from app.recovery.recovery_manager import RecoveryManager
from app.recovery.recovery_state import RecoveryResult
from app.storage.constants import PAGE_SIZE
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecordType
from app.workload.config import WorkloadConfig
from app.workload.exceptions import (
    WorkloadConfigError,
    WorkloadError,
    WorkloadExecutionError,
    WorkloadValidationError,
)
from app.workload.executor import WorkloadExecutor
from app.workload.generator import WorkloadGenerator
from app.workload.models import (
    OperationType,
    TransactionOutcome,
    WorkloadOperation,
    WorkloadPlan,
    WorkloadResult,
)


@pytest.fixture
def temp_wal_path(tmp_path: Path) -> Path:
    """Isolated temporary WAL filepath."""
    return tmp_path / "workload_test.wal"


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Isolated temporary database filepath."""
    return tmp_path / "workload_test.db"


class TestWorkloadGeneratorSuite:
    """Comprehensive test suite for WorkloadConfig, WorkloadGenerator, and WorkloadExecutor."""

    # -------------------------------------------------------------------------
    # 1. Configuration validation and invalid inputs
    # -------------------------------------------------------------------------
    def test_01_config_validation_and_invalid_inputs(self) -> None:
        """Verify that invalid configuration values are strictly rejected."""
        # Negative num_transactions
        with pytest.raises(WorkloadConfigError, match="num_transactions"):
            WorkloadConfig(num_transactions=-1)

        with pytest.raises(WorkloadConfigError, match="num_transactions"):
            WorkloadConfig(num_transactions=True)  # type: ignore

        # Invalid num_pages (< 1)
        with pytest.raises(WorkloadConfigError, match="num_pages"):
            WorkloadConfig(num_pages=0)

        # Negative operations_per_transaction
        with pytest.raises(WorkloadConfigError, match="operations_per_transaction"):
            WorkloadConfig(operations_per_transaction=-2)

        # Invalid ratio bounds (< 0.0 or > 1.0)
        with pytest.raises(WorkloadConfigError, match="read_ratio"):
            WorkloadConfig(read_ratio=1.5)

        with pytest.raises(WorkloadConfigError, match="read_ratio"):
            WorkloadConfig(read_ratio=-0.1)

        # Ratios not summing to 1.0
        with pytest.raises(WorkloadConfigError, match="must sum to 1.0"):
            WorkloadConfig(commit_ratio=0.6, abort_ratio=0.6, incomplete_ratio=0.0)

        with pytest.raises(WorkloadConfigError, match="must sum to 1.0"):
            WorkloadConfig(commit_ratio=0.2, abort_ratio=0.2, incomplete_ratio=0.2)

        # Invalid pool_size (< 2)
        with pytest.raises(WorkloadConfigError, match="pool_size"):
            WorkloadConfig(pool_size=1)

        # Invalid crash_fault_point
        with pytest.raises(WorkloadConfigError, match="Invalid crash_fault_point"):
            WorkloadConfig(crash_fault_point="NON_EXISTENT_FAULT_POINT")

        # Invalid checkpoint_interval (< 1)
        with pytest.raises(WorkloadConfigError, match="checkpoint_interval"):
            WorkloadConfig(checkpoint_interval=0)

        # Invalid seed type
        with pytest.raises(WorkloadConfigError, match="seed"):
            WorkloadConfig(seed="not_an_int")  # type: ignore

    # -------------------------------------------------------------------------
    # 2. Deterministic operation-plan generation using a fixed seed
    # -------------------------------------------------------------------------
    def test_02_deterministic_plan_generation_fixed_seed(self) -> None:
        """Verify that a fixed config and seed produce the exact same plan."""
        config = WorkloadConfig(
            num_transactions=5,
            num_pages=3,
            operations_per_transaction=4,
            read_ratio=0.4,
            commit_ratio=0.6,
            abort_ratio=0.2,
            incomplete_ratio=0.2,
            seed=4242,
        )

        plan1 = WorkloadGenerator.generate(config)
        plan2 = WorkloadGenerator.generate(config)

        assert len(plan1) == len(plan2)
        assert plan1 == plan2
        assert plan1.to_dict() == plan2.to_dict()

        # Check that individual operations match precisely
        for op1, op2 in zip(plan1.operations, plan2.operations):
            assert op1 == op2
            assert op1.step_index == op2.step_index
            assert op1.transaction_id == op2.transaction_id
            assert op1.operation_type == op2.operation_type
            assert op1.page_id == op2.page_id
            assert op1.offset == op2.offset
            assert op1.data == op2.data

    # -------------------------------------------------------------------------
    # 3. Different seeds producing appropriately different plans
    # -------------------------------------------------------------------------
    def test_03_different_seeds_produce_different_plans(self) -> None:
        """Verify that varying the seed changes generated operations or distributions."""
        config1 = WorkloadConfig(
            num_transactions=5,
            num_pages=4,
            operations_per_transaction=5,
            seed=101,
        )
        config2 = WorkloadConfig(
            num_transactions=5,
            num_pages=4,
            operations_per_transaction=5,
            seed=202,
        )

        plan1 = WorkloadGenerator.generate(config1)
        plan2 = WorkloadGenerator.generate(config2)

        # The plans must not be equal
        assert plan1 != plan2
        # Different operation payloads or page assignments
        ops1_data = [op.data for op in plan1.operations if op.data is not None]
        ops2_data = [op.data for op in plan2.operations if op.data is not None]
        assert ops1_data != ops2_data

    # -------------------------------------------------------------------------
    # 4. Valid read/update workload execution
    # -------------------------------------------------------------------------
    def test_04_valid_read_update_workload(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify successful execution of a mixed read/update workload."""
        config = WorkloadConfig(
            num_transactions=4,
            num_pages=3,
            operations_per_transaction=4,
            read_ratio=0.5,
            commit_ratio=0.75,
            abort_ratio=0.25,
            incomplete_ratio=0.0,
            seed=123,
        )

        result = WorkloadExecutor.execute(
            config=config,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )

        assert result.is_valid is True
        assert len(result.validation_errors) == 0
        assert result.transactions_started == 4
        assert result.transactions_committed + result.transactions_aborted == 4
        assert result.reads_executed > 0
        assert result.updates_executed > 0
        assert result.executed_operations == result.planned_operations

    # -------------------------------------------------------------------------
    # 5. Committed transaction durability
    # -------------------------------------------------------------------------
    def test_05_committed_transaction_durability(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify that 100% committed transaction updates are durable and survive restart."""
        config = WorkloadConfig(
            num_transactions=4,
            num_pages=2,
            operations_per_transaction=3,
            read_ratio=0.0,
            commit_ratio=1.0,
            abort_ratio=0.0,
            incomplete_ratio=0.0,
            seed=777,
        )

        result = WorkloadExecutor.execute(
            config=config,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )

        assert result.is_valid is True
        assert result.transactions_committed == 4
        assert result.transactions_aborted == 0
        assert result.transactions_incomplete == 0

        # Restart recovery should find 4 winners and 0 losers
        rec_res = RecoveryManager.recover_from_files(
            db_path=temp_db_path,
            wal_path=temp_wal_path,
            pool_size=config.pool_size,
        )
        assert rec_res.is_success is True
        assert set(rec_res.winners) == {1, 2, 3, 4}
        assert rec_res.losers == []
        assert rec_res.updates_undone == 0

    # -------------------------------------------------------------------------
    # 6. Aborted transaction rollback
    # -------------------------------------------------------------------------
    def test_06_aborted_transaction_rollback(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify that aborted transactions are rolled back via CLRs to baseline data."""
        config = WorkloadConfig(
            num_transactions=3,
            num_pages=2,
            operations_per_transaction=3,
            read_ratio=0.0,
            commit_ratio=0.0,
            abort_ratio=1.0,
            incomplete_ratio=0.0,
            seed=888,
        )

        result = WorkloadExecutor.execute(
            config=config,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )

        assert result.is_valid is True
        assert result.transactions_aborted == 3
        assert result.transactions_committed == 0

        # Disk pages should have been restored to baseline pattern
        with DiskManager(db_path=temp_db_path) as dm:
            for pid in range(2):
                page = dm.read_page(pid)
                data = page.read(0, 16)
                assert data == f"PAGE_{pid:04d}_INIT_B".encode("ascii")

    # -------------------------------------------------------------------------
    # 7. Incomplete transactions and subsequent recovery
    # -------------------------------------------------------------------------
    def test_07_incomplete_transactions_and_subsequent_recovery(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify that transactions left incomplete are rolled back by RecoveryManager."""
        config = WorkloadConfig(
            num_transactions=3,
            num_pages=2,
            operations_per_transaction=3,
            read_ratio=0.0,
            commit_ratio=0.0,
            abort_ratio=0.0,
            incomplete_ratio=1.0,
            auto_recover=True,
            seed=999,
        )

        result = WorkloadExecutor.execute(
            config=config,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )

        assert result.is_valid is True
        assert result.transactions_incomplete == 3
        assert result.recovery_result is not None
        assert result.recovery_result.is_success is True
        assert set(result.recovery_result.losers) == {1, 2, 3}
        assert result.recovery_result.updates_undone > 0

    # -------------------------------------------------------------------------
    # 8. Interleaved transactions
    # -------------------------------------------------------------------------
    def test_08_interleaved_transactions(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify interleaved transaction scheduling, execution, and validity."""
        config = WorkloadConfig(
            num_transactions=4,
            num_pages=3,
            operations_per_transaction=3,
            read_ratio=0.2,
            commit_ratio=0.5,
            abort_ratio=0.5,
            incomplete_ratio=0.0,
            interleave_probability=1.0,
            seed=555,
        )

        plan = WorkloadGenerator.generate(config)
        # In an interleaved plan with interleave_probability=1.0, BEGIN ops precede body ops
        first_ops = [op.operation_type for op in plan.operations[:4]]
        assert first_ops == [OperationType.BEGIN] * 4

        result = WorkloadExecutor.execute(
            config=config,
            plan=plan,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )

        assert result.is_valid is True
        assert result.transactions_started == 4
        assert len(result.validation_errors) == 0

    # -------------------------------------------------------------------------
    # 9. Workloads operating on multiple pages
    # -------------------------------------------------------------------------
    def test_09_workloads_operating_on_multiple_pages(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify workloads spanning multiple pages without non-interference bugs."""
        config = WorkloadConfig(
            num_transactions=6,
            num_pages=8,
            operations_per_transaction=4,
            read_ratio=0.2,
            commit_ratio=0.6,
            abort_ratio=0.4,
            incomplete_ratio=0.0,
            same_page_probability=0.0,  # Uniform spread across all 8 pages
            seed=444,
        )

        result = WorkloadExecutor.execute(
            config=config,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )

        assert result.is_valid is True
        with DiskManager(db_path=temp_db_path) as dm:
            assert dm.get_page_count() == 8

    # -------------------------------------------------------------------------
    # 10. Zero-operation and minimum-size workloads
    # -------------------------------------------------------------------------
    def test_10_zero_operation_and_minimum_size_workloads(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify boundary workload sizes: 0 transactions, and 0 operations per transaction."""
        # Zero transactions
        cfg_zero = WorkloadConfig(
            num_transactions=0,
            num_pages=2,
            operations_per_transaction=0,
        )
        plan_zero = WorkloadGenerator.generate(cfg_zero)
        assert len(plan_zero) == 0

        res_zero = WorkloadExecutor.execute(
            config=cfg_zero,
            plan=plan_zero,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )
        assert res_zero.is_valid is True
        assert res_zero.transactions_started == 0
        assert res_zero.executed_operations == 0

        # Minimum size: 1 transaction with 0 body operations (only BEGIN and COMMIT)
        db_min = temp_db_path.with_name("min.db")
        wal_min = temp_wal_path.with_name("min.wal")
        cfg_min = WorkloadConfig(
            num_transactions=1,
            num_pages=1,
            operations_per_transaction=0,
            commit_ratio=1.0,
            abort_ratio=0.0,
            incomplete_ratio=0.0,
        )
        res_min = WorkloadExecutor.execute(
            config=cfg_min,
            db_path=db_min,
            wal_path=wal_min,
        )
        assert res_min.is_valid is True
        assert res_min.transactions_started == 1
        assert res_min.transactions_committed == 1
        assert res_min.executed_operations == 2  # BEGIN + COMMIT

    # -------------------------------------------------------------------------
    # 11. Reproducibility across independent test runs
    # -------------------------------------------------------------------------
    def test_11_reproducibility_across_independent_runs(
        self, tmp_path: Path
    ) -> None:
        """Verify that identical configurations and seeds produce identical database states."""
        config = WorkloadConfig(
            num_transactions=3,
            num_pages=2,
            operations_per_transaction=3,
            read_ratio=0.0,
            commit_ratio=1.0,
            abort_ratio=0.0,
            incomplete_ratio=0.0,
            seed=333,
        )

        db1 = tmp_path / "run1.db"
        wal1 = tmp_path / "run1.wal"
        res1 = WorkloadExecutor.execute(config=config, db_path=db1, wal_path=wal1)

        db2 = tmp_path / "run2.db"
        wal2 = tmp_path / "run2.wal"
        res2 = WorkloadExecutor.execute(config=config, db_path=db2, wal_path=wal2)

        assert res1.executed_operations == res2.executed_operations
        assert res1.updates_executed == res2.updates_executed

        # Compare persistent disk content byte-for-byte across both runs
        with open(db1, "rb") as f1, open(db2, "rb") as f2:
            assert f1.read() == f2.read()

    # -------------------------------------------------------------------------
    # 12. Crash injection and restart recovery using persistent files
    # -------------------------------------------------------------------------
    def test_12_crash_injection_and_restart_recovery(
        self, tmp_path: Path
    ) -> None:
        """Verify real subprocess termination at fault point and restart recovery from files."""
        db_crash = tmp_path / "crash_exp.db"
        wal_crash = tmp_path / "crash_exp.wal"

        config = WorkloadConfig(
            num_transactions=3,
            num_pages=2,
            operations_per_transaction=2,
            read_ratio=0.0,
            commit_ratio=1.0,
            abort_ratio=0.0,
            incomplete_ratio=0.0,
            crash_fault_point=FaultPoint.DURING_TRANSACTION_COMMIT,
            crash_exit_code=DEFAULT_CRASH_EXIT_CODE,
            auto_recover=True,
            seed=1212,
        )

        result = WorkloadExecutor.run_crash_experiment(
            config=config,
            db_path=db_crash,
            wal_path=wal_crash,
            temp_dir=tmp_path,
        )

        assert result.crash_configured is True
        assert result.crash_observed is True
        assert result.exit_code == DEFAULT_CRASH_EXIT_CODE
        assert result.recovery_result is not None
        assert result.recovery_result.is_success is True

    # -------------------------------------------------------------------------
    # 13. Correct result counts and serialization
    # -------------------------------------------------------------------------
    def test_13_result_counts_and_serialization(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify WorkloadResult properties, to_dict serialization, and immutability."""
        config = WorkloadConfig(
            num_transactions=2,
            num_pages=2,
            operations_per_transaction=2,
            commit_ratio=0.5,
            abort_ratio=0.5,
            incomplete_ratio=0.0,
            seed=909,
        )

        result = WorkloadExecutor.execute(config=config, db_path=temp_db_path, wal_path=temp_wal_path)

        # Serialization to dictionary
        d = result.to_dict()
        assert isinstance(d, dict)
        assert d["planned_operations"] == result.planned_operations
        assert d["executed_operations"] == result.executed_operations
        assert d["transactions_started"] == 2
        assert "config" in d
        assert "WorkloadResult" in repr(result)

        # Immutability check: mutating returned list copies does not modify internal state
        committed = result.committed_transactions
        committed.append(99999)
        assert 99999 not in result.committed_transactions

    # -------------------------------------------------------------------------
    # 14. Invalid workload configurations failing safely
    # -------------------------------------------------------------------------
    def test_14_invalid_execution_parameters_fail_safely(self) -> None:
        """Verify that missing files or invalid parameters raise clear execution errors."""
        config = WorkloadConfig(num_transactions=2)

        # None paths rejected
        with pytest.raises(WorkloadExecutionError, match="must both be provided"):
            WorkloadExecutor.execute(config=config, db_path=None, wal_path=None)

        # run_crash_experiment without crash_fault_point
        with pytest.raises(WorkloadExecutionError, match="crash_fault_point"):
            WorkloadExecutor.run_crash_experiment(
                config=config,
                db_path=Path("dummy.db"),
                wal_path=Path("dummy.wal"),
                temp_dir=Path("."),
            )

    # -------------------------------------------------------------------------
    # 15. Compatibility with existing tests and public APIs
    # -------------------------------------------------------------------------
    def test_15_compatibility_with_existing_apis(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify seamless interoperability with RecoveryManager and CheckpointManager."""
        config = WorkloadConfig(
            num_transactions=4,
            num_pages=2,
            operations_per_transaction=3,
            commit_ratio=0.5,
            abort_ratio=0.5,
            incomplete_ratio=0.0,
            checkpoint_interval=4,  # Triggers checkpointing during execution
            seed=616,
        )

        result = WorkloadExecutor.execute(
            config=config,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )

        assert result.is_valid is True
        assert result.executed_operations > 0

        # Post-execution RecoveryManager recovery
        rec_res = RecoveryManager.recover_from_files(
            db_path=temp_db_path,
            wal_path=temp_wal_path,
            pool_size=config.pool_size,
        )
        assert rec_res.is_success is True
        assert rec_res.losers == []

    # -------------------------------------------------------------------------
    # 16. Validator detects disk mismatch and recovery failures
    # -------------------------------------------------------------------------
    def test_16_validator_detects_disk_mismatch_and_recovery_failures(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify that the expected-state validator catches disk mismatches, recovery errors, and winner/loser discrepancies."""
        config = WorkloadConfig(
            num_transactions=2,
            num_pages=2,
            operations_per_transaction=2,
            commit_ratio=1.0,
            abort_ratio=0.0,
            incomplete_ratio=0.0,
            seed=1010,
        )

        result = WorkloadExecutor.execute(
            config=config,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )
        assert result.is_valid is True

        # 1. Deliberately tamper with Page 0 bytes on disk
        with open(temp_db_path, "r+b") as f:
            f.seek(0)
            f.write(b"CORRUPTED_DISK_BYTES_HERE!")

        # Re-run validation against original expected pages
        fake_expected = {
            0: bytearray(b"A" * PAGE_SIZE),
            1: bytearray(b"B" * PAGE_SIZE),
        }
        errors = WorkloadExecutor.validate_database_state(
            db_path=temp_db_path,
            expected_committed_pages=fake_expected,
        )
        assert len(errors) > 0
        assert any("persistent disk state mismatch" in err for err in errors)

        # 2. Check unallocated page detection
        unallocated_expected = {
            99: bytearray(b"X" * PAGE_SIZE),
        }
        err_unallocated = WorkloadExecutor.validate_database_state(
            db_path=temp_db_path,
            expected_committed_pages=unallocated_expected,
        )
        assert any("expected but not allocated on disk" in err for err in err_unallocated)

        # 3. Check recovery error detection
        failed_rec = RecoveryResult(is_success=False, error_message="Injected recovery failure")
        err_rec_fail = WorkloadExecutor.validate_database_state(
            db_path=temp_db_path,
            expected_committed_pages={},
            recovery_result=failed_rec,
        )
        assert any("Recovery failed with error: Injected recovery failure" in err for err in err_rec_fail)

        # 4. Check winner / loser discrepancy detection
        mismatch_rec = RecoveryResult(
            is_success=True,
            analysis_result=None,
            redo_result=None,
            undo_result=None,
        )
        err_winner_mismatch = WorkloadExecutor.validate_database_state(
            db_path=temp_db_path,
            expected_committed_pages={},
            recovery_result=mismatch_rec,
            expected_winners=[1, 2],
            expected_losers=[],
        )
        assert any("Recovery winners mismatch" in err for err in err_winner_mismatch)

    # -------------------------------------------------------------------------
    # 17. Repeated updates to the same page and rollback
    # -------------------------------------------------------------------------
    def test_17_repeated_updates_same_page_and_rollback(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Verify that multiple repeated updates to the same page/slot are tracked and rolled back properly."""
        # 1. Abort scenario: 8 updates all on Page 0 (wrapping slots 0..3 twice)
        config_abort = WorkloadConfig(
            num_transactions=1,
            num_pages=2,
            operations_per_transaction=8,
            read_ratio=0.0,
            commit_ratio=0.0,
            abort_ratio=1.0,
            incomplete_ratio=0.0,
            same_page_probability=1.0,  # All operations on Page 0
            seed=424242,
        )

        res_abort = WorkloadExecutor.execute(
            config=config_abort,
            db_path=temp_db_path,
            wal_path=temp_wal_path,
        )
        assert res_abort.is_valid is True
        assert res_abort.transactions_aborted == 1

        # Inspect WAL: 1 BEGIN, 8 UPDATEs, 8 CLRs, 1 ABORT = 18 records
        with LogManager(wal_path=temp_wal_path) as lm:
            recs = lm.read_all()
            assert len(recs) == 18
            updates = [r for r in recs if r.record_type == LogRecordType.UPDATE]
            clrs = [r for r in recs if r.record_type == LogRecordType.CLR]
            assert len(updates) == 8
            assert len(clrs) == 8

        # Page 0 must be cleanly restored to baseline data
        with DiskManager(db_path=temp_db_path) as dm:
            p0 = dm.read_page(0)
            assert p0.read(0, 16) == b"PAGE_0000_INIT_B"

        # 2. Commit scenario: 8 updates all on Page 0
        db_commit = temp_db_path.with_name("repeat_commit.db")
        wal_commit = temp_wal_path.with_name("repeat_commit.wal")
        config_commit = WorkloadConfig(
            num_transactions=1,
            num_pages=2,
            operations_per_transaction=8,
            read_ratio=0.0,
            commit_ratio=1.0,
            abort_ratio=0.0,
            incomplete_ratio=0.0,
            same_page_probability=1.0,
            seed=424242,
        )

        res_commit = WorkloadExecutor.execute(
            config=config_commit,
            db_path=db_commit,
            wal_path=wal_commit,
        )
        assert res_commit.is_valid is True
        assert res_commit.transactions_committed == 1
