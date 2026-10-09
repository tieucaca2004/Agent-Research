"""Fetch admission control (Sprint 07, decision D4 / OD-A = A1).

S04's per-URL budget includes the time a fetch waits inside the crawler for the global semaphore
and the per-host slot (finding S04-F1, probes P1/P5). ``FetchAdmission`` lets a fetch into the
crawler only when a global, a per-host and a per-job slot are free, so that wait happens here,
outside the S04 budget. One instance per process, shared by every job using the one crawler.

Residual (accepted, OD-A = A1, measured in ``CrawlStats``): a redirect hop into a host that another
admitted fetch is using still waits inside S04 (P7); so do S04's per-host minimum interval and the
robots.txt lock. Admission cannot know redirect targets in advance.

Grant order: within a job the lowest pending position first; across jobs round-robin. Every
granted ticket must be released (``release`` is idempotent); a waiter that is cancelled or times
out after being granted releases its ticket itself, so no slot can leak.
"""

from __future__ import annotations

import asyncio
import bisect
import itertools
from collections import Counter
from dataclasses import dataclass, field

from research_agent.crawler.policy import PolicyViolation, check_url


@dataclass
class AdmissionTicket:
    job: str
    position: int
    host: str | None
    waited_s: float
    released: bool = False


@dataclass(order=True)
class _Waiter:
    position: int
    seq: int
    job: str = field(compare=False)
    host: str | None = field(compare=False)
    enqueued: float = field(compare=False)
    future: asyncio.Future[AdmissionTicket] = field(compare=False)


