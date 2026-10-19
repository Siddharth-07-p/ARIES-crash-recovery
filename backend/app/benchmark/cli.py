"""Command-line interface for running ARIES database benchmarks."""

import argparse
from pathlib import Path
import sys
from typing import List, Optional
from app.benchmark.config import BenchmarkConfig
from app.benchmark.exporter import BenchmarkExporter
from app.benchmark.models import BenchmarkScenarioResult
from app.benchmark.runner import BenchmarkRunner
from app.benchmark.scenarios import get_preset_scenario, list_preset_scenarios


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="ARIES Database Performance Benchmarking Framework",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default="small_workload",
        help="Preset scenario name to execute (or 'all' for all presets)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of measured benchmark repetitions",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Number of unmeasured warmup runs",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic random seed",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional destination path for JSON export",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Optional destination path for CSV export",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all registered benchmark preset scenarios and exit",
    )
    return parser.parse_args(args)


def print_summary_table(result: BenchmarkScenarioResult) -> None:
    """Print a formatted console summary of the benchmark scenario result."""
    cfg = result.config
    print("\n" + "=" * 78)
    print(f" BENCHMARK REPORT: {cfg.scenario_name.upper()}")
    print("=" * 78)
    print(f"Status:            {'PASSED' if result.is_success else 'FAILED'}")
    print(f"Repetitions:       {len(result.measured_runs)} measured ({len(result.warmup_runs)} warmup)")
    print(f"Valid Runs:        {len(result.valid_runs)} / {len(result.measured_runs)}")

    if result.environment:
        env = result.environment
        print(f"Environment:       {env.os_name} {env.architecture} | Python {env.python_version} ({env.cpu_count} CPUs)")

    print("-" * 78)
    print(f"{'Metric':<25} | {'Mean':<10} | {'Median':<10} | {'Min':<10} | {'Max':<10} | {'StdDev':<10}")
    print("-" * 78)

    for metric_name, dist in result.aggregates.items():
        print(
            f"{metric_name:<25} | {dist.mean:<10.4f} | {dist.median:<10.4f} | "
            f"{dist.min:<10.4f} | {dist.max:<10.4f} | {dist.std_dev:<10.4f}"
        )

    print("=" * 78 + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)

    if args.list:
        print("\nAvailable preset scenarios:")
        for name in list_preset_scenarios():
            print(f"  - {name}")
        print()
        return 0

    scenarios_to_run: List[str] = (
        list_preset_scenarios() if args.scenario.lower() == "all" else [args.scenario]
    )

    all_passed = True

    for sc_name in scenarios_to_run:
        try:
            config = get_preset_scenario(
                sc_name,
                repetitions=args.runs,
                warmup_runs=args.warmup,
                seed=args.seed,
            )
        except Exception as exc:
            print(f"Error configuring scenario '{sc_name}': {exc}", file=sys.stderr)
            return 1

        print(f"Executing scenario '{sc_name}' ({args.runs} runs, {args.warmup} warmup)...")
        result = BenchmarkRunner.run(config)
        print_summary_table(result)

        if not result.is_success:
            all_passed = False

        if args.output_json:
            json_file = (
                args.output_json
                if len(scenarios_to_run) == 1
                else args.output_json.with_name(f"{args.output_json.stem}_{sc_name}.json")
            )
            BenchmarkExporter.export_json(result, json_file)
            print(f"Exported JSON: {json_file}")

        if args.output_csv:
            csv_file = (
                args.output_csv
                if len(scenarios_to_run) == 1
                else args.output_csv.with_name(f"{args.output_csv.stem}_{sc_name}.csv")
            )
            BenchmarkExporter.export_csv(result, csv_file)
            print(f"Exported CSV:  {csv_file}")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
