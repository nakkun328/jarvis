"""In-memory brute-force limiter for the login endpoint.

Each attempt is recorded as a failure *before* the passphrase is checked and refunded if it turns
out correct, so a burst of concurrent guesses cannot slip past the limit. After ``free_attempts``
failures from one client the client is locked out for ``base_lockout`` seconds, doubling with each
further failure up to ``max_lockout``. A global budget caps failures across all clients in a
rolling window, which also bounds distributed guessing; the trade-off is that an attacker who
spends that budget can delay new logins until it recovers (existing sessions are unaffected, and
restarting the process resets all state). State is per process and lost on restart.
"""

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Client:
    failures: int = 0
    locked_until: float = 0.0
    last_seen: float = 0.0


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0


class LoginLimiter:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        free_attempts: int = 5,
        base_lockout: float = 30.0,
        max_lockout: float = 900.0,
        global_limit: int = 50,
        global_window: float = 900.0,
        failure_delay: float = 0.5,
        max_clients: int = 4096,
        forget_after: float = 3600.0,
    ) -> None:
        self.clock = clock
        self.free_attempts = free_attempts
        self.base_lockout = base_lockout
        self.max_lockout = max_lockout
        self.global_limit = global_limit
        self.global_window = global_window
        self.failure_delay = failure_delay
        self.max_clients = max_clients
        self.forget_after = forget_after
        self._clients: dict[str, _Client] = {}
        self._global: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._global and now - self._global[0] >= self.global_window:
            self._global.popleft()
        if len(self._clients) >= self.max_clients:
            idle = [k for k, c in self._clients.items() if c.locked_until <= now]
            for key in sorted(idle, key=lambda k: self._clients[k].last_seen)[
                : max(1, len(self._clients) // 4)
            ]:
                del self._clients[key]
            while len(self._clients) >= self.max_clients:
                oldest = min(self._clients, key=lambda k: self._clients[k].last_seen)
                del self._clients[oldest]

    def begin(self, client: str) -> Decision:
        """Check the limits and, when allowed, count this attempt as a failure."""
        now = self.clock()
        self._prune(now)
        state = self._clients.get(client)
        if state is not None and state.locked_until > now:
            return Decision(False, math.ceil(state.locked_until - now))
        if len(self._global) >= self.global_limit:
            return Decision(False, math.ceil(self.global_window - (now - self._global[0])))
        if state is None:
            state = self._clients[client] = _Client()
        elif now - state.last_seen > self.forget_after:
            state.failures = 0  # old failures fade out
        state.failures += 1
        state.last_seen = now
        if state.failures >= self.free_attempts:
            exponent = min(state.failures - self.free_attempts, 30)
            state.locked_until = now + min(self.base_lockout * 2**exponent, self.max_lockout)
        self._global.append(now)
        return Decision(True)

    def succeeded(self, client: str) -> None:
        """Forget this client's failures and refund the global budget for the attempt."""
        self._clients.pop(client, None)
        if self._global:
            self._global.pop()
