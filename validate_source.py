from __future__ import annotations

import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def main() -> int:
    py_files = sorted(p for p in ROOT.glob("*.py") if p.name != "__init__.py")
    failed = []
    for p in py_files:
        try:
            py_compile.compile(str(p), doraise=True)
            print(f"OK {p.name}")
        except Exception as e:
            failed.append((p.name, str(e)))
            print(f"FAIL {p.name}: {e}")
    if failed:
        print("FAILED", failed)
        return 1
    print("OK all python files compile")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
