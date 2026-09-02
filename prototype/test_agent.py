"""Tests for the async agent loop.

sync_agent is the layer that turns "move this one file" into "keep this whole
directory in step over a link that keeps dropping". What matters here is not
the transfer itself, which test_sync.py covers, but the behaviour around it:
does a failed file get retried, does the retry resume rather than start again,
does a file that cannot be sent produce a usable error instead of taking the
agent down with it, and do the workers actually overlap.

    python3 -m unittest discover . -v
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest

import sync_server
from sync_agent import Job, SyncAgent, sync_all
from sync_client import PermanentError, SyncClient, TransientError, sha256_file

TEST_CHUNK = 64 * 1024
TOKEN = "demo-token"


class Fixture:
    """A throwaway server and watched directory. Mixed into both a sync and an
    async TestCase, so the blocking wrapper can be tested without an event loop
    already running underneath it."""

    def setUp(self):
        real_chunk = sync_server.CHUNK_SIZE
        sync_server.CHUNK_SIZE = TEST_CHUNK
        self.addCleanup(setattr, sync_server, "CHUNK_SIZE", real_chunk)

        self.root = tempfile.mkdtemp()
        self.local = os.path.join(self.root, "local")
        os.makedirs(self.local)
        self.httpd = sync_server.serve(os.path.join(self.root, "server"), 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.client = SyncClient(self.url, TOKEN, base_delay=0.01, max_attempts=2)
        self.store = self.httpd.RequestHandlerClass.store

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.local, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def agent(self, **kwargs) -> SyncAgent:
        kwargs.setdefault("base_delay", 0.01)
        kwargs.setdefault("max_attempts", 3)
        return SyncAgent(self.client, **kwargs)


class AgentBase(Fixture, unittest.IsolatedAsyncioTestCase):
    pass


class TestQueue(AgentBase):
    async def test_a_batch_of_files_all_arrive(self):
        """The whole queue drains and every file is on the server."""
        files = {f"capture-{i}.bin": os.urandom(TEST_CHUNK + i) for i in range(6)}
        jobs = [Job(self.write(name, data), name) for name, data in files.items()]

        outcomes = await self.agent(workers=2).run(jobs)

        self.assertEqual(len(outcomes), len(jobs))
        self.assertEqual({o.action for o in outcomes}, {"uploaded"})
        for name, data in files.items():
            blob = self.store.blob_path(sha256_file(os.path.join(self.local, name)))
            self.assertTrue(os.path.exists(blob), f"{name} never landed")
            self.assertEqual(os.path.getsize(blob), len(data))
        # Per-worker byte accounting adds up to the batch. One counter shared
        # across worker threads would quietly lose some of this.
        self.assertEqual(sum(o.bytes_sent for o in outcomes),
                         sum(len(d) for d in files.values()))

    async def test_an_empty_queue_is_not_an_error(self):
        self.assertEqual(await self.agent().run([]), [])

    async def test_a_missing_file_is_reported_rather_than_raised(self):
        """A file deleted between the scan and the transfer is normal in a
        directory being worked in. It is a result, not a crash."""
        outcomes = await self.agent().run(
            [Job(os.path.join(self.local, "gone.bin"), "gone.bin")])

        self.assertEqual(outcomes[0].action, "vanished")
        self.assertTrue(outcomes[0].ok)
        self.assertEqual(outcomes[0].attempts, 1)


class TestRetries(AgentBase):
    async def test_a_transfer_that_fails_once_is_retried_and_resumes(self):
        """The retry has to pick up where the drop happened, not start again.

        The hook drops the link partway through the first attempt only. If the
        retry restarted the file the byte count would come out higher than the
        file itself. It is exactly the file size, so nothing went twice.
        """
        data = os.urandom(TEST_CHUNK * 6)
        path = self.write("archive.bin", data)
        dropped = []

        def drop_once(chunk_index):
            if chunk_index == 2 and not dropped:
                dropped.append(chunk_index)
                raise TransientError("link went down")

        self.client.on_chunk = drop_once

        outcomes = await self.agent(workers=1).run([Job(path, "archive.bin")])

        self.assertEqual(outcomes[0].action, "uploaded")
        self.assertEqual(outcomes[0].attempts, 2, "should have taken a second go")
        self.assertEqual(outcomes[0].bytes_sent, len(data),
                         "a resume must not re-send bytes the server already had")
        self.assertTrue(os.path.exists(self.store.blob_path(sha256_file(path))))

    async def test_a_transfer_that_keeps_failing_reports_a_usable_error(self):
        """Once the attempts are spent the agent gives up loudly, says how many
        goes it had and why, and publishes nothing."""
        path = self.write("doomed.bin", os.urandom(TEST_CHUNK * 3))
        messages = []

        def always_drop(chunk_index):
            raise TransientError("connection reset by peer")

        self.client.on_chunk = always_drop

        outcomes = await self.agent(workers=1, max_attempts=3,
                                    log=messages.append).run([Job(path, "doomed.bin")])

        outcome = outcomes[0]
        self.assertEqual(outcome.action, "failed")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.attempts, 3)
        self.assertIn("after 3 attempts", outcome.detail)
        self.assertIn("connection reset by peer", outcome.detail)
        self.assertFalse(os.path.exists(self.store.blob_path(sha256_file(path))))
        self.assertTrue(any("retrying in" in m for m in messages),
                        "the operator should be told a retry is coming")

    async def test_a_permanent_failure_is_not_retried(self):
        """Retrying something that cannot succeed only wastes the link."""
        path = self.write("rejected.bin", os.urandom(TEST_CHUNK))

        def refuse(chunk_index):
            raise PermanentError("server rejected this client")

        self.client.on_chunk = refuse

        outcomes = await self.agent(max_attempts=5).run([Job(path, "rejected.bin")])

        self.assertEqual(outcomes[0].action, "failed")
        self.assertEqual(outcomes[0].attempts, 1, "a permanent error must not be retried")
        self.assertIn("server rejected this client", outcomes[0].detail)

    async def test_one_bad_file_does_not_stop_the_others(self):
        """A single unreadable file must not take the batch down with it."""
        good = self.write("good.bin", os.urandom(TEST_CHUNK))
        missing = os.path.join(self.local, "not-here.bin")

        outcomes = await self.agent(workers=2).run(
            [Job(missing, "not-here.bin"), Job(good, "good.bin")])

        by_path = {o.remote_path: o.action for o in outcomes}
        self.assertEqual(by_path["good.bin"], "uploaded")
        self.assertEqual(by_path["not-here.bin"], "vanished")


class TestConcurrency(AgentBase):
    async def test_workers_transfer_files_at_the_same_time(self):
        """Two workers should overlap: one file on the wire while the next is
        being read and hashed. The hook records how many are in flight at once."""
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def track(chunk_index):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.015)               # hold the slot long enough to overlap
            with lock:
                state["now"] -= 1

        self.client.on_chunk = track
        jobs = [Job(self.write(f"f{i}.bin", os.urandom(TEST_CHUNK * 3)), f"f{i}.bin")
                for i in range(4)]

        outcomes = await self.agent(workers=2).run(jobs)

        self.assertEqual({o.action for o in outcomes}, {"uploaded"})
        self.assertGreaterEqual(state["peak"], 2, "the workers ran one after another")


class TestBlockingWrapper(Fixture, unittest.TestCase):
    def test_sync_all_runs_from_ordinary_blocking_code(self):
        """Not every caller is async. sync_all owns the event loop for them."""
        path = self.write("plain.bin", os.urandom(TEST_CHUNK))

        outcomes = sync_all(self.client, [Job(path, "plain.bin")], base_delay=0.01)

        self.assertEqual(outcomes[0].action, "uploaded")
        self.assertTrue(os.path.exists(self.store.blob_path(sha256_file(path))))


if __name__ == "__main__":
    unittest.main()
