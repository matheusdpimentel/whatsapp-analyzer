"""Requests-per-minute and tokens-per-minute limits as token buckets on an injectable clock.

Both buckets start full (one minute of budget) and refill continuously. A request that needs
more tokens than a whole minute allows waits for a full bucket and then takes it, so it is
never blocked forever.
"""

from whatsapp_analyzer.runner.model import Clock

MINUTE = 60.0


class _Bucket:
    def __init__(self, per_minute: int, clock: Clock) -> None:
        self.capacity = float(per_minute)
        self.level = float(per_minute)
        self.rate = per_minute / MINUTE
        self.clock = clock
        self.updated = clock.now()

    def refill(self) -> None:
        now = self.clock.now()
        self.level = min(self.capacity, self.level + (now - self.updated) * self.rate)
        self.updated = now

    def wait_for(self, amount: float) -> float:
        self.refill()
        needed = min(amount, self.capacity)
        return 0.0 if self.level >= needed else (needed - self.level) / self.rate

    def take(self, amount: float) -> None:
        self.level -= amount


class RateLimiter:
    def __init__(self, rpm: int | None, tpm: int | None, clock: Clock) -> None:
        self.clock = clock
        self.requests = _Bucket(rpm, clock) if rpm else None
        self.tokens = _Bucket(tpm, clock) if tpm else None

    async def acquire(self, tokens: int) -> None:
        while True:
            waits = [0.0]
            if self.requests is not None:
                waits.append(self.requests.wait_for(1))
            if self.tokens is not None:
                waits.append(self.tokens.wait_for(tokens))
            wait = max(waits)
            if wait <= 0:
                if self.requests is not None:
                    self.requests.take(1)
                if self.tokens is not None:
                    self.tokens.take(tokens)
                return
            await self.clock.sleep(wait)
