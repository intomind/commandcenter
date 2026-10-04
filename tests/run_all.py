"""Run every test suite. No device, no network.

    python3 tests/run_all.py

This is the command. There is no other. If a suite exists and is not run from
here, it will rot.
"""
from __future__ import annotations
import pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
SUITES = sorted(p for p in HERE.glob("test_*.py"))

failed, total, passed = [], 0, 0
for s in SUITES:
    r = subprocess.run([sys.executable, str(s)], capture_output=True, text=True,
                       cwd=HERE)
    tail = [l for l in r.stdout.splitlines() if "passed" in l]
    line = tail[-1] if tail else "(no summary)"
    print(f"{s.name:24s} {line.strip()}")
    if r.returncode != 0:
        failed.append(s.name)
        for l in r.stdout.splitlines():
            if l.strip().startswith("FAIL"):
                print(f"    {l.strip()}")
        if r.stderr.strip():
            print("   ", r.stderr.strip().splitlines()[-1])
    try:
        n, d = line.split()[0].split("/")
        passed += int(n); total += int(d)
    except Exception:
        # A suite whose summary we cannot parse contributed ZERO tests to the
        # count, silently, and the run still said PASS. That is how a suite
        # rots into decoration. It is a failure.
        failed.append(f"{s.name} (no parsable 'n/n passed' summary. Its tests "
                      f"were NOT counted)")
        print(f"    FAIL: no 'n/n passed' summary; this suite was not counted")

print("-" * 52)
print(f"{passed}/{total} tests passed across {len(SUITES)} suite(s)")
if failed:
    print("FAILED SUITES:", ", ".join(failed))
sys.exit(1 if failed else 0)
