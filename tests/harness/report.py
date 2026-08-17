"""Coloured PASS/FAIL tally for scenario/step assertions.

Usage:
    r = Results()
    r.check("app deployed", lambda: bool(app_url))
    r.assert_eq("25 cases seeded", 25, count)
    r.finish()   # prints the tally, raises SystemExit(1) if anything failed
"""
from __future__ import annotations

import sys
from typing import Callable

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


def step(msg: str) -> None:
    """A step banner."""
    print(f"\n▶ {msg}")


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failed_names: list[str] = []

    def _ok(self, name: str, extra: str = "") -> None:
        print(f"  {_GREEN}PASS{_RESET} {name}{extra}")
        self.passed += 1

    def _no(self, name: str, extra: str = "") -> None:
        print(f"  {_RED}FAIL{_RESET} {name}{extra}")
        self.failed += 1
        self.failed_names.append(name)

    def check(self, name: str, cond: bool | Callable[[], bool]) -> None:
        """Pass if `cond` is truthy. `cond` may be a bool or a zero-arg callable (a callable that raises
        counts as FAIL)."""
        try:
            ok = cond() if callable(cond) else bool(cond)
        except Exception as e:  # noqa: BLE001 — a raising probe is a failed check, not a crash
            ok = False
            name = f"{name}  ({type(e).__name__}: {e})"
        (self._ok if ok else self._no)(name)

    def assert_eq(self, name: str, expected, actual) -> None:
        if expected == actual:
            self._ok(name, f"  (={actual})")
        else:
            self._no(name, f"  expected [{expected}] got [{actual}]")

    def assert_contains(self, name: str, haystack: str, needle: str) -> None:
        if needle in (haystack or ""):
            self._ok(name)
        else:
            self._no(name, f"  [{needle}] not in output")

    def finish(self) -> None:
        print(f"\n── {self.passed} passed, {self.failed} failed ──")
        if self.failed:
            print(f"FAILED: {', '.join(self.failed_names)}")
            sys.exit(1)
