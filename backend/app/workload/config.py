"""Configuration data structures and validation for synthetic workloads."""

from typing import Any, Dict, Optional, Union
from app.fault.constants import DEFAULT_CRASH_EXIT_CODE, FaultPoint
from app.workload.exceptions import WorkloadConfigError


class WorkloadConfig:
    """Configurable parameters governing synthetic transaction workload generation and execution.

    Attributes:
        num_transactions: Total number of transactions to generate (>= 0).
        num_pages: Number of accessible database pages (>= 1).
        operations_per_transaction: Number of operations (read/update) per transaction (>= 0).
        read_ratio: Probability of an operation being a READ (0.0 to 1.0).
        commit_ratio: Probability of a transaction committing (0.0 to 1.0).
        abort_ratio: Probability of a transaction aborting (0.0 to 1.0).
        incomplete_ratio: Probability of a transaction left active/incomplete (0.0 to 1.0).
        same_page_probability: Probability that an operation targets page 0 instead of uniform distribution.
        interleave_probability: Probability of scheduling operations in an interleaved order.
        seed: Deterministic integer seed for reproducible plan generation.
        pool_size: BufferManager pool capacity (>= 2).
        crash_fault_point: Optional FaultPoint at which to trigger a simulated crash.
        crash_exit_code: Exit code when fault injection terminates process.
        auto_recover: Whether to automatically invoke RecoveryManager after workload or crash.
        checkpoint_interval: Optional interval in operations at which to trigger a checkpoint.
    """

    def __init__(
        self,
        num_transactions: int = 10,
        num_pages: int = 4,
        operations_per_transaction: int = 5,
        read_ratio: float = 0.3,
        commit_ratio: float = 0.7,
        abort_ratio: float = 0.2,
        incomplete_ratio: float = 0.1,
        same_page_probability: float = 0.2,
        interleave_probability: float = 0.5,
        seed: Optional[int] = 42,
        pool_size: int = 10,
        crash_fault_point: Optional[Union[FaultPoint, str]] = None,
        crash_exit_code: int = DEFAULT_CRASH_EXIT_CODE,
        auto_recover: bool = True,
        checkpoint_interval: Optional[int] = None,
    ) -> None:
        """Initialize and validate WorkloadConfig."""
        self._validate_and_set(
            num_transactions=num_transactions,
            num_pages=num_pages,
            operations_per_transaction=operations_per_transaction,
            read_ratio=read_ratio,
            commit_ratio=commit_ratio,
            abort_ratio=abort_ratio,
            incomplete_ratio=incomplete_ratio,
            same_page_probability=same_page_probability,
            interleave_probability=interleave_probability,
            seed=seed,
            pool_size=pool_size,
            crash_fault_point=crash_fault_point,
            crash_exit_code=crash_exit_code,
            auto_recover=auto_recover,
            checkpoint_interval=checkpoint_interval,
        )

    def _validate_and_set(
        self,
        num_transactions: int,
        num_pages: int,
        operations_per_transaction: int,
        read_ratio: float,
        commit_ratio: float,
        abort_ratio: float,
        incomplete_ratio: float,
        same_page_probability: float,
        interleave_probability: float,
        seed: Optional[int],
        pool_size: int,
        crash_fault_point: Optional[Union[FaultPoint, str]],
        crash_exit_code: int,
        auto_recover: bool,
        checkpoint_interval: Optional[int],
    ) -> None:
        """Validate input values and assign internal fields."""
        if not isinstance(num_transactions, int) or isinstance(num_transactions, bool) or num_transactions < 0:
            raise WorkloadConfigError(
                f"num_transactions must be a non-negative integer, got {num_transactions!r}"
            )

        if not isinstance(num_pages, int) or isinstance(num_pages, bool) or num_pages < 1:
            raise WorkloadConfigError(
                f"num_pages must be an integer >= 1, got {num_pages!r}"
            )

        if not isinstance(operations_per_transaction, int) or isinstance(operations_per_transaction, bool) or operations_per_transaction < 0:
            raise WorkloadConfigError(
                f"operations_per_transaction must be a non-negative integer, got {operations_per_transaction!r}"
            )

        for name, val in [
            ("read_ratio", read_ratio),
            ("commit_ratio", commit_ratio),
            ("abort_ratio", abort_ratio),
            ("incomplete_ratio", incomplete_ratio),
            ("same_page_probability", same_page_probability),
            ("interleave_probability", interleave_probability),
        ]:
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not (0.0 <= float(val) <= 1.0):
                raise WorkloadConfigError(
                    f"{name} must be a float between 0.0 and 1.0, got {val!r}"
                )

        if num_transactions > 0:
            ratio_sum = float(commit_ratio) + float(abort_ratio) + float(incomplete_ratio)
            if abs(ratio_sum - 1.0) > 1e-5:
                raise WorkloadConfigError(
                    f"commit_ratio ({commit_ratio}) + abort_ratio ({abort_ratio}) + "
                    f"incomplete_ratio ({incomplete_ratio}) must sum to 1.0, got {ratio_sum:.4f}"
                )

        if seed is not None:
            if not isinstance(seed, int) or isinstance(seed, bool):
                raise WorkloadConfigError(f"seed must be an integer or None, got {seed!r}")

        if not isinstance(pool_size, int) or isinstance(pool_size, bool) or pool_size < 2:
            raise WorkloadConfigError(f"pool_size must be an integer >= 2, got {pool_size!r}")

        norm_fault_point: Optional[FaultPoint] = None
        if crash_fault_point is not None:
            if isinstance(crash_fault_point, FaultPoint):
                norm_fault_point = crash_fault_point
            elif isinstance(crash_fault_point, str):
                try:
                    norm_fault_point = FaultPoint(crash_fault_point)
                except ValueError:
                    raise WorkloadConfigError(f"Invalid crash_fault_point: {crash_fault_point!r}")
            else:
                raise WorkloadConfigError(
                    f"crash_fault_point must be FaultPoint, str, or None, got {type(crash_fault_point).__name__}"
                )

        if not isinstance(crash_exit_code, int) or isinstance(crash_exit_code, bool):
            raise WorkloadConfigError(f"crash_exit_code must be an integer, got {crash_exit_code!r}")

        if not isinstance(auto_recover, bool):
            raise WorkloadConfigError(f"auto_recover must be a boolean, got {auto_recover!r}")

        if checkpoint_interval is not None:
            if not isinstance(checkpoint_interval, int) or isinstance(checkpoint_interval, bool) or checkpoint_interval < 1:
                raise WorkloadConfigError(
                    f"checkpoint_interval must be an integer >= 1 or None, got {checkpoint_interval!r}"
                )

        self._num_transactions: int = num_transactions
        self._num_pages: int = num_pages
        self._operations_per_transaction: int = operations_per_transaction
        self._read_ratio: float = float(read_ratio)
        self._commit_ratio: float = float(commit_ratio)
        self._abort_ratio: float = float(abort_ratio)
        self._incomplete_ratio: float = float(incomplete_ratio)
        self._same_page_probability: float = float(same_page_probability)
        self._interleave_probability: float = float(interleave_probability)
        self._seed: Optional[int] = seed
        self._pool_size: int = pool_size
        self._crash_fault_point: Optional[FaultPoint] = norm_fault_point
        self._crash_exit_code: int = crash_exit_code
        self._auto_recover: bool = auto_recover
        self._checkpoint_interval: Optional[int] = checkpoint_interval

    @property
    def num_transactions(self) -> int:
        return self._num_transactions

    @property
    def num_pages(self) -> int:
        return self._num_pages

    @property
    def operations_per_transaction(self) -> int:
        return self._operations_per_transaction

    @property
    def read_ratio(self) -> float:
        return self._read_ratio

    @property
    def update_ratio(self) -> float:
        return 1.0 - self._read_ratio

    @property
    def commit_ratio(self) -> float:
        return self._commit_ratio

    @property
    def abort_ratio(self) -> float:
        return self._abort_ratio

    @property
    def incomplete_ratio(self) -> float:
        return self._incomplete_ratio

    @property
    def same_page_probability(self) -> float:
        return self._same_page_probability

    @property
    def interleave_probability(self) -> float:
        return self._interleave_probability

    @property
    def seed(self) -> Optional[int]:
        return self._seed

    @property
    def pool_size(self) -> int:
        return self._pool_size

    @property
    def crash_fault_point(self) -> Optional[FaultPoint]:
        return self._crash_fault_point

    @property
    def crash_exit_code(self) -> int:
        return self._crash_exit_code

    @property
    def auto_recover(self) -> bool:
        return self._auto_recover

    @property
    def checkpoint_interval(self) -> Optional[int]:
        return self._checkpoint_interval

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "num_transactions": self._num_transactions,
            "num_pages": self._num_pages,
            "operations_per_transaction": self._operations_per_transaction,
            "read_ratio": self._read_ratio,
            "commit_ratio": self._commit_ratio,
            "abort_ratio": self._abort_ratio,
            "incomplete_ratio": self._incomplete_ratio,
            "same_page_probability": self._same_page_probability,
            "interleave_probability": self._interleave_probability,
            "seed": self._seed,
            "pool_size": self._pool_size,
            "crash_fault_point": self._crash_fault_point.value if self._crash_fault_point else None,
            "crash_exit_code": self._crash_exit_code,
            "auto_recover": self._auto_recover,
            "checkpoint_interval": self._checkpoint_interval,
        }

    def __repr__(self) -> str:
        return (
            f"WorkloadConfig(txs={self._num_transactions}, pages={self._num_pages}, "
            f"ops_per_tx={self._operations_per_transaction}, read_ratio={self._read_ratio:.2f}, "
            f"commit={self._commit_ratio:.2f}, abort={self._abort_ratio:.2f}, "
            f"incomplete={self._incomplete_ratio:.2f}, seed={self._seed})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkloadConfig):
            return False
        return self.to_dict() == other.to_dict()
