"""Deterministic synthetic transaction workload plan generator."""

from collections import deque
import random
from typing import Dict, List, Optional
from app.storage.constants import PAGE_SIZE
from app.workload.config import WorkloadConfig
from app.workload.exceptions import WorkloadError
from app.workload.models import (
    OperationType,
    TransactionOutcome,
    WorkloadOperation,
    WorkloadPlan,
)


class WorkloadGenerator:
    """Generates reproducible, deterministic synthetic database workload plans.

    A fixed WorkloadConfig and seed produce the exact same sequence of WorkloadOperations.
    Distinguishes plan generation from execution and crash recovery outcomes.
    """

    @classmethod
    def generate(
        cls,
        config: WorkloadConfig,
        seed: Optional[int] = None,
    ) -> WorkloadPlan:
        """Generate a deterministic WorkloadPlan based on the given configuration.

        Args:
            config: WorkloadConfig specifying parameters and distributions.
            seed: Optional seed override. If None, uses config.seed.

        Returns:
            WorkloadPlan containing the planned sequence of operations.
        """
        actual_seed = seed if seed is not None else config.seed
        rng = random.Random(actual_seed)

        if config.num_transactions == 0:
            return WorkloadPlan(
                config=config,
                operations=[],
                planned_transactions={},
            )

        # 1. Determine transaction outcomes deterministically
        planned_transactions: Dict[int, TransactionOutcome] = {}
        for tx_id in range(1, config.num_transactions + 1):
            r = rng.random()
            if r < config.commit_ratio:
                planned_transactions[tx_id] = TransactionOutcome.COMMITTED
            elif r < (config.commit_ratio + config.abort_ratio):
                planned_transactions[tx_id] = TransactionOutcome.ABORTED
            else:
                planned_transactions[tx_id] = TransactionOutcome.INCOMPLETE

        # 2. Build per-transaction operation sequences
        tx_queues: Dict[int, deque[WorkloadOperation]] = {}
        for tx_id in range(1, config.num_transactions + 1):
            queue: deque[WorkloadOperation] = deque()

            # BEGIN operation
            queue.append(
                WorkloadOperation(
                    step_index=0,
                    transaction_id=tx_id,
                    operation_type=OperationType.BEGIN,
                )
            )

            # Body operations (READ or UPDATE)
            for op_idx in range(config.operations_per_transaction):
                is_read = rng.random() < config.read_ratio
                op_type = OperationType.READ if is_read else OperationType.UPDATE

                # Page target selection (same_page_probability hotspot vs uniform)
                if rng.random() < config.same_page_probability:
                    page_id = 0
                else:
                    page_id = rng.randint(0, config.num_pages - 1)

                # 16-byte slot alignment within the 4KB page
                # Partition slots by transaction to prevent uncommitted dirty-write anomalies
                max_slots = PAGE_SIZE // 16
                slot = ((tx_id - 1) * 4 + (op_idx % 4)) % max_slots
                offset = slot * 16
                length = 16

                if op_type == OperationType.UPDATE:
                    noise = rng.randint(100, 999)
                    data = f"T{tx_id:03d}O{op_idx:03d}P{page_id:02d}D{noise:03d}".encode("ascii")[:16]
                else:
                    data = None

                queue.append(
                    WorkloadOperation(
                        step_index=0,
                        transaction_id=tx_id,
                        operation_type=op_type,
                        page_id=page_id,
                        offset=offset,
                        length=length,
                        data=data,
                    )
                )

            # Terminal operation (COMMIT, ABORT, or None if INCOMPLETE)
            outcome = planned_transactions[tx_id]
            if outcome == TransactionOutcome.COMMITTED:
                queue.append(
                    WorkloadOperation(
                        step_index=0,
                        transaction_id=tx_id,
                        operation_type=OperationType.COMMIT,
                    )
                )
            elif outcome == TransactionOutcome.ABORTED:
                queue.append(
                    WorkloadOperation(
                        step_index=0,
                        transaction_id=tx_id,
                        operation_type=OperationType.ABORT,
                    )
                )

            tx_queues[tx_id] = queue

        # 3. Schedule operations: Interleaved or Sequential
        scheduled_ops: List[WorkloadOperation] = []
        is_interleaved = (
            config.interleave_probability > 0.0
            and rng.random() < config.interleave_probability
            and config.num_transactions > 1
        )

        if is_interleaved:
            # First, emit BEGIN for all transactions to activate them
            for tx_id in range(1, config.num_transactions + 1):
                if tx_queues[tx_id] and tx_queues[tx_id][0].operation_type == OperationType.BEGIN:
                    scheduled_ops.append(tx_queues[tx_id].popleft())

            # Interleave body operations across transactions
            while True:
                # Find transactions that still have body operations (non-terminal)
                eligible_txs = [
                    tx_id
                    for tx_id in range(1, config.num_transactions + 1)
                    if tx_queues[tx_id]
                    and tx_queues[tx_id][0].operation_type in (OperationType.READ, OperationType.UPDATE)
                ]
                if not eligible_txs:
                    break
                chosen_tx = rng.choice(eligible_txs)
                scheduled_ops.append(tx_queues[chosen_tx].popleft())

            # Finally, emit remaining terminal operations (COMMIT or ABORT)
            for tx_id in range(1, config.num_transactions + 1):
                while tx_queues[tx_id]:
                    scheduled_ops.append(tx_queues[tx_id].popleft())
        else:
            # Sequential execution: complete each transaction before starting the next
            for tx_id in range(1, config.num_transactions + 1):
                while tx_queues[tx_id]:
                    scheduled_ops.append(tx_queues[tx_id].popleft())

        # 4. Assign definitive sequential step_index
        indexed_ops: List[WorkloadOperation] = []
        for idx, op in enumerate(scheduled_ops):
            indexed_ops.append(
                WorkloadOperation(
                    step_index=idx,
                    transaction_id=op.transaction_id,
                    operation_type=op.operation_type,
                    page_id=op.page_id,
                    offset=op.offset,
                    length=op.length,
                    data=op.data,
                )
            )

        return WorkloadPlan(
            config=config,
            operations=indexed_ops,
            planned_transactions=planned_transactions,
        )
