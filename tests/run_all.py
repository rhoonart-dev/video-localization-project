#!/usr/bin/env python3
"""의존성 없는 테스트 러너 (pytest 미설치 환경용).

tests/test_*.py 의 test_* 함수를 모두 실행하고 PASS/FAIL/ERROR 를 집계한다.
pytest 가 있으면 `pytest -q` 도 동일하게 동작한다(conftest.py 가 경로 설정).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    passed = 0
    failures: list[str] = []
    for f in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            mod = _load(f)
        except Exception:  # noqa: BLE001
            failures.append(f"IMPORT {f.name}\n{traceback.format_exc()}")
            continue
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
            except Exception:  # noqa: BLE001
                failures.append(f"{f.name}::{name}\n{traceback.format_exc()}")

    print(f"\n{'=' * 60}")
    print(f"PASSED: {passed}   FAILED/ERROR: {len(failures)}")
    for fail in failures:
        print("-" * 60)
        print(fail)
    print("=" * 60)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