class FetchAdmission:
    def __init__(
        self,
        *,
        global_limit: int,
        per_host_limit: int,
        per_job_limit: int,
        extra_ports: frozenset[int] = frozenset(),
    ) -> None:
        if min(global_limit, per_host_limit, per_job_limit) < 1:
            raise ValueError("admission limits must be positive")
        self.global_limit = global_limit
        self.per_host_limit = per_host_limit
        self.per_job_limit = per_job_limit
        self._extra_ports = extra_ports
        self._in_flight = 0
        self._by_host: Counter[str] = Counter()
        self._by_job: Counter[str] = Counter()
        self._waiting: dict[str, list[_Waiter]] = {}
        self._jobs: list[str] = []
        self._next_job = 0
        self._seq = itertools.count()
        # observed maxima, for tests and logs
        self.max_in_flight = 0
        self.max_in_flight_by_host: Counter[str] = Counter()
        self.max_in_flight_by_job: Counter[str] = Counter()
        self.granted_total = 0

    # -- state ------------------------------------------------------------------------

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def waiting(self) -> int:
        return sum(len(w) for w in self._waiting.values())

    def in_flight_for(self, job: str) -> int:
        return self._by_job[job]

    def host_key(self, url: str) -> str | None:
        """S04's own host key (``check_url``); ``None`` for a URL S04 rejects before any
        network activity (it then needs no host slot)."""
        try:
            return check_url(url, extra_ports=self._extra_ports)
        except PolicyViolation:
            return None

    # -- acquire / release --------------------------------------------------------------

    async def acquire(
        self, job: str, position: int, url: str, *, deadline: float | None = None
    ) -> AdmissionTicket | None:
        """Wait for a slot. Returns ``None`` (nothing admitted) if the loop-time ``deadline``
        passes first; cancellation propagates after the waiter is removed."""
        loop = asyncio.get_running_loop()
        if deadline is not None and loop.time() >= deadline:
            return None
        waiter = _Waiter(
            position=position,
            seq=next(self._seq),
            job=job,
            host=self.host_key(url),
            enqueued=loop.time(),
            future=loop.create_future(),
        )
        if job not in self._waiting:
            self._waiting[job] = []
            if job not in self._jobs:
                self._jobs.append(job)
        bisect.insort(self._waiting[job], waiter)
        self._dispatch()
        try:
            if deadline is None:
                return await waiter.future
            async with asyncio.timeout_at(deadline):
                return await waiter.future
        except TimeoutError:
            self._abandon(waiter)
            return None
        except BaseException:
            self._abandon(waiter)
            raise

    def release(self, ticket: AdmissionTicket) -> None:
        if ticket.released:
            return
        ticket.released = True
        self._in_flight -= 1
        self._by_job[ticket.job] -= 1
        if ticket.host is not None:
            self._by_host[ticket.host] -= 1
        self._forget_idle(ticket.job)
        self._dispatch()

    # -- internals ----------------------------------------------------------------------

    def _abandon(self, waiter: _Waiter) -> None:
        future = waiter.future
        if future.done() and not future.cancelled():
            self.release(future.result())  # granted just before cancellation / timeout
            return
        if not future.done():
            future.cancel()
        queue = self._waiting.get(waiter.job)
        if queue is not None and waiter in queue:
            queue.remove(waiter)
        self._forget_idle(waiter.job)
        self._dispatch()

    def _forget_idle(self, job: str) -> None:
        if not self._waiting.get(job) and self._by_job[job] <= 0:
            self._waiting.pop(job, None)
            if job in self._jobs:
                index = self._jobs.index(job)
                self._jobs.pop(index)
                if index < self._next_job:
                    self._next_job -= 1
            del self._by_job[job]

    def _dispatch(self) -> None:
        loop = asyncio.get_running_loop()
        while self._in_flight < self.global_limit and self._jobs:
            granted = False
            count = len(self._jobs)
            for step in range(count):
                index = (self._next_job + step) % count
                job = self._jobs[index]
                queue = self._waiting.get(job)
                if queue:
                    # a waiter cancelled while queued has a cancelled future but leaves the queue
                    # only when its task runs again: never grant to it (no slot may leak)
                    queue[:] = [w for w in queue if not w.future.done()]
                if not queue or self._by_job[job] >= self.per_job_limit:
                    continue
                waiter = next(
                    (
                        w
                        for w in queue
                        if w.host is None or self._by_host[w.host] < self.per_host_limit
                    ),
                    None,
                )
                if waiter is None:
                    continue
                queue.remove(waiter)
                self._grant(waiter, loop.time())
                self._next_job = (index + 1) % count
                granted = True
                break
            if not granted:
                return

    def _grant(self, waiter: _Waiter, now: float) -> None:
        self._in_flight += 1
        self._by_job[waiter.job] += 1
        if waiter.host is not None:
            self._by_host[waiter.host] += 1
            self.max_in_flight_by_host[waiter.host] = max(
                self.max_in_flight_by_host[waiter.host], self._by_host[waiter.host]
            )
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        self.max_in_flight_by_job[waiter.job] = max(
            self.max_in_flight_by_job[waiter.job], self._by_job[waiter.job]
        )
        self.granted_total += 1
        waiter.future.set_result(
            AdmissionTicket(
                job=waiter.job,
                position=waiter.position,
                host=waiter.host,
                waited_s=now - waiter.enqueued,
            )
        )


class BodyBudget:
    """Bounds fetched bodies held per process (fetching, waiting for and running extraction).

    Taken after admission and before the fetch, released after extraction: while it is full,
    admitted fetches wait here (outside the S04 budget) instead of piling up bodies."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._semaphore = asyncio.Semaphore(capacity)
        self.held = 0
        self.max_held = 0

    async def acquire(self, *, deadline: float | None = None) -> bool:
        loop = asyncio.get_running_loop()
        if deadline is not None and loop.time() >= deadline:
            return False
        try:
            if deadline is None:
                await self._semaphore.acquire()
            else:
                async with asyncio.timeout_at(deadline):
                    await self._semaphore.acquire()
        except TimeoutError:
            return False
        self.held += 1
        self.max_held = max(self.max_held, self.held)
        return True

    def release(self) -> None:
        self.held -= 1
        self._semaphore.release()
