"""The agent loop: a queue of changed files draining over one slow link.

`sync_client.sync_file` moves a single file. This is the layer above it: a
queue that the watcher and the reconciliation scan feed, a small number of
workers draining it, and a retry policy for a file whose transfer failed
outright.

Why asyncio here and plain blocking code underneath. Almost all of the time in
this agent is spent waiting, either on the network or on a backoff after a
failure, and asyncio lets one thread hold a long queue of pending files without
a thread each and without locks around the queue. The work that is not waiting
(reading a file, hashing it, pushing bytes) is ordinary blocking code in
sync_client, so it runs in a thread pool through run_in_executor. That keeps
the event loop free, and it means the next file can be hashed while the current
one is still on the wire.

Concurrency is deliberately capped low. The link is the bottleneck, so eight
transfers at once does not finish the batch sooner, it just makes every file
slower and widens the blast radius when the connection drops. Two workers is
enough to keep the link busy while the next file is being hashed.

Standard library only, Python 3.9+.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace

from sync_client import PermanentError, SyncClient, TransientError


@dataclass
class Job:
    """One file to sync: where it is locally, what it is called on the server."""
    local_path: str
    remote_path: str


@dataclass
class Outcome:
    """What happened to one job, including how many attempts it took."""
    remote_path: str
    action: str          # uploaded | skipped | linked | changed | vanished | rejected | failed
    detail: str
    attempts: int = 1
    bytes_sent: int = 0

    @property
    def ok(self) -> bool:
        return self.action != "failed"

    def __str__(self) -> str:
        tail = f" (attempt {self.attempts})" if self.attempts > 1 else ""
        return f"{self.remote_path}: {self.action}, {self.detail}{tail}"


class SyncAgent:
    """Drains a queue of jobs, retrying a file that fails to transfer.

    Two levels of retry are in play and they cover different things.
    sync_client retries a single HTTP request that did not get through. This
    class retries the whole file when that has already been exhausted, which is
    the case where the link is properly down rather than briefly lossy, and it
    waits a lot longer between goes.
    """

    def __init__(self, client: SyncClient, workers: int = 2, max_attempts: int = 3,
                 base_delay: float = 1.0, max_delay: float = 60.0, log=None):
        self.client = client
        self.workers = max(1, workers)
        self.max_attempts = max(1, max_attempts)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.log = log

    def _say(self, message: str) -> None:
        if callable(self.log):
            self.log(message)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, same reasoning as the client:
        a link that comes back brings every queued file with it, and retrying
        in lockstep is how you knock the server over twice."""
        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return random.uniform(0, ceiling)

    async def run(self, jobs) -> list:
        """Run a batch of jobs to completion and return one Outcome each.

        In the full agent the queue is fed continuously by the watcher and the
        scan and never closes. A finite batch is the same code with an end.
        """
        jobs = list(jobs)
        if not jobs:
            return []

        queue: asyncio.Queue = asyncio.Queue()
        for job in jobs:
            queue.put_nowait(job)

        results: list = []
        workers = [asyncio.create_task(self._worker(queue, results))
                   for _ in range(min(self.workers, len(jobs)))]
        try:
            await queue.join()
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return results

    async def _worker(self, queue: asyncio.Queue, results: list) -> None:
        # Each worker gets its own client. SyncClient carries a bytes_sent
        # counter, and two threads incrementing one counter is a data race that
        # would quietly under-report exactly the number this design is judged
        # on. Separate clients also keep one worker's retry state to itself.
        client = replace(self.client, bytes_sent=0)
        while True:
            job = await queue.get()
            try:
                results.append(await self._sync_one(client, job))
            finally:
                queue.task_done()

    async def _sync_one(self, client: SyncClient, job: Job) -> Outcome:
        loop = asyncio.get_running_loop()
        start = client.bytes_sent
        last: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                # Blocking work off the event loop. Hashing a large file is
                # CPU and disk bound, and hashlib releases the GIL for blocks
                # over 2047 bytes, so it genuinely overlaps with a transfer
                # running in another worker.
                result = await loop.run_in_executor(
                    None, client.sync_file, job.local_path, job.remote_path)

            except PermanentError as e:
                # Retrying will not fix this one, so say so and move on rather
                # than burning the link on it.
                self._say(f"{job.remote_path}: failed, {e}")
                return Outcome(job.remote_path, "failed",
                               f"not retried, this will not succeed on a repeat: {e}",
                               attempt, client.bytes_sent - start)

            except TransientError as e:
                last = e
                if attempt < self.max_attempts:
                    delay = self._backoff(attempt)
                    self._say(f"{job.remote_path}: attempt {attempt} of "
                              f"{self.max_attempts} failed ({e}), retrying in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    continue

            except Exception as e:  # noqa: BLE001 - an unknown failure is still a result
                # Never let one bad file take the agent down with it. Report it
                # with the type name so the log says something useful.
                self._say(f"{job.remote_path}: failed, unexpected {type(e).__name__}: {e}")
                return Outcome(job.remote_path, "failed",
                               f"unexpected {type(e).__name__}: {e}",
                               attempt, client.bytes_sent - start)

            else:
                outcome = Outcome(job.remote_path, result.action, result.detail,
                                  attempt, client.bytes_sent - start)
                self._say(str(outcome))
                return outcome

        detail = f"transfer failed after {self.max_attempts} attempts: {last}"
        self._say(f"{job.remote_path}: {detail}")
        return Outcome(job.remote_path, "failed", detail,
                       self.max_attempts, client.bytes_sent - start)


def sync_all(client: SyncClient, jobs, **kwargs) -> list:
    """Blocking wrapper, for a caller that is not already running an event loop."""
    return asyncio.run(SyncAgent(client, **kwargs).run(jobs))
