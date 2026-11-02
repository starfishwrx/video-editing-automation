"""Bounded preparation-to-consumer handoff; no request owns a media slot."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def iter_ready(jobs, prepare, consume, *, prep_workers, api_workers, on_error):
    """Yield completed (job, result) pairs without a whole-round barrier.

    At most prep_workers + api_workers jobs are admitted at once. Preparation
    errors never reach consume; completion order does not define editorial order.
    The consumer retains responsibility for shared account slots and deadlines.
    """
    if prep_workers < 1 or api_workers < 1:
        raise ValueError("worker counts must be positive")
    jobs = iter(jobs)
    exhausted = False
    limit = prep_workers + api_workers
    with ThreadPoolExecutor(max_workers=prep_workers) as prep_pool, \
            ThreadPoolExecutor(max_workers=api_workers) as api_pool:
        active = {}
        while active or not exhausted:
            while (not exhausted and len(active) < limit
                   and sum(kind == "prep" for kind, _ in active.values()) < prep_workers):
                try:
                    job = next(jobs)
                except StopIteration:
                    exhausted = True
                    break
                active[prep_pool.submit(prepare, job)] = ("prep", job)
            if not active:
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                kind, job = active.pop(future)
                try:
                    value = future.result()
                except Exception as exc:
                    yield job, on_error(job, exc)
                    continue
                if kind == "prep":
                    active[api_pool.submit(consume, job)] = ("api", job)
                else:
                    yield job, value
