"""Benchmark exception hierarchy."""


class BenchmarkError(Exception):
    """Base exception for all benchmarking errors."""
    pass


class BenchmarkConfigError(BenchmarkError):
    """Raised when benchmark configuration parameters are invalid."""
    pass


class BenchmarkExecutionError(BenchmarkError):
    """Raised when benchmark execution fails."""
    pass


class BenchmarkExportError(BenchmarkError):
    """Raised when exporting benchmark results fails."""
    pass
