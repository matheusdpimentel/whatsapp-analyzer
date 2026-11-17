"""Time the main stages on synthetic data of growing size (offline, fake provider,
requests_per_minute raised so the default rate limit does not dominate).

Usage: python scripts/benchmark.py [--sizes 100,1000,4000] [--exe PATH]
(sizes are requested conversation counts; the table reports what was generated)

Prints a Markdown table: wall-clock seconds per stage and the peak memory of the largest
child process (POSIX only; "n/a" on Windows). Results depend on the machine: record the
hardware next to any figure you publish (see docs/benchmarks.md).
"""

from __future__ import annotations

import argparse
import csv
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]


def peak_child_mb() -> str:
    if resource is None:
        return "n/a"
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return f"{peak / (1024 * 1024 if sys.platform == 'darwin' else 1024):.0f}"


def count_dataset(path: Path) -> tuple[int, int]:
    """Return (distinct conversations, message rows) of a canonical CSV, as actually written."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return len({row["conversation_id"] for row in rows}), len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="100,1000,4000", help="conversations per dataset")
    parser.add_argument("--exe", default=shutil.which("whatsapp-analyzer"))
    args = parser.parse_args()
    args.exe = str(Path(args.exe).resolve()) if args.exe else None
    if not args.exe:
        sys.exit("whatsapp-analyzer not found; pass --exe")
    print(f"Python {platform.python_version()} on {platform.platform()} ({platform.machine()})\n")
    print("| Conversations | Messages | import | metrics | llm run (fake) | export | peak MB* |")
    print("|---:|---:|---:|---:|---:|---:|---:|")
    for size in (int(s) for s in args.sizes.split(",")):
        with tempfile.TemporaryDirectory(prefix="wca-bench-") as tmp:
            root = Path(tmp)

            def run(*cmd: str, root: Path = root) -> tuple[float, str]:
                start = time.perf_counter()
                proc = subprocess.run(
                    [args.exe, *cmd],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                if proc.returncode != 0:
                    sys.exit(f"{' '.join(cmd)} failed ({proc.returncode}):\n{proc.stderr[-800:]}")
                return time.perf_counter() - start, proc.stdout

            run("demo-data", "d", "--seed", "7", "--conversations", str(size))
            run("init", "w")
            # The default 60 requests/minute applies to the offline fake provider too (a real
            # provider's limits are its own); lift it so the benchmark measures our code.
            config = root / "w" / "analyzer.toml"
            text = config.read_text(encoding="utf-8")
            assert "requests_per_minute = 60" in text
            config.write_text(
                text.replace("requests_per_minute = 60", "requests_per_minute = 100000")
            )
            seconds = {}
            seconds["import"], _ = run("import", "d/conversations.csv", "-w", "w")
            seconds["metrics"], _ = run("metrics", "run", "-w", "w")
            seconds["llm"], _ = run("llm", "run", "-w", "w")
            seconds["export"], _ = run("export", "-w", "w", "--out", "s.csv")
            conversations, messages = count_dataset(root / "d" / "conversations.csv")
            row = " | ".join(f"{seconds[k]:.1f}s" for k in ("import", "metrics", "llm", "export"))
            print(f"| {conversations} | {messages} | {row} | {peak_child_mb()} |")
    print("\n*Peak memory of the largest single child process so far (cumulative maximum).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
