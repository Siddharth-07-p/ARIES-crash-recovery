"""Models and data structures representing workload operations, plans, and results."""

from enum import Enum
from typing import Any, Dict, List, Optional
from app.recovery.recovery_state import RecoveryResult
from app.workload.config import WorkloadConfig
from app.workload.exceptions import WorkloadError


class OperationType(str, Enum):
    """Types of database operations generated in a workload plan."""
    BEGIN = "BEGIN"
    READ = "READ"
    UPDATE = "UPDATE"
    COMMIT = "COMMIT"
    ABORT = "ABORT"


class TransactionOutcome(str, Enum):
    """Expected terminal outcome of a generated transaction."""
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"
    INCOMPLETE = "INCOMPLETE"


class WorkloadOperation:
    """Represents a single deterministic operation within a workload plan.

    Attributes:
        step_index: Zero-based sequence index of this operation in the plan.
        transaction_id: Positive integer ID of the transaction executing the operation.
        operation_type: OperationType (BEGIN, READ, UPDATE, COMMIT, ABORT).
        page_id: Target page ID (for READ and UPDATE), or None.
        offset: Byte offset within the page (for READ and UPDATE), or None.
        length: Byte length read or modified, or None.
        data: Payload bytes written (for UPDATE), or None.
    """

    __slots__ = (
        "_step_index",
        "_transaction_id",
        "_operation_type",
        "_page_id",
        "_offset",
        "_length",
        "_data",
    )

    def __init__(
        self,
        step_index: int,
        transaction_id: int,
        operation_type: OperationType,
        page_id: Optional[int] = None,
        offset: Optional[int] = None,
        length: Optional[int] = None,
        data: Optional[bytes] = None,
    ) -> None:
        self._step_index: int = step_index
        self._transaction_id: int = transaction_id
        self._operation_type: OperationType = operation_type
        self._page_id: Optional[int] = page_id
        self._offset: Optional[int] = offset
        self._length: Optional[int] = length
        self._data: Optional[bytes] = bytes(data) if data is not None else None

    @property
    def step_index(self) -> int:
        return self._step_index

    @property
    def transaction_id(self) -> int:
        return self._transaction_id

    @property
    def operation_type(self) -> OperationType:
        return self._operation_type

    @property
    def page_id(self) -> Optional[int]:
        return self._page_id

    @property
    def offset(self) -> Optional[int]:
        return self._offset

    @property
    def length(self) -> Optional[int]:
        return self._length

    @property
    def data(self) -> Optional[bytes]:
        return self._data

    def to_dict(self) -> Dict[str, Any]:
        """Convert operation to dictionary representation."""
        return {
            "step_index": self._step_index,
            "transaction_id": self._transaction_id,
            "operation_type": self._operation_type.value,
            "page_id": self._page_id,
            "offset": self._offset,
            "length": self._length,
            "data": self._data.hex() if self._data is not None else None,
        }

    def __repr__(self) -> str:
        details = []
        if self._page_id is not None:
            details.append(f"p={self._page_id}")
        if self._offset is not None:
            details.append(f"off={self._offset}")
        if self._data is not None:
            details.append(f"data={self._data!r}")
        extra = f" ({', '.join(details)})" if details else ""
        return f"Op#{self._step_index}(T{self._transaction_id}:{self._operation_type.value}{extra})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkloadOperation):
            return False
        return (
            self._step_index == other._step_index
            and self._transaction_id == other._transaction_id
            and self._operation_type == other._operation_type
            and self._page_id == other._page_id
            and self._offset == other._offset
            and self._length == other._length
            and self._data == other._data
        )


class WorkloadPlan:
    """Deterministic, immutable sequence of planned operations generated from a configuration."""

    def __init__(
        self,
        config: WorkloadConfig,
        operations: List[WorkloadOperation],
        planned_transactions: Dict[int, TransactionOutcome],
    ) -> None:
        self._config: WorkloadConfig = config
        self._operations: List[WorkloadOperation] = list(operations)
        self._planned_transactions: Dict[int, TransactionOutcome] = dict(planned_transactions)

    @property
    def config(self) -> WorkloadConfig:
        return self._config

    @property
    def operations(self) -> List[WorkloadOperation]:
        return list(self._operations)

    @property
    def planned_transactions(self) -> Dict[int, TransactionOutcome]:
        return dict(self._planned_transactions)

    def __len__(self) -> int:
        return len(self._operations)

    def __getitem__(self, index: int) -> WorkloadOperation:
        return self._operations[index]

    def to_dict(self) -> Dict[str, Any]:
        """Convert plan to serializable dictionary."""
        return {
            "config": self._config.to_dict(),
            "operations_count": len(self._operations),
            "planned_transactions": {
                tx_id: outcome.value for tx_id, outcome in self._planned_transactions.items()
            },
            "operations": [op.to_dict() for op in self._operations],
        }

    def __repr__(self) -> str:
        return f"WorkloadPlan(txs={len(self._planned_transactions)}, ops={len(self._operations)})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkloadPlan):
            return False
        return (
            self._config == other._config
            and self._operations == other._operations
            and self._planned_transactions == other._planned_transactions
        )


