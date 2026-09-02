"""Resumable, integrity-checked upload of a single file.

This is the piece of the design worth showing in code: it is where change
detection, bandwidth efficiency, reliability and integrity all have to hold at
the same time, and where the interesting failure modes live.

Prototype, not production. It has no daemon, no filesystem watcher, no local
manifest and no compression. Those are described in the design document; the
point of this file is the transfer of one file version, done correctly.

Standard library only, Python 3.9+.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

READ_BUFFER = 1 << 20

# Status codes worth retrying: the request may well succeed later and repeating
# it is safe, because every endpoint in this protocol is idempotent.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Network-level failures. socket.timeout is only an alias of TimeoutError from
# 3.10, so it is listed separately to keep 3.9 behaviour the same.
NETWORK_ERRORS = (
    urllib.error.URLError, socket.timeout, TimeoutError,
    ConnectionError, http.client.HTTPException,
)


class TransientError(Exception):
    """A failure the caller should retry.

    retry_after carries the server's own instruction when it sent one
    (RFC 9110 s10.2.3). A server under load knows better than our backoff
    curve does, so we defer to it when it speaks.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """A failure that retrying will not fix."""


@dataclass
class Fingerprint:
    """Cheap identity of a file version, from one stat() call.

    This is what the agent compares against its local manifest to decide
    whether a file is even a candidate for hashing. inode is carried because
    mtime can move backwards (restores, archive extraction, `touch -d`) while a
    changed inode is good evidence the path holds a different file. It is a
    strong signal on local POSIX filesystems and a weaker one elsewhere: some
    network filesystems do not report a stable st_ino, so it is treated as one
    input to the decision rather than proof.
    """
    size: int
    mtime_ns: int
    inode: int

    @classmethod
    def of(cls, path: str) -> "Fingerprint":
        st = os.stat(path)
        return cls(st.st_size, st.st_mtime_ns, st.st_ino)


@dataclass
class Result:
    action: str                 # skipped | linked | uploaded | changed | vanished | rejected
    sha256: str | None = None
    bytes_sent: int = 0
    detail: str = ""


def sha256_fileobj(fh):
    """SHA-256 of an open binary file object.

    hashlib.file_digest (Python 3.11+) can read through the file descriptor
    directly, and hashlib releases the GIL for blocks over 2047 bytes, so
    hashing a large file does not stall the rest of the agent. The fallback
    keeps this runnable on older interpreters without changing behaviour.
    """
    if hasattr(hashlib, "file_digest"):
        return hashlib.file_digest(fh, "sha256")
    h = hashlib.sha256()
    for block in iter(lambda: fh.read(READ_BUFFER), b""):
        h.update(block)
    return h


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return sha256_fileobj(fh).hexdigest()


def content_digest_header(chunk: bytes) -> str:
    """RFC 9530 Content-Digest field: sha-256, base64, sf-binary colons."""
    return "sha-256=:" + base64.b64encode(hashlib.sha256(chunk).digest()).decode() + ":"


