"""Poll-until-condition helpers — including the CI-run-id poll and the credential-validate retry.

All take a zero-arg predicate/producer and a timeout; they return True/the value on success, or print a
timeout line and return False/None — never raise, so a scenario reports a clean FAIL instead of a crash.
"""
from __future__ import annotations

import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


def wait_until(desc: str, timeout: int, predicate: Callable[[], bool], interval: int = 5) -> bool:
    """Poll `predicate` until it returns truthy or `timeout` seconds elapse. A raising predicate = not-yet."""
    t = 0
    while True:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 — a transient error just means "not ready yet"
            pass
        t += interval
        if t >= timeout:
            print(f"  (timeout after {timeout}s waiting for: {desc})")
            return False
        time.sleep(interval)


def wait_value(desc: str, timeout: int, produce: Callable[[], Optional[T]],
               interval: int = 5) -> Optional[T]:
    """Poll `produce` until it returns a non-None value or timeout; return the value or None."""
    t = 0
    while True:
        try:
            v = produce()
            if v is not None:
                return v
        except Exception:  # noqa: BLE001
            pass
        t += interval
        if t >= timeout:
            print(f"  (timeout after {timeout}s waiting for: {desc})")
            return None
        time.sleep(interval)


def wait_equals(desc: str, timeout: int, produce: Callable[[], object], want: object,
                interval: int = 5) -> bool:
    """Poll `produce` until it equals `want` (e.g. an investigation status). Prints last-seen on timeout."""
    t = 0
    last = None
    while True:
        try:
            last = produce()
            if last == want:
                return True
        except Exception:  # noqa: BLE001
            pass
        t += interval
        if t >= timeout:
            print(f"  (timeout {timeout}s: {desc} is '{last}', wanted '{want}')")
            return False
        time.sleep(interval)