class WorkloadResult:
    """Encapsulates the complete, immutable execution and validation outcome of a synthetic workload."""

    __slots__ = (
        "_config",
        "_seed",
        "_planned_operations",
        "_executed_operations",
        "_transactions_started",
        "_transactions_committed",
        "_transactions_aborted",
        "_transactions_incomplete",
        "_reads_executed",
        "_updates_executed",
        "_crash_configured",
        "_crash_observed",
        "_fault_point",
        "_exit_code",
        "_recovery_result",
        "_is_valid",
        "_validation_errors",
        "_committed_transactions",
        "_aborted_transactions",
        "_incomplete_transactions",
    )

    def __init__(
        self,
        config: WorkloadConfig,
        seed: Optional[int],
        planned_operations: int,
        executed_operations: int,
        transactions_started: int,
        transactions_committed: int,
        transactions_aborted: int,
        transactions_incomplete: int,
        reads_executed: int,
        updates_executed: int,
        crash_configured: bool,
        crash_observed: bool,
        is_valid: bool,
        fault_point: Optional[str] = None,
        exit_code: Optional[int] = None,
        recovery_result: Optional[RecoveryResult] = None,
        validation_errors: Optional[List[str]] = None,
        committed_transactions: Optional[List[int]] = None,
        aborted_transactions: Optional[List[int]] = None,
        incomplete_transactions: Optional[List[int]] = None,
    ) -> None:
        self._config: WorkloadConfig = config
        self._seed: Optional[int] = seed
        self._planned_operations: int = planned_operations
        self._executed_operations: int = executed_operations
        self._transactions_started: int = transactions_started
        self._transactions_committed: int = transactions_committed
        self._transactions_aborted: int = transactions_aborted
        self._transactions_incomplete: int = transactions_incomplete
        self._reads_executed: int = reads_executed
        self._updates_executed: int = updates_executed
        self._crash_configured: bool = crash_configured
        self._crash_observed: bool = crash_observed
        self._fault_point: Optional[str] = fault_point
        self._exit_code: Optional[int] = exit_code
        self._recovery_result: Optional[RecoveryResult] = recovery_result
        self._is_valid: bool = is_valid
        self._validation_errors: List[str] = list(validation_errors or [])
        self._committed_transactions: List[int] = sorted(list(committed_transactions or []))
        self._aborted_transactions: List[int] = sorted(list(aborted_transactions or []))
        self._incomplete_transactions: List[int] = sorted(list(incomplete_transactions or []))

    @property
    def config(self) -> WorkloadConfig:
        return self._config

    @property
    def seed(self) -> Optional[int]:
        return self._seed

    @property
    def planned_operations(self) -> int:
        return self._planned_operations

    @property
    def executed_operations(self) -> int:
        return self._executed_operations

    @property
    def transactions_started(self) -> int:
        return self._transactions_started

    @property
    def transactions_committed(self) -> int:
        return self._transactions_committed

    @property
    def transactions_aborted(self) -> int:
        return self._transactions_aborted

    @property
    def transactions_incomplete(self) -> int:
        return self._transactions_incomplete

    @property
    def reads_executed(self) -> int:
        return self._reads_executed

    @property
    def updates_executed(self) -> int:
        return self._updates_executed

    @property
    def crash_configured(self) -> bool:
        return self._crash_configured

    @property
    def crash_observed(self) -> bool:
        return self._crash_observed

    @property
    def fault_point(self) -> Optional[str]:
        return self._fault_point

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    @property
    def recovery_result(self) -> Optional[RecoveryResult]:
        return self._recovery_result

    @property
    def is_valid(self) -> bool:
        return self._is_valid

    @property
    def validation_errors(self) -> List[str]:
        return list(self._validation_errors)

    @property
    def committed_transactions(self) -> List[int]:
        return list(self._committed_transactions)

    @property
    def aborted_transactions(self) -> List[int]:
        return list(self._aborted_transactions)

    @property
    def incomplete_transactions(self) -> List[int]:
        return list(self._incomplete_transactions)

    def to_dict(self) -> Dict[str, Any]:
        """Convert WorkloadResult to dictionary representation."""
        return {
            "config": self._config.to_dict(),
            "seed": self._seed,
            "planned_operations": self._planned_operations,
            "executed_operations": self._executed_operations,
            "transactions_started": self._transactions_started,
            "transactions_committed": self._transactions_committed,
            "transactions_aborted": self._transactions_aborted,
            "transactions_incomplete": self._transactions_incomplete,
            "reads_executed": self._reads_executed,
            "updates_executed": self._updates_executed,
            "crash_configured": self._crash_configured,
            "crash_observed": self._crash_observed,
            "fault_point": self._fault_point,
            "exit_code": self._exit_code,
            "recovery_result": self._recovery_result.to_dict() if self._recovery_result else None,
            "is_valid": self._is_valid,
            "validation_errors": list(self._validation_errors),
            "committed_transactions": list(self._committed_transactions),
            "aborted_transactions": list(self._aborted_transactions),
            "incomplete_transactions": list(self._incomplete_transactions),
        }

    def __repr__(self) -> str:
        status = "VALID" if self._is_valid else "INVALID"
        crash_str = f", crash={self._fault_point}" if self._crash_observed else ""
        return (
            f"WorkloadResult(status={status}, txs={self._transactions_started}, "
            f"committed={self._transactions_committed}, aborted={self._transactions_aborted}, "
            f"incomplete={self._transactions_incomplete}{crash_str})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkloadResult):
            return False
        return self.to_dict() == other.to_dict()
