"""Run every tests/test_*.py in its own process (each one points screentime at scratch folders and a
test credential by changing module globals), with warnings as errors.

Run:     python tests/run_all.py            (or a few: python tests/run_all.py backup sync)
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    names = sys.argv[1:]
    scripts = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if names:
        scripts = [s for s in scripts if any(f"test_{n}" in os.path.basename(s) for n in names)]
    failed = []
    for script in scripts:
        name = os.path.basename(script)
        result = subprocess.run([sys.executable, "-W", "error", script], capture_output=True, text=True,
                                cwd=os.path.dirname(HERE))
        checks = result.stdout.count("PASS ")
        if result.returncode:
            failed.append(name)
            print(f"FAIL {name} (after {checks} checks)\n{result.stdout[-2000:]}{result.stderr[-3000:]}")
        else:
            print(f"ok   {name} ({checks} checks)")
    print(f"\n{len(scripts) - len(failed)} of {len(scripts)} test files passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
