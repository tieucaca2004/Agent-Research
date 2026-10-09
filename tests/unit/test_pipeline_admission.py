"""Sprint 07: fetch admission control (D4 / OD-A = A1) — limits, grant order, slot safety."""

from __future__ import annotations

import asyncio
import random

import pytest

from research_agent.pipeline.admission import AdmissionTicket, BodyBudget, FetchAdmission


def admission(global_limit: int = 5, per_host: int = 1, per_job: int = 5) -> FetchAdmission:
    return FetchAdmission(
        global_limit=global_limit,
        per_host_limit=per_host,
        per_job_limit=per_job,
        extra_ports=frozenset({8080}),
    )


def u(host: str, path: str = "/p") -> str:
    return f"http://{host}:8080{path}"


async def test_global_limit_and_position_order() -> None:
    adm = admission(global_limit=2, per_host=5)
    order: list[int] = []
    tickets: dict[int, AdmissionTicket] = {}

    async def take(p: int) -> None:
        ticket = await adm.acquire("j", p, u(f"h{p}.test"))
        assert ticket is not None
        order.append(p)
        tickets[p] = ticket

    tasks = [asyncio.create_task(take(p)) for p in (4, 2, 0, 3, 1)]
    await asyncio.sleep(0)
    assert adm.in_flight == 2 and adm.waiting == 3
    while len(order) < 5:
        await asyncio.sleep(0)
        for p in list(order):
            if p in tickets and not tickets[p].released and adm.waiting:
                adm.release(tickets[p])
                break
    await asyncio.gather(*tasks)
    # the first two are whoever registered first; after that the lowest pending position wins
    assert order[2:] == sorted(order[2:])
    assert adm.max_in_flight == 2


async def test_per_host_limit_uses_the_s04_host_key() -> None:
    adm = admission(global_limit=5, per_host=1)
    assert adm.host_key(u("A.Test.")) == "a.test"  # S04 check_url: lower case, no trailing dot
    first = await adm.acquire("j", 0, u("a.test", "/1"))
    waiter = asyncio.create_task(adm.acquire("j", 1, u("A.TEST", "/2")))
    other = await adm.acquire("j", 2, u("b.test"))
    await asyncio.sleep(0)
    assert not waiter.done() and other is not None
    assert first is not None
    adm.release(first)
    second = await waiter
    assert second is not None and second.host == "a.test"
    assert adm.max_in_flight_by_host["a.test"] == 1


async def test_url_rejected_by_s04_needs_no_host_slot() -> None:
    adm = admission(global_limit=1)
    assert adm.host_key("http://a.test:1/") is None  # port not allowed by S04 policy
    assert adm.host_key("javascript:alert(1)") is None
    ticket = await adm.acquire("j", 0, "http://a.test:1/")
    assert ticket is not None and ticket.host is None
    adm.release(ticket)
    assert adm.in_flight == 0


async def test_per_job_limit_and_round_robin_between_jobs() -> None:
    adm = admission(global_limit=4, per_host=10, per_job=3)
    granted: list[str] = []

    async def take(job: str, p: int) -> AdmissionTicket:
        ticket = await adm.acquire(job, p, u(f"{job}{p}.test"))
        assert ticket is not None
        granted.append(job)
        return ticket

    a = [asyncio.create_task(take("A", p)) for p in range(6)]
    await asyncio.sleep(0)
    b = [asyncio.create_task(take("B", p)) for p in range(6)]
    await asyncio.sleep(0)
    assert adm.in_flight_for("A") == 3  # per-job cap, although the global limit is 4
    assert adm.in_flight_for("B") == 1
    done = [t.result() for t in a if t.done()]
    for ticket in done:
        adm.release(ticket)
    await asyncio.sleep(0)
    # after A releases, grants alternate instead of serving A's backlog first
    assert granted[4:7].count("B") >= 1 and granted[4:7].count("A") >= 1
    for task in a + b:
        if not task.done():
            task.cancel()
    await asyncio.gather(*a, *b, return_exceptions=True)
    for task in a + b:
        if task.done() and not task.cancelled() and task.exception() is None:
            adm.release(task.result())
    assert adm.in_flight == 0 and adm.waiting == 0
    assert max(adm.max_in_flight_by_job.values()) <= 3


