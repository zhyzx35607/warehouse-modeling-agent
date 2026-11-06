"""Circuit Breaker for external service calls.

Prevents cascading failures by tracking failure counts and
temporarily blocking calls to failing services.

States: CLOSED → OPEN (after N failures) → HALF_OPEN (after timeout)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"        # Normal: calls pass through
    OPEN = "open"            # Blocking: calls fail fast
    HALF_OPEN = "half_open"  # Probing: one call allowed to test recovery


class CircuitBreaker:
    """Failure-aware circuit breaker for async operations.

    Usage:
        cb = CircuitBreaker("dolphin_scheduler", failure_threshold=3, timeout=30)
        result = await cb.call(my_async_func, arg1, arg2)

    Args:
        name: Service name for logging
        failure_threshold: Consecutive failures to trigger OPEN
        timeout: Seconds before transitioning OPEN → HALF_OPEN
        success_threshold: Consecutive successes in HALF_OPEN to CLOSE
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        timeout: float = 30.0,
        success_threshold: int = 2,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.success_threshold = success_threshold

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        self._last_failure: Optional[str] = None
        self._total_failures = 0
        self._total_successes = 0

    @property
    def state(self) -> CircuitState:
        self._maybe_transition()
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    def _maybe_transition(self):
        """Check and perform state transitions."""
        now = time.monotonic()

        # OPEN → HALF_OPEN after timeout
        if self._state == CircuitState.OPEN:
            if now - self._last_failure_time >= self.timeout:
                self._state = CircuitState.HALF_OPEN
                logger.info(f"[{self.name}] OPEN → HALF_OPEN (timeout elapsed)")

        # HALF_OPEN → CLOSED after success threshold
        elif self._state == CircuitState.HALF_OPEN:
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                logger.info(f"[{self.name}] HALF_OPEN → CLOSED (recovered)")

    def record_success(self):
        """Record a successful call."""
        self._total_successes += 1
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
        else:
            self._failure_count = 0
            self._success_count = 0

    def record_failure(self, error: str = ""):
        """Record a failed call."""
        self._total_failures += 1
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        self._last_failure = error
        self._success_count = 0

        if (self._state == CircuitState.CLOSED and
                self._failure_count >= self.failure_threshold):
            self._state = CircuitState.OPEN
            logger.warning(
                f"[{self.name}] CLOSED → OPEN "
                f"({self._failure_count} failures, last: {error[:100]})"
            )
        elif self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning(
                f"[{self.name}] HALF_OPEN → OPEN (probe failed: {error[:100]})"
            )

    async def call(self, fn: Callable, *args, **kwargs) -> Any:
        """Call `fn` with circuit breaker protection.

        Raises CircuitBreakerOpenError if circuit is OPEN.
        Otherwise calls fn and tracks success/failure.
        """
        self._maybe_transition()

        if self._state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"[{self.name}] Circuit is OPEN — calls blocked. "
                f"Last failure: {self._last_failure or 'unknown'}"
            )

        try:
            result = await fn(*args, **kwargs)
            self.record_success()
            return result
        except CircuitBreakerOpenError:
            raise
        except Exception as e:
            self.record_failure(str(e))
            raise

    def stats(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "total_failures": self._total_failures,
            "total_successes": self._total_successes,
            "last_failure": self._last_failure,
            "is_open": self.is_open,
        }


class CircuitBreakerOpenError(Exception):
    """Raised when a call is blocked by an open circuit breaker."""
    pass


# ── Circuit breaker registry ─────────────────────────────────────

_breakers: Dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    """Get or create a named circuit breaker."""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name=name, **kwargs)
    return _breakers[name]


def get_all_stats() -> list[dict]:
    """Get stats for all circuit breakers."""
    return [cb.stats() for cb in _breakers.values()]


# ── Decorator ────────────────────────────────────────────────────

def with_circuit_breaker(
    name: str,
    failure_threshold: int = 3,
    timeout: float = 30.0,
):
    """Decorator to wrap an async function with a circuit breaker.

    Usage:
        @with_circuit_breaker("dolphin_scheduler", failure_threshold=3)
        async def deploy_dag(...):
            ...
    """
    def decorator(fn):
        breaker = get_breaker(name, failure_threshold=failure_threshold, timeout=timeout)

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return await breaker.call(fn, *args, **kwargs)

        wrapper._breaker = breaker
        return wrapper
    return decorator