def _retry_after(headers) -> float | None:
    """Parse the delta-seconds form of Retry-After. The HTTP-date form is
    ignored here rather than half-implemented; falling back to our own backoff
    is safe, and a wrong date parse is worse than no parse."""
    raw = (headers or {}).get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class SyncClient:
    base_url: str
    token: str
    max_attempts: int = 5
    base_delay: float = 0.2
    max_delay: float = 30.0
    timeout: float = 30.0
    # Counts file content actually put on the wire, including bytes re-sent by a
    # retry. It does not count the small control-plane requests (plan,
    # finalise), which are a few hundred bytes each.
    bytes_sent: int = 0
    # Test hook: called with the chunk number just before it is sent. Raising
    # from it simulates a connection that dies mid-file.
    on_chunk: object = field(default=None, repr=False)

    # -- transport --------------------------------------------------------
    def _once(self, method: str, path: str, body: bytes | None, headers: dict, meter: bool = False):
        # Metered before the send, so a retried chunk is counted every time it
        # actually goes over the wire. Counting only successes would flatter the
        # numbers in exactly the situation this design is about.
        if meter and body:
            self.bytes_sent += len(body)
        req = urllib.request.Request(
            self.base_url.rstrip("/") + path, data=body, method=method,
            headers={"Authorization": f"Bearer {self.token}", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            payload = {}
            try:
                payload = json.loads(e.read() or b"{}")
            except Exception:
                pass
            if e.code in RETRYABLE_STATUS:
                raise TransientError(f"HTTP {e.code}", _retry_after(e.headers)) from e
            return e.code, payload
        except NETWORK_ERRORS as e:
            # We never learned whether the server acted. Safe to retry: every
            # endpoint here is idempotent, and RFC 9110 s9.2.2 explicitly
            # permits repeating a request whose response was not received.
            raise TransientError(str(e)) from e

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None, meter: bool = False):
        """Send with bounded retries and exponential backoff with full jitter.

        Full jitter (sleep uniformly in [0, backoff]) rather than a fixed
        backoff: when a flaky link recovers, every queued agent would otherwise
        retry in lockstep and knock the server over again.
        """
        headers = headers or {}
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                return self._once(method, path, body, headers, meter)
            except TransientError as e:
                last = e
                if attempt == self.max_attempts - 1:
                    break
                if e.retry_after is not None:
                    time.sleep(min(self.max_delay, e.retry_after))
                    continue
                backoff = min(self.max_delay, self.base_delay * (2 ** attempt))
                time.sleep(random.uniform(0, backoff))
        raise TransientError(f"gave up after {self.max_attempts} attempts: {last}")

    # -- protocol ---------------------------------------------------------
    def plan(self, files: list) -> list:
        """One round trip that answers, for a batch of files: do you already
        have this, do you have the content under another name, and if you need
        it, how much of it do you already hold?"""
        body = json.dumps({"files": files}).encode()
        status, payload = self.request(
            "POST", "/v1/sync/plan", body, {"Content-Type": "application/json"}
        )
        if status != 200:
            raise PermanentError(f"plan failed: {status} {payload}")
        return payload["results"]

    def offset(self, upload_id: str) -> int:
        """Ask the server how much of this upload it holds. The plan response
        already carries this, so it is only needed to re-check an upload the
        client is part way through."""
        status, payload = self.request("GET", f"/v1/uploads/{upload_id}")
        if status != 200:
            raise PermanentError(f"offset probe failed: {status} {payload}")
        return payload["offset"]

    def sync_file(self, local_path: str, remote_path: str) -> Result:
        # 1. Capture the version identity BEFORE reading a single byte. Both the
        #    cheap fingerprint and the content hash describe the same instant.
        try:
            before = Fingerprint.of(local_path)
            digest = sha256_file(local_path)
        except OSError as e:
            # The file went away, or is not a readable regular file. This is
            # normal in a directory that is being worked in; the next
            # reconciliation pass will settle it.
            return Result("vanished", None, 0, f"could not read {local_path}: {e}")

        plan = self.plan([{"path": remote_path, "size": before.size, "sha256": digest}])[0]
        action = plan.get("action")

        if action == "skip":
            return Result("skipped", digest, 0, "server already holds this version")
        if action == "linked":
            # Content already on the server under another path. A rename, a copy
            # or a revert costs one small request and zero file bytes.
            return Result("linked", digest, 0, "content already present, path published")
        if action != "upload":
            return Result("rejected", digest, 0, plan.get("reason", "server declined"))

        upload_id = plan["upload_id"]
        chunk_size = plan["chunk_size"]
        offset = plan["offset"]            # the server is the authority on this
        start_bytes = self.bytes_sent

        # 2. Send only what the server does not already hold. On a resume after
        #    a dropped connection this skips straight to the tail of the file.
        #    The loop seeks to the server's offset every pass rather than
        #    tracking a position of its own, so the two can never drift apart.
        stalls = 0
        try:
            with open(local_path, "rb") as fh:
                while True:
                    fh.seek(offset)
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    if callable(self.on_chunk):
                        self.on_chunk(offset // chunk_size)  # test hook: fail mid-file
                    new_offset = self._put_chunk(upload_id, offset, chunk)
                    # A 409 can legitimately send us backwards to resynchronise,
                    # so a non-advancing answer is tolerated a few times. What is
                    # not tolerated is spinning on it forever.
                    stalls = 0 if new_offset > offset else stalls + 1
                    if stalls > 3:
                        raise PermanentError(
                            f"server offset stuck at {new_offset}; giving up")
                    offset = new_offset
        except OSError as e:
            return Result("vanished", digest, self.bytes_sent - start_bytes,
                          f"stopped reading {local_path}: {e}")

        # 3. Before finalising, re-check the cheap fingerprint. If the file moved
        #    under us we abandon this version rather than publish a torn read.
        #    This is an optimisation, not the guarantee: it saves a pointless
        #    finalise round trip. The guarantee is step 4.
        try:
            after = Fingerprint.of(local_path)
        except OSError as e:
            return Result("vanished", digest, self.bytes_sent - start_bytes,
                          f"file disappeared before finalise: {e}")
        if after != before:
            return Result("changed", digest, self.bytes_sent - start_bytes,
                          "file changed during transfer; will resync the new version")

        # 4. Ask the server to verify and commit. The server hashes what it
        #    actually received and compares it with the digest we committed to
        #    in step 1. We cannot assert success; we can only ask.
        status, payload = self.request(
            "POST", f"/v1/uploads/{upload_id}/finalise",
            json.dumps({"path": remote_path, "sha256": digest, "size": before.size}).encode(),
            {"Content-Type": "application/json"},
        )
        sent = self.bytes_sent - start_bytes
        if status == 200 and payload.get("status") == "committed":
            return Result("uploaded", digest, sent, "verified and committed by server")
        if status == 409 and payload.get("error") == "digest_mismatch":
            return Result("changed", digest, sent,
                          "server rejected: received bytes did not match the declared digest")
        raise PermanentError(f"finalise failed: {status} {payload}")

    def _put_chunk(self, upload_id: str, offset: int, chunk: bytes) -> int:
        """Upload one chunk, returning the server's new offset.

        Network-level retries happen a layer down in request(). What is handled
        here is the two answers that mean "try again with different bytes": a
        chunk that arrived corrupted (422, send it again) and the server being
        at a different offset than we thought (409, take its answer and let the
        caller re-read from there).
        """
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Digest": content_digest_header(chunk),
        }
        for _ in range(3):
            status, payload = self.request(
                "PUT", f"/v1/uploads/{upload_id}/at/{offset}", chunk, headers, meter=True
            )
            if status == 200:
                return payload["offset"]
            if status == 409:
                return payload["offset"]      # caller reseeks; no local position to fix up
            if status == 422:
                continue                      # corrupted in flight, send it again
            raise PermanentError(f"chunk at {offset} rejected: {status} {payload}")
        raise TransientError(f"chunk at {offset} did not stick after 3 attempts")
