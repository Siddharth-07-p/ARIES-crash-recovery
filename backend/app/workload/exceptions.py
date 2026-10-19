"""Exceptions for workload generation, execution, and validation."""


class WorkloadError(Exception):
    """Base exception for all workload-related errors."""


class WorkloadConfigError(WorkloadError):
    """Raised when workload configuration parameters are invalid."""


class WorkloadExecutionError(WorkloadError):
    """Raised when an error occurs during workload execution."""


class WorkloadValidationError(WorkloadError):
    """Raised when post-workload verification or validation fails."""
