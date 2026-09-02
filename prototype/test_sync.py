"""Tests for the resumable upload prototype.

Each test maps onto one of the four core requirements, plus the failure modes
that are easy to claim and hard to actually get right. Standard library
unittest, so from this directory:

    python3 -m unittest discover . -v
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import sync_server
from sync_client import SyncClient, sha256_file

TEST_CHUNK = 64 * 1024  # small chunks keep the tests quick
TOKEN = "demo-token"
CLIENT = "workstation-01"


def digest_header(chunk: bytes) -> dict:
    return {"Content-Digest": "sha-256=:"
            + base64.b64encode(hashlib.sha256(chunk).digest()).decode() + ":"}


class Base(unittest.TestCase):
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
        self.client = SyncClient(self.url, TOKEN, base_delay=0.01, max_attempts=3)
        self.store = self.httpd.RequestHandlerClass.store

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()          # release the SQLite handles so rmtree works on Windows
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name: str, data: bytes) -> str:
        p = os.path.join(self.local, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def upload_id(self, path: str, local: str) -> str:
        return self.client.plan([{"path": path, "size": os.path.getsize(local),
                                  "sha256": sha256_file(local)}])[0]["upload_id"]

    def raw(self, method, path, body=None, headers=None, token=TOKEN):
        req = urllib.request.Request(
            self.url + path, data=body, method=method,
            headers={"Authorization": f"Bearer {token}", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class TestIntegrity(Base):
    def test_upload_is_byte_identical_on_the_server(self):
        """Integrity: what the server commits is exactly what the client read."""
        data = os.urandom(TEST_CHUNK * 3 + 517)  # deliberately not a whole number of chunks
        p = self.write("report.bin", data)

        r = self.client.sync_file(p, "report.bin")

        self.assertEqual(r.action, "uploaded")
        blob = self.store.blob_path(sha256_file(p))
        self.assertTrue(os.path.exists(blob))
        with open(blob, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertEqual(self.store.current(CLIENT, "report.bin"), sha256_file(p))

    def test_awkward_file_sizes_all_round_trip(self):
        """The off-by-one sizes around the chunk boundary, where resume and
        finalise arithmetic goes wrong if it is going to."""
        for label, n in [("empty", 0), ("one-byte", 1), ("under", TEST_CHUNK - 1),
                         ("exact", TEST_CHUNK), ("over", TEST_CHUNK + 1),
                         ("two-exact", TEST_CHUNK * 2)]:
            with self.subTest(size=label):
                data = os.urandom(n)
                p = self.write(f"{label}.bin", data)
                r = self.client.sync_file(p, f"{label}.bin")
                self.assertEqual(r.action, "uploaded")
                with open(self.store.blob_path(sha256_file(p)), "rb") as fh:
                    self.assertEqual(fh.read(), data)

    def test_corrupted_chunk_is_refused_before_it_touches_the_part_file(self):
        """A chunk whose Content-Digest does not match is rejected, the server's
        offset does not move, and no partial file is created at all."""
        data = os.urandom(TEST_CHUNK)
        p = self.write("x.bin", data)
        uid = self.upload_id("x.bin", p)

        wrong = base64.b64encode(hashlib.sha256(b"not this").digest()).decode()
        status, body = self.raw("PUT", f"/v1/uploads/{uid}/at/0", data,
                                {"Content-Digest": f"sha-256=:{wrong}:"})

        self.assertEqual(status, 422)
        self.assertEqual(body["offset"], 0)
        self.assertFalse(os.path.exists(self.store.part_path(uid)))


class TestRaceCondition(Base):
    def test_file_modified_mid_upload_is_not_published(self):
        """The important one.

        The file is edited in place while the upload is running, and its mtime
        is then restored so the client's cheap metadata check is fooled. The
        client therefore does try to finalise. The server hashes what it
        actually received, finds it does not match the digest the client
        committed to before it started reading, and refuses to publish.

        This is why the whole-file hash is verified server side rather than
        trusted from the client: metadata checks are an optimisation, the
        digest comparison is the guarantee.
        """
        original = b"A" * (TEST_CHUNK * 4)
        p = self.write("live.log", original)
        before_stat = os.stat(p)
        declared = sha256_file(p)

        def tamper(index):
            if index == 1:
                with open(p, "r+b") as fh:      # in-place edit, size unchanged
                    fh.seek(TEST_CHUNK * 3)
                    fh.write(b"B" * TEST_CHUNK)
                os.utime(p, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))

        self.client.on_chunk = tamper
        r = self.client.sync_file(p, "live.log")

        self.assertEqual(r.action, "changed")
        self.assertIn("did not match", r.detail)
        self.assertFalse(self.store.has_blob(declared), "torn read must not be published")
        self.assertIsNone(self.store.current(CLIENT, "live.log"))

        # And the next pass syncs the settled file correctly.
        self.client.on_chunk = None
        r2 = self.client.sync_file(p, "live.log")
        self.assertEqual(r2.action, "uploaded")
        self.assertEqual(self.store.current(CLIENT, "live.log"), sha256_file(p))

    def test_visible_modification_is_caught_before_finalising(self):
        """When the metadata check does fire we skip the pointless finalise
        round trip. Nothing is published either way."""
        p = self.write("notes.txt", b"C" * (TEST_CHUNK * 3))

        def grow(index):
            if index == 1:
                with open(p, "ab") as fh:
                    fh.write(b"D" * 4096)

        self.client.on_chunk = grow
        r = self.client.sync_file(p, "notes.txt")

        self.assertEqual(r.action, "changed")
        self.assertIn("during transfer", r.detail)
        self.assertIsNone(self.store.current(CLIENT, "notes.txt"))

    def test_file_deleted_mid_upload_does_not_crash_the_agent(self):
        """A directory being worked in will delete files under the agent. That
        is an outcome to report, not an exception to propagate."""
        p = self.write("doomed.bin", os.urandom(TEST_CHUNK * 3))

        def vanish(index):
            if index == 1:
                os.unlink(p)

        self.client.on_chunk = vanish
        r = self.client.sync_file(p, "doomed.bin")

        self.assertEqual(r.action, "vanished")
        self.assertIsNone(self.store.current(CLIENT, "doomed.bin"))


class TestReliability(Base):
    def test_interrupted_transfer_resumes_instead_of_restarting(self):
        """Reliability plus bandwidth: a dropped connection costs the chunk in
        flight, not the file."""
        data = os.urandom(TEST_CHUNK * 8)
        p = self.write("big.bin", data)

        class Dropped(Exception):
            pass

        def die(index):
            if index == 3:
                raise Dropped("connection lost")

        self.client.on_chunk = die
        with self.assertRaises(Dropped):
            self.client.sync_file(p, "big.bin")
        self.assertEqual(self.client.bytes_sent, TEST_CHUNK * 3)

        self.client.on_chunk = None
        r = self.client.sync_file(p, "big.bin")

        self.assertEqual(r.action, "uploaded")
        self.assertEqual(r.bytes_sent, TEST_CHUNK * 5, "resume must send only the tail")
        self.assertEqual(self.client.bytes_sent, TEST_CHUNK * 8, "no byte sent twice")
        with open(self.store.blob_path(sha256_file(p)), "rb") as fh:
            self.assertEqual(fh.read(), data)

    def test_replayed_chunk_is_idempotent(self):
        """A retry after a lost response must not duplicate data. PUT is
        idempotent (RFC 9110 s9.2.2) and the server enforces that here."""
        data = os.urandom(TEST_CHUNK * 2)
        p = self.write("dup.bin", data)
        uid = self.upload_id("dup.bin", p)

        chunk = data[:TEST_CHUNK]
        hdr = digest_header(chunk)
        s1, b1 = self.raw("PUT", f"/v1/uploads/{uid}/at/0", chunk, hdr)
        s2, b2 = self.raw("PUT", f"/v1/uploads/{uid}/at/0", chunk, hdr)

        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(b1["offset"], TEST_CHUNK)
        self.assertEqual(b2["offset"], TEST_CHUNK, "replay must not append twice")
        self.assertEqual(os.path.getsize(self.store.part_path(uid)), TEST_CHUNK)

    def test_out_of_order_chunk_is_refused_with_the_real_offset(self):
        data = os.urandom(TEST_CHUNK * 3)
        p = self.write("ooo.bin", data)
        uid = self.upload_id("ooo.bin", p)

        chunk = data[TEST_CHUNK * 2:]
        status, body = self.raw("PUT", f"/v1/uploads/{uid}/at/{TEST_CHUNK * 2}",
                                chunk, digest_header(chunk))

        self.assertEqual(status, 409)
        self.assertEqual(body["offset"], 0, "server tells the client where it actually is")
        self.assertFalse(os.path.exists(self.store.part_path(uid)))

    def test_client_recovers_when_the_server_offset_disagrees(self):
        """The 409 path end to end: the server already holds more than the
        client thinks, and the client resynchronises rather than restarting."""
        data = os.urandom(TEST_CHUNK * 4)
        p = self.write("skew.bin", data)
        uid = self.upload_id("skew.bin", p)

        # Someone else already delivered the first two chunks.
        for i in (0, 1):
            chunk = data[i * TEST_CHUNK:(i + 1) * TEST_CHUNK]
            self.raw("PUT", f"/v1/uploads/{uid}/at/{i * TEST_CHUNK}", chunk, digest_header(chunk))

        # Force the client to start from zero anyway.
        original_plan = self.client.plan

        def stale_plan(files):
            results = original_plan(files)
            for item in results:
                if item.get("action") == "upload":
                    item["offset"] = 0
            return results

        self.client.plan = stale_plan
        r = self.client.sync_file(p, "skew.bin")

        self.assertEqual(r.action, "uploaded")
        with open(self.store.blob_path(sha256_file(p)), "rb") as fh:
            self.assertEqual(fh.read(), data)

    def test_short_chunk_does_not_wedge_the_upload(self):
        """A short write partway through leaves the offset unaligned. Because
        chunks are addressed by byte offset rather than index, the next chunk
        still lands and the upload completes."""
        data = os.urandom(TEST_CHUNK * 3)
        p = self.write("short.bin", data)
        uid = self.upload_id("short.bin", p)

        head = data[:100]
        status, body = self.raw("PUT", f"/v1/uploads/{uid}/at/0", head, digest_header(head))
        self.assertEqual((status, body["offset"]), (200, 100))

        r = self.client.sync_file(p, "short.bin")
        self.assertEqual(r.action, "uploaded")
        with open(self.store.blob_path(sha256_file(p)), "rb") as fh:
            self.assertEqual(fh.read(), data)

    def test_premature_finalise_keeps_the_bytes_already_transferred(self):
        """Incomplete is not the same as corrupt. A short upload gets the real
        offset back, not a deleted part file."""
        data = os.urandom(TEST_CHUNK * 4)
        p = self.write("half.bin", data)
        uid = self.upload_id("half.bin", p)
        for i in (0, 1, 2):
            chunk = data[i * TEST_CHUNK:(i + 1) * TEST_CHUNK]
            self.raw("PUT", f"/v1/uploads/{uid}/at/{i * TEST_CHUNK}", chunk, digest_header(chunk))

        status, body = self.raw(
            "POST", f"/v1/uploads/{uid}/finalise",
            json.dumps({"path": "half.bin", "sha256": sha256_file(p), "size": len(data)}).encode(),
            {"Content-Type": "application/json"})

        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "incomplete")
        self.assertEqual(body["offset"], TEST_CHUNK * 3)
        self.assertEqual(os.path.getsize(self.store.part_path(uid)), TEST_CHUNK * 3,
                         "recoverable data must not be thrown away")

    def test_finalise_is_idempotent(self):
        p = self.write("once.bin", os.urandom(TEST_CHUNK + 11))
        self.assertEqual(self.client.sync_file(p, "once.bin").action, "uploaded")
        # A repeat finalise (lost response, agent restart) is a success, not a fault.
        digest = sha256_file(p)
        uid = sync_server.upload_id_for(CLIENT, "once.bin", digest)
        status, body = self.raw("POST", f"/v1/uploads/{uid}/finalise",
                                json.dumps({"path": "once.bin", "sha256": digest,
                                            "size": os.path.getsize(p)}).encode(),
                                {"Content-Type": "application/json"})
        self.assertEqual((status, body["status"]), (200, "committed"))

    def test_offset_probe_reports_what_the_server_holds(self):
        data = os.urandom(TEST_CHUNK * 2)
        p = self.write("probe.bin", data)
        uid = self.upload_id("probe.bin", p)
        self.assertEqual(self.client.offset(uid), 0)

        chunk = data[:TEST_CHUNK]
        self.raw("PUT", f"/v1/uploads/{uid}/at/0", chunk, digest_header(chunk))
        self.assertEqual(self.client.offset(uid), TEST_CHUNK)


class TestBandwidth(Base):
    def test_unchanged_file_transfers_no_file_bytes(self):
        """Change detection: a second pass over an unchanged file is a single
        small control request and zero file bytes."""
        p = self.write("static.bin", os.urandom(TEST_CHUNK * 2))
        self.assertEqual(self.client.sync_file(p, "static.bin").action, "uploaded")
        self.client.bytes_sent = 0

        r = self.client.sync_file(p, "static.bin")

        self.assertEqual(r.action, "skipped")
        self.assertEqual(r.bytes_sent, 0)
        self.assertEqual(self.client.bytes_sent, 0)

    def test_rename_transfers_no_file_bytes(self):
        """A rename is new metadata, not new content. Because the server is
        content addressed, it costs one small request."""
        data = os.urandom(TEST_CHUNK * 2)
        p = self.write("draft.bin", data)
        self.client.sync_file(p, "draft.bin")
        renamed = os.path.join(self.local, "final.bin")
        os.rename(p, renamed)
        self.client.bytes_sent = 0

        r = self.client.sync_file(renamed, "final.bin")

        self.assertEqual(r.action, "linked")
        self.assertEqual(self.client.bytes_sent, 0)
        self.assertEqual(self.store.current(CLIENT, "final.bin"),
                         hashlib.sha256(data).hexdigest())

    def test_duplicate_content_at_a_new_path_transfers_no_file_bytes(self):
        data = os.urandom(TEST_CHUNK)
        a = self.write("a.bin", data)
        self.client.sync_file(a, "a.bin")
        b = self.write("b.bin", data)
        self.client.bytes_sent = 0
        self.assertEqual(self.client.sync_file(b, "b.bin").action, "linked")
        self.assertEqual(self.client.bytes_sent, 0)


class TestSecurity(Base):
    def test_unauthenticated_requests_are_refused(self):
        status, _ = self.raw("POST", "/v1/sync/plan", b'{"files":[]}',
                             {"Content-Type": "application/json"}, token="wrong")
        self.assertEqual(status, 401)

    def test_a_non_ascii_token_is_refused_not_a_crash(self):
        """http.server decodes headers as latin-1, and hmac.compare_digest
        raises on non-ASCII str. Comparing bytes keeps this a 401."""
        status, _ = self.raw("GET", f"/v1/uploads/{'0' * 32}", token="d\u00e9mo")
        self.assertEqual(status, 401)

    def test_path_traversal_is_rejected_not_sanitised(self):
        for bad in ["../../etc/shadow", "/etc/shadow", "C:\\Windows\\win.ini",
                    "a/../../b", "ok/./x", "nul\x00byte"]:
            with self.subTest(path=bad):
                self.assertIsNone(sync_server.safe_relpath(bad))
        self.assertEqual(sync_server.safe_relpath("reports/2026/q1.csv"), "reports/2026/q1.csv")

    def test_traversal_path_is_refused_by_the_plan_endpoint(self):
        status, body = self.raw(
            "POST", "/v1/sync/plan",
            json.dumps({"files": [{"path": "../../etc/shadow", "size": 1, "sha256": "a" * 64}]}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["results"][0]["action"], "reject")
        self.assertEqual(body["results"][0]["path"], "../../etc/shadow",
                         "the caller must be able to correlate a rejection back to its request")

    def test_an_upload_session_cannot_be_repointed_at_another_path(self):
        """The session id is bound to (client, path, digest), so it cannot be
        used to publish the same bytes under a name it was not opened for."""
        p = self.write("mine.bin", os.urandom(TEST_CHUNK))
        digest = sha256_file(p)
        uid = self.upload_id("mine.bin", p)
        status, _ = self.raw("POST", f"/v1/uploads/{uid}/finalise",
                             json.dumps({"path": "someone-else.bin", "sha256": digest,
                                         "size": os.path.getsize(p)}).encode(),
                             {"Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_one_client_cannot_claim_another_clients_content(self):
        """Blobs are stored once globally, but deduplication is scoped per
        client: a digest must not become a capability for content you have
        never sent."""
        sync_server.TOKENS["second-token"] = "workstation-02"
        self.addCleanup(sync_server.TOKENS.pop, "second-token", None)

        data = os.urandom(TEST_CHUNK)
        p = self.write("secret.bin", data)
        self.assertEqual(self.client.sync_file(p, "secret.bin").action, "uploaded")
        digest = sha256_file(p)
        self.assertTrue(self.store.has_blob(digest))

        other = SyncClient(self.url, "second-token", base_delay=0.01, max_attempts=2)
        plan = other.plan([{"path": "stolen.bin", "size": len(data), "sha256": digest}])[0]

        self.assertEqual(plan["action"], "upload", "must not be handed content it never sent")
        self.assertIsNone(self.store.current("workstation-02", "stolen.bin"))

    def test_declared_size_is_not_trusted_on_the_dedupe_path(self):
        """The size in the index comes from the blob, never from the client."""
        data = os.urandom(TEST_CHUNK)
        p = self.write("real.bin", data)
        self.client.sync_file(p, "real.bin")
        digest = sha256_file(p)

        self.raw("POST", "/v1/sync/plan",
                 json.dumps({"files": [{"path": "lie.bin", "size": 999999999,
                                        "sha256": digest}]}).encode(),
                 {"Content-Type": "application/json"})

        row = self.store.db.execute(
            "SELECT size FROM paths WHERE client_id=? AND path=?", (CLIENT, "lie.bin")).fetchone()
        self.assertEqual(row[0], len(data))

    def test_oversize_file_is_refused_before_any_bytes_are_sent(self):
        status, body = self.raw(
            "POST", "/v1/sync/plan",
            json.dumps({"files": [{"path": "huge.bin",
                                   "size": sync_server.MAX_FILE_BYTES + 1,
                                   "sha256": "b" * 64}]}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["results"][0]["reason"], "too_large")

    def test_a_true_size_is_not_accepted_as_the_number_one(self):
        """isinstance(True, int) is True in Python. JSON booleans must not slip
        through a size check."""
        status, body = self.raw(
            "POST", "/v1/sync/plan",
            json.dumps({"files": [{"path": "bool.bin", "size": True, "sha256": "c" * 64}]}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["results"][0]["reason"], "invalid_item")


if __name__ == "__main__":
    unittest.main(verbosity=2)
