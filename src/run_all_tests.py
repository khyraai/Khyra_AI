"""
run_all_tests.py — Master test runner.

Executes all test suites in order and prints a combined pass/fail summary.

Suites:
  1. test_isolation.py    — DB schema, client upsert, DID routing, booking isolation, call logs
  2. test_did_welcome.py  — DID-swap welcome routing (DB-only path)
  3. test_guardrails.py   — Security guardrails Layer 1 (regex) + Layer 2 (live LLM)

Usage (inside container):
    python /app/src/run_all_tests.py
"""

import os
import sys
import subprocess
import time

SUITE_DIR = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    {
        "name":  "Isolation & DB Schema",
        "file":  "test_isolation.py",
        "desc":  "client_id columns, DID routing, booking isolation, call logs",
    },
    {
        "name":  "DID-Swap Welcome Routing",
        "file":  "test_did_welcome.py",
        "desc":  "DID → clinic resolution, dynamic welcome, cache invalidation",
    },
    {
        "name":  "Security Guardrails",
        "file":  "test_guardrails.py",
        "desc":  "Pre-filter regex + live LLM deflection on all 5 agents",
    },
]

WIDTH = 68


def run_suite(suite: dict) -> tuple[bool, str, float]:
    path = os.path.join(SUITE_DIR, suite["file"])
    t0 = time.time()
    result = subprocess.run(
        [sys.executable, path],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    elapsed = time.time() - t0
    output = result.stdout + result.stderr
    passed = result.returncode == 0
    return passed, output, elapsed


def main():
    print("=" * WIDTH)
    print("  Khyra AI — Full Regression Test Suite")
    print("=" * WIDTH)
    print()

    suite_results = []

    for i, suite in enumerate(SUITES, 1):
        print(f"{'─' * WIDTH}")
        print(f"  [{i}/{len(SUITES)}] {suite['name']}")
        print(f"  {suite['desc']}")
        print(f"{'─' * WIDTH}")

        passed, output, elapsed = run_suite(suite)

        # Print the suite output indented
        for line in output.splitlines():
            print(f"  {line}")

        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"\n  → {status}  ({elapsed:.1f}s)")
        print()
        suite_results.append((suite["name"], passed, elapsed))

    # ── Combined summary ────────────────────────────────────────────────
    print("=" * WIDTH)
    print("  FINAL SUMMARY")
    print("=" * WIDTH)
    total_pass = sum(1 for _, ok, _ in suite_results if ok)
    total_fail = sum(1 for _, ok, _ in suite_results if not ok)
    total_time = sum(t for _, _, t in suite_results)

    for name, ok, elapsed in suite_results:
        icon = "✅" if ok else "❌"
        print(f"  {icon}  {name:<35}  ({elapsed:.1f}s)")

    print()
    print(f"  Suites passed : {total_pass}/{len(suite_results)}")
    print(f"  Total time    : {total_time:.1f}s")
    print("=" * WIDTH)

    if total_fail > 0:
        print("\n  ❌ Some suites FAILED — see output above for details.\n")
        sys.exit(1)
    else:
        print("\n  ✅ All suites passed.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
