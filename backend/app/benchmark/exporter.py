"""Export facilities for benchmark scenario results to JSON and CSV formats."""

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Union
from app.benchmark.exceptions import BenchmarkExportError
from app.benchmark.models import BenchmarkScenarioResult


class BenchmarkExporter:
    """Exports benchmark run metrics and aggregate statistics to file formats."""

    CSV_HEADERS: List[str] = [
        "scenario_name",
        "run_index",
        "is_warmup",
        "is_success",
        "validation_passed",
        "workload_duration_s",
        "tx_throughput_tps",
        "committed_tps",
        "op_throughput_ops",
        "transactions_started",
        "transactions_committed",
        "transactions_aborted",
        "transactions_incomplete",
        "reads_executed",
        "updates_executed",
        "executed_operations",
        "recovery_duration_s",
        "analysis_duration_s",
        "redo_duration_s",
        "undo_duration_s",
        "records_redone",
        "clrs_generated",
        "updates_undone",
        "pages_modified_count",
        "winners_count",
        "losers_count",
        "error_message",
    ]

    @classmethod
    def export_json(
        cls,
        result: BenchmarkScenarioResult,
        filepath: Union[str, Path],
    ) -> Path:
        """Export benchmark result to JSON file.

        Args:
            result: BenchmarkScenarioResult to export.
            filepath: Destination file path.

        Returns:
            Resolved Path of the saved file.

        Raises:
            BenchmarkExportError: If writing fails.
        """
        try:
            out_path = Path(filepath).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result.to_dict(), f, indent=2)
            return out_path
        except Exception as exc:
            raise BenchmarkExportError(f"Failed to export JSON to {filepath}: {exc}") from exc

    @classmethod
    def export_csv(
        cls,
        result: BenchmarkScenarioResult,
        filepath: Union[str, Path],
    ) -> Path:
        """Export per-run benchmark metrics to CSV file.

        Args:
            result: BenchmarkScenarioResult to export.
            filepath: Destination file path.

        Returns:
            Resolved Path of the saved file.

        Raises:
            BenchmarkExportError: If writing fails.
        """
        try:
            out_path = Path(filepath).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(cls.CSV_HEADERS)

                scenario_name = result.config.scenario_name

                for run in result.all_runs:
                    wl = run.workload_metrics
                    rec = run.recovery_metrics

                    row: List[Any] = [
                        scenario_name,
                        run.run_index,
                        run.is_warmup,
                        run.is_success,
                        run.validation_passed,
                        f"{wl.duration_s:.6f}" if wl else "",
                        f"{wl.tx_throughput_tps:.2f}" if wl else "",
                        f"{wl.committed_tps:.2f}" if wl else "",
                        f"{wl.op_throughput_ops:.2f}" if wl else "",
                        wl.transactions_started if wl else "",
                        wl.transactions_committed if wl else "",
                        wl.transactions_aborted if wl else "",
                        wl.transactions_incomplete if wl else "",
                        wl.reads_executed if wl else "",
                        wl.updates_executed if wl else "",
                        wl.executed_operations if wl else "",
                        f"{rec.total_duration_s:.6f}" if rec else "",
                        f"{rec.analysis_duration_s:.6f}" if rec and rec.analysis_duration_s is not None else "",
                        f"{rec.redo_duration_s:.6f}" if rec and rec.redo_duration_s is not None else "",
                        f"{rec.undo_duration_s:.6f}" if rec and rec.undo_duration_s is not None else "",
                        rec.records_redone if rec else "",
                        rec.clrs_generated if rec else "",
                        rec.updates_undone if rec else "",
                        rec.pages_modified_count if rec else "",
                        rec.winners_count if rec else "",
                        rec.losers_count if rec else "",
                        run.error_message or "",
                    ]
                    writer.writerow(row)

            return out_path
        except Exception as exc:
            raise BenchmarkExportError(f"Failed to export CSV to {filepath}: {exc}") from exc