async def test_deadline_while_waiting_returns_none_and_frees_nothing() -> None:
    adm = admission(global_limit=1)
    loop = asyncio.get_running_loop()
    holder = await adm.acquire("j", 0, u("a.test"))
    assert holder is not None
    assert await adm.acquire("j", 1, u("b.test"), deadline=loop.time() + 0.05) is None
    assert await adm.acquire("j", 2, u("c.test"), deadline=loop.time() - 1) is None
    assert adm.waiting == 0 and adm.in_flight == 1
    adm.release(holder)
    adm.release(holder)  # idempotent
    assert adm.in_flight == 0


async def test_cancelled_waiter_is_removed() -> None:
    adm = admission(global_limit=1)
    holder = await adm.acquire("j", 0, u("a.test"))
    waiter = asyncio.create_task(adm.acquire("j", 1, u("b.test")))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert adm.waiting == 0
    assert holder is not None
    adm.release(holder)
    assert adm.in_flight == 0


async def test_grant_racing_cancellation_releases_the_slot() -> None:
    """A waiter granted and cancelled before it resumes must not leak its slot."""
    adm = admission(global_limit=1)
    holder = await adm.acquire("j", 0, u("a.test"))
    assert holder is not None
    waiter = asyncio.create_task(adm.acquire("j", 1, u("b.test")))
    await asyncio.sleep(0)
    adm.release(holder)  # grants the waiter (future resolved)…
    waiter.cancel()  # …and it is cancelled before running again
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert adm.in_flight == 0 and adm.waiting == 0
    again = await adm.acquire("j", 2, u("c.test"))
    assert again is not None


async def test_randomized_schedules_never_leak_slots() -> None:
    """A11: 1000 seeded schedules mixing completion, cancellation, deadlines and exceptions."""
    loop = asyncio.get_running_loop()
    for seed in range(1000):
        rng = random.Random(seed)  # noqa: S311 - reproducible test schedule, not crypto
        adm = admission(
            global_limit=rng.randint(1, 4), per_host=rng.randint(1, 2), per_job=rng.randint(1, 3)
        )
        bodies = BodyBudget(rng.randint(1, 4))

        async def worker(
            job: str,
            p: int,
            adm: FetchAdmission = adm,
            rng: random.Random = rng,
            bodies: BodyBudget = bodies,
        ) -> None:
            deadline = loop.time() + rng.choice([0.0005, 0.002, 1.0])
            ticket = await adm.acquire(job, p, u(f"h{rng.randint(0, 3)}.test"), deadline=deadline)
            if ticket is None:
                return
            try:
                held = await bodies.acquire(deadline=deadline)
                try:
                    for _ in range(rng.randint(0, 3)):
                        await asyncio.sleep(0)
                    if rng.random() < 0.1:
                        raise RuntimeError("boom")
                finally:
                    if held:
                        bodies.release()
            finally:
                adm.release(ticket)

        tasks = [
            asyncio.create_task(worker(rng.choice("AB"), p)) for p in range(rng.randint(1, 12))
        ]
        for _ in range(rng.randint(0, 4)):
            await asyncio.sleep(0)
        for task in tasks:
            if rng.random() < 0.2:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert adm.in_flight == 0 and adm.waiting == 0, seed
        assert bodies.held == 0, seed
        assert adm.max_in_flight <= adm.global_limit, seed


async def test_body_budget_bounds_and_deadline() -> None:
    bodies = BodyBudget(2)
    loop = asyncio.get_running_loop()
    assert await bodies.acquire() and await bodies.acquire()
    assert not await bodies.acquire(deadline=loop.time() + 0.02)
    bodies.release()
    assert await bodies.acquire(deadline=loop.time() + 0.02)
    assert bodies.max_held == 2
    with pytest.raises(ValueError):
        BodyBudget(0)
    with pytest.raises(ValueError):
        FetchAdmission(global_limit=0, per_host_limit=1, per_job_limit=1)
