"""Minimal collection server for the resumable upload protocol.

Prototype, not production. Standard library only, so it runs with
`python3 sync_server.py` and nothing to install. A real deployment would sit
behind a hardened HTTP server with TLS termination, real client
authentication, quotas and structured audit logging.

The one idea worth reading the code for: the server's authoritative state for
an in-flight upload is the *length of the partial file on disk*. There is no
session table that can drift out of step with the bytes it describes, so a
server restart mid-upload cannot corrupt a resume. Recovering the offset is a
stat() call, and chunks are addressed by the byte offset they start at rather
than by index, so there is nothing to keep in step in the first place.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 1 MiB. Chunk size bounds the work lost when a connection drops, and trades
# that against per-request overhead. It is advertised by the server rather than
# hard-coded in the client so it can be tuned per link without redeploying agents.
CHUNK_SIZE = 1 << 20
MAX_FILE_BYTES = 8 << 30       # refuse absurd files, and stop accepting bytes past it
MAX_PLAN_ITEMS = 500           # bound the work one request can cause
MAX_PATH_LEN = 1024

# Prototype credential. Real deployments use mTLS or a short-lived token issued
# against a device credential held in the OS keystore. The point that matters
# here is that every request is attributed to a client id, because authorisation
# is per-client namespace.
TOKENS = {"demo-token": "workstation-01"}

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_DIGEST_HDR = re.compile(r"\Asha-256=:([A-Za-z0-9+/=]+):\Z")


def _sha256_fileobj(fh):
    """hashlib.file_digest where available (3.11+), streaming read otherwise."""
    if hasattr(hashlib, "file_digest"):
        return hashlib.file_digest(fh, "sha256")
    h = hashlib.sha256()
    for block in iter(lambda: fh.read(1 << 20), b""):
        h.update(block)
    return h


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
class Store:
    """Content-addressed blob store plus a path index.

    Client-supplied paths are only ever stored as database strings; they are
    never used to build a filesystem path. Blobs live at blobs/<sha256> and
    partial uploads at uploads/<upload_id>.part, both server-generated hex.
    That removes path traversal as a filesystem-write primitive entirely,
    rather than trying to filter it out of the input.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.blobs = os.path.join(self.root, "blobs")
        self.parts = os.path.join(self.root, "uploads")
        for d in (self.blobs, self.parts):
            os.makedirs(d, mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self._upload_locks: dict[str, threading.Lock] = {}
        self.db = sqlite3.connect(os.path.join(self.root, "index.db"), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        # synchronous=FULL: the index must not survive a power failure that ate
        # the blob it points at. See https://www.sqlite.org/atomiccommit.html
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS paths(
                   client_id TEXT NOT NULL,
                   path      TEXT NOT NULL,
                   sha256    TEXT NOT NULL,
                   size      INTEGER NOT NULL,
                   PRIMARY KEY (client_id, path))"""
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def blob_path(self, digest: str) -> str:
        return os.path.join(self.blobs, digest)

    def part_path(self, upload_id: str) -> str:
        return os.path.join(self.parts, upload_id + ".part")

    def upload_lock(self, upload_id: str) -> threading.Lock:
        """One lock per upload, so appending to a part file is read-check-act
        atomic. Without it two concurrent PUTs at the same offset both observe
        the old length and both append, and the offset the server reports stops
        describing the bytes it holds."""
        with self._lock:
            return self._upload_locks.setdefault(upload_id, threading.Lock())

    def has_blob(self, digest: str) -> bool:
        return os.path.exists(self.blob_path(digest))

    def blob_size(self, digest: str) -> int:
        return os.path.getsize(self.blob_path(digest))

    def current(self, client_id: str, path: str) -> str | None:
        row = self.db.execute(
            "SELECT sha256 FROM paths WHERE client_id=? AND path=?", (client_id, path)
        ).fetchone()
        return row[0] if row else None

    def client_holds(self, client_id: str, digest: str) -> bool:
        """Has THIS client already published this content under some path?

        Deduplication is scoped per client on purpose. Blobs are stored once
        globally, so a cross-client check would be a bigger bandwidth win, but
        it would also turn a digest into a capability: anyone who could obtain a
        hash could have it linked into their own namespace and learn that
        another client holds that exact file. Cross-tenant dedup needs a
        proof-of-possession step (challenge the client to hash a random byte
        range) before it is safe, and that was not worth building here.
        """
        return self.db.execute(
            "SELECT 1 FROM paths WHERE client_id=? AND sha256=? LIMIT 1", (client_id, digest)
        ).fetchone() is not None

    def link(self, client_id: str, path: str, digest: str, size: int) -> None:
        """Publish path -> blob. Atomic and idempotent."""
        with self._lock:
            self.db.execute(
                "INSERT INTO paths(client_id,path,sha256,size) VALUES(?,?,?,?) "
                "ON CONFLICT(client_id,path) DO UPDATE SET "
                "sha256=excluded.sha256, size=excluded.size",
                (client_id, path, digest, size),
            )
            self.db.commit()


def upload_id_for(client_id: str, path: str, digest: str) -> str:
    """Deterministic session id, derived from the identity of the file version.

    Handing these out at random would mean a client that lost its local state
    could not find its half-finished upload, and the server would accumulate
    orphaned sessions. Deriving it means resume is idempotent by construction.
    Because the client id is part of the input, the id is also an unguessable
    128-bit capability for one client's upload of one file version.
    """
    h = hashlib.sha256(b"\x00".join([client_id.encode(), path.encode(), digest.encode()]))
    return h.hexdigest()[:32]


def safe_relpath(path: str) -> str | None:
    """Validate a client-supplied path. Rejects rather than sanitises.

    Sanitising invites the caller to assume something was salvaged, and every
    sanitiser eventually meets an encoding it did not expect. These strings are
    served back to operators and other clients, so they are validated at the
    trust boundary and refused if they are not what we said we accept.
    """
    if not path or len(path) > MAX_PATH_LEN:
        return None
    if path[0] in "/\\" or re.match(r"\A[A-Za-z]:", path):
        return None
    if any(ord(c) < 0x20 for c in path):
        return None
    parts = path.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return "/".join(parts)


def _fsync_dir(path: str) -> None:
    """Best effort durability for a rename. POSIX only; Windows has no
    equivalent and simply skips it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "sync-collector/0.1"
    protocol_version = "HTTP/1.1"
    store: Store = None  # type: ignore[assignment]
    _read_body = False   # reset per request; the instance is reused per connection

    def log_message(self, fmt, *args):
        if os.environ.get("SYNC_SERVER_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _client(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        # Compare as bytes: http.server decodes headers as latin-1, and
        # hmac.compare_digest raises TypeError on non-ASCII str operands.
        token = auth[7:].encode("utf-8", "surrogateescape")
        matched = None
        for known, client in TOKENS.items():
            # No early return, so the loop takes the same time whichever entry
            # matches. compare_digest itself is constant time per comparison.
            if hmac.compare_digest(token, known.encode()):
                matched = client
        return matched

    def _json(self, code: int, body: dict) -> None:
        # If we are answering without having consumed the request body, the
        # unread bytes would be parsed as the next request on a keep-alive
        # connection. Close instead of desynchronising, and say so (RFC 9112).
        closing = not self._read_body and self._declared_length() > 0
        if closing:
            self.close_connection = True
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if closing:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _declared_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            return 0

    def _body(self, limit: int) -> bytes | None:
        """Read the request body, or None if it is larger than we will accept.

        Transfer-Encoding: chunked is not handled; this prototype only speaks to
        a client that sends Content-Length.
        """
        n = self._declared_length()
        if n > limit:
            return None
        if n <= 0:
            return b""
        data = self.rfile.read(n)
        self._read_body = True
        return data

    def _offset(self, upload_id: str) -> int:
        """Bytes already accepted for this upload, read from the part file."""
        try:
            return os.path.getsize(self.store.part_path(upload_id))
        except FileNotFoundError:
            return 0

    # -- routing ----------------------------------------------------------
    def do_POST(self):
        self._read_body = False
        client = self._client()
        if not client:
            return self._json(401, {"error": "unauthenticated"})
        if self.path == "/v1/sync/plan":
            return self._plan(client)
        m = re.match(r"\A/v1/uploads/([0-9a-f]{32})/finalise\Z", self.path)
        if m:
            return self._finalise(client, m.group(1))
        self._json(404, {"error": "not_found"})

    def do_PUT(self):
        self._read_body = False
        if not self._client():
            return self._json(401, {"error": "unauthenticated"})
        # Chunks are addressed by the byte offset they start at, not by index.
        # An index has to be interpreted against a chunk size that both sides
        # must agree on; an offset means exactly one thing.
        m = re.match(r"\A/v1/uploads/([0-9a-f]{32})/at/(\d{1,13})\Z", self.path)
        if not m:
            return self._json(404, {"error": "not_found"})
        self._chunk(m.group(1), int(m.group(2)))

    def do_GET(self):
        self._read_body = False
        if not self._client():
            return self._json(401, {"error": "unauthenticated"})
        m = re.match(r"\A/v1/uploads/([0-9a-f]{32})\Z", self.path)
        if not m:
            return self._json(404, {"error": "not_found"})
        self._json(200, {"offset": self._offset(m.group(1)), "chunk_size": CHUNK_SIZE})

    # -- handlers ---------------------------------------------------------
    def _plan(self, client: str):
        raw = self._body(1 << 20)
        if raw is None:
            return self._json(413, {"error": "body_too_large"})
        try:
            items = json.loads(raw)["files"]
        except Exception:
            return self._json(400, {"error": "bad_request"})
        if not isinstance(items, list) or len(items) > MAX_PLAN_ITEMS:
            return self._json(400, {"error": "bad_request"})

        out = []
        for item in items:
            path = safe_relpath(str(item.get("path", "")))
            digest = str(item.get("sha256", "")).lower()
            size = item.get("size")
            # isinstance(True, int) is True in Python, so bools are excluded
            # explicitly rather than being silently accepted as size 1.
            valid_size = isinstance(size, int) and not isinstance(size, bool)
            if path is None or not _HEX64.match(digest) or not valid_size:
                out.append({"path": item.get("path"), "action": "reject", "reason": "invalid_item"})
                continue
            if size < 0 or size > MAX_FILE_BYTES:
                out.append({"path": path, "action": "reject", "reason": "too_large"})
                continue
            if self.store.current(client, path) == digest:
                out.append({"path": path, "action": "skip"})          # already have it, here
                continue
            if self.store.client_holds(client, digest) and self.store.has_blob(digest):
                # This client already has the content under another path: a
                # rename, a copy, or a revert. Publish the mapping, send zero
                # file bytes. The size comes from the blob, never from the
                # client, so the index cannot be made to lie about it.
                self.store.link(client, path, digest, self.store.blob_size(digest))
                out.append({"path": path, "action": "linked"})
                continue
            uid = upload_id_for(client, path, digest)
            out.append({
                "path": path,
                "action": "upload",
                "upload_id": uid,
                "chunk_size": CHUNK_SIZE,
                "offset": self._offset(uid),                          # resume point
            })
        self._json(200, {"results": out})

    def _chunk(self, upload_id: str, offset: int):
        body = self._body(CHUNK_SIZE + (1 << 16))
        if body is None:
            return self._json(413, {"error": "chunk_too_large"})
        if len(body) > CHUNK_SIZE:
            return self._json(400, {"error": "oversize_chunk"})

        # Per-chunk integrity, checked before the bytes are allowed anywhere
        # near the partial file. RFC 9530 Content-Digest, sha-256, base64
        # sf-binary. This catches corruption early rather than at finalise,
        # so a bad chunk costs one chunk of bandwidth, not a whole file.
        m = _DIGEST_HDR.match(self.headers.get("Content-Digest", "").strip())
        if not m:
            return self._json(400, {"error": "missing_content_digest"})
        try:
            expected = base64.b64decode(m.group(1), validate=True)
        except Exception:
            return self._json(400, {"error": "bad_content_digest"})
        if not hmac.compare_digest(hashlib.sha256(body).digest(), expected):
            return self._json(422, {"error": "chunk_digest_mismatch", "offset": self._offset(upload_id)})

        with self.store.upload_lock(upload_id):
            have = self._offset(upload_id)

            if offset + len(body) <= have:
                # Full replay of bytes we already hold. PUT is idempotent
                # (RFC 9110 s9.2.2), so this is a success: it is exactly what a
                # client does when our response to its first attempt was lost.
                return self._json(200, {"offset": have})
            if offset != have:
                # A gap, or an overlap we will not try to be clever about.
                return self._json(409, {"error": "offset_mismatch", "offset": have})
            if have + len(body) > MAX_FILE_BYTES:
                # The declared size in the plan is not evidence, so the ceiling
                # is enforced here too, where the bytes actually arrive.
                return self._json(413, {"error": "too_large", "offset": have})

            with open(self.store.part_path(upload_id), "ab") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())  # the offset we report must survive power loss
            # Report the observed length, not have + len(body). The file is the
            # source of truth; anything else is a prediction about it.
            self._json(200, {"offset": self._offset(upload_id)})

    def _finalise(self, client: str, upload_id: str):
        raw = self._body(1 << 16)
        if raw is None:
            return self._json(413, {"error": "body_too_large"})
        try:
            body = json.loads(raw)
            path = safe_relpath(str(body["path"]))
            digest = str(body["sha256"]).lower()
            size = int(body["size"])
        except Exception:
            return self._json(400, {"error": "bad_request"})
        if path is None or not _HEX64.match(digest) or size < 0 or size > MAX_FILE_BYTES:
            return self._json(400, {"error": "bad_request"})
        # The session id is bound to (client, path, content hash), so a client
        # cannot finalise someone else's upload or relabel its own.
        if upload_id != upload_id_for(client, path, digest):
            return self._json(403, {"error": "upload_id_mismatch"})

        if self.store.client_holds(client, digest) and self.store.has_blob(digest):
            # Already committed by this client, possibly by a retry whose
            # response was lost. Same scoping as the plan endpoint, and the size
            # is taken from the blob rather than from the request.
            self.store.link(client, path, digest, self.store.blob_size(digest))
            return self._json(200, {"status": "committed", "sha256": digest})

        with self.store.upload_lock(upload_id):
            part = self.store.part_path(upload_id)
            if not os.path.exists(part):
                if size != 0:
                    return self._json(404, {"error": "no_such_upload"})
                open(part, "wb").close()  # a zero-byte file uploads zero chunks

            actual_size = os.path.getsize(part)
            if actual_size < size:
                # Not corrupt, just not finished. Keep the bytes and tell the
                # client where to resume; deleting here would throw away
                # perfectly good transferred data.
                return self._json(409, {"error": "incomplete", "offset": actual_size})

            with open(part, "rb") as fh:
                actual = _sha256_fileobj(fh).hexdigest()

            # THE integrity gate. The server independently hashes what it
            # received and compares it against the digest the client committed
            # to *before* it started reading the file. A file that changed under
            # the client mid-transfer yields a torn byte stream, so this
            # comparison fails and nothing is published. The client does not get
            # to assert success.
            if actual_size != size or actual != digest:
                try:
                    os.unlink(part)
                except FileNotFoundError:
                    pass
                return self._json(409, {
                    "error": "digest_mismatch",
                    "expected": digest, "actual": actual,
                    "expected_size": size, "actual_size": actual_size,
                })

            # Atomic publication. The blob appears under its final name in one
            # step (POSIX rename), the directory entry is flushed, and only then
            # is the path index row committed. A crash between the two leaves an
            # unreferenced blob, which is garbage to be collected, not
            # corruption. The reverse order would leave the index pointing at a
            # file that does not exist.
            os.replace(part, self.store.blob_path(digest))
            _fsync_dir(self.store.blobs)
            self.store.link(client, path, digest, actual_size)
            self._json(200, {"status": "committed", "sha256": digest})


def serve(root: str, port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"store": Store(root)})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "./server-data"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    httpd = serve(root, port)
    print(f"collection server on http://127.0.0.1:{httpd.server_address[1]}  root={root}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        httpd.RequestHandlerClass.store.close()
