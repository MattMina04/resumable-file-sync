# Resumable file sync

A design and a working prototype for a utility that watches a set of directories and syncs them to a remote collection server over a bandwidth-constrained, unreliable link.

The problem has four hard parts, and they interact:

| Requirement | How this design meets it |
|---|---|
| **Change detection** | Filesystem events for latency, a periodic `stat()` reconciliation scan for correctness, a local SQLite manifest, and SHA-256 computed only when metadata says something moved. |
| **Bandwidth** | Only changed files are candidates. A batched pre-flight request lets the server answer *skip*, *link* (it already has the content under another name) or *upload from byte N*. Transfers are chunked and resumable. |
| **Reliability** | Every endpoint is idempotent, the server is the sole authority on how much it has received, retries use exponential backoff with full jitter, and client state is committed atomically. |
| **Integrity** | Per-chunk SHA-256 on arrival, then a whole-file SHA-256 computed *by the server* and matched against the digest the client declared before it started reading. Publication is atomic and happens only after that check passes. |

**[Design document (PDF)](docs/Efficient-Data-Synchronisation-Utility.pdf)**: two pages, the full architecture, trade-offs and honest limitations. Also available as [markdown](docs/design.md) and [.docx](docs/Efficient-Data-Synchronisation-Utility.docx).

---

## The prototype

`prototype/` implements the piece worth showing in code: the transfer of a single file version, done correctly. Standard library only, no dependencies, Python 3.9 or newer.

```bash
cd prototype
python3 demo.py                       # end-to-end walkthrough
python3 -m unittest discover . -v     # 26 tests
```

`demo.py` output:

```
1. First sync of a new file
   uploaded  verified and committed by server
   sha256 2266c26257548fc4...   file bytes sent: 524,288

2. Nothing changed, sync again
   skipped   server already holds this version
   file bytes sent: 0   (one small plan request, no content)

3. File renamed (same content, new path)
   linked    content already present, path published
   file bytes sent: 0   (one small plan request, no content)

4. New file, connection dies after 3 of 8 chunks, then resumes
   interrupted: link went down   file bytes sent so far: 196,608
   uploaded  verified and committed by server
   resumed and sent only: 327,680 bytes (a restart would have cost 524,288)

5. File edited mid-upload, with its mtime restored to hide the change
   changed   server rejected: received bytes did not match the declared digest
   published? False   (the torn read is discarded)
```

### Files

| File | What it is |
|---|---|
| `sync_client.py` | Client. Hashing, chunking, retry with backoff and jitter, resume, the pre-finalise metadata check. |
| `sync_server.py` | Minimal collection server. Chunk verification, resumable offset, whole-file verification, atomic publish. |
| `test_sync.py` | 26 tests, grouped by requirement: integrity, the modification race, reliability, bandwidth, security. |
| `demo.py` | The walkthrough above. |

### The protocol

```
POST /v1/sync/plan                     [{path, size, sha256}, ...]
     -> per file: skip | linked | upload {upload_id, chunk_size, offset}
PUT  /v1/uploads/{id}/at/{byte-offset} chunk bytes + Content-Digest: sha-256=:...:
     -> 200 {offset} | 409 {offset} | 422 chunk digest mismatch
GET  /v1/uploads/{id}                  -> {offset, chunk_size}
POST /v1/uploads/{id}/finalise         {path, sha256, size}
     -> 200 committed | 409 incomplete {offset} | 409 digest_mismatch
```

Chunks are addressed by the byte offset they start at rather than by index. An index has to be interpreted against a chunk size both sides must agree on; an offset means exactly one thing, and it is the same number the server reports for resume.

### Two ideas in the code worth a look

**The server's resume offset is the length of the partial file.** There is no session table that can fall out of step with the bytes it describes, so a server restart mid-upload cannot corrupt a resume. Recovering the offset is a `stat()`:

```python
def _offset(self, upload_id: str) -> int:
    try:
        return os.path.getsize(self.store.part_path(upload_id))
    except FileNotFoundError:
        return 0
```

**The client cannot assert that a sync succeeded, only ask.** The client declares a digest before it reads a byte; the server hashes what it actually received and compares. A file edited mid-transfer produces a torn byte stream, which cannot match, so nothing is published:

```python
if actual_size != size or actual != digest:
    try:
        os.unlink(part)
    except FileNotFoundError:
        pass
    return self._json(409, {"error": "digest_mismatch", ...})

os.replace(part, self.store.blob_path(digest))   # atomic
_fsync_dir(self.store.blobs)
self.store.link(client, path, digest, actual_size)
```

`test_file_modified_mid_upload_is_not_published` edits a file mid-upload *and restores its mtime* so the client's cheap metadata check is fooled into finalising. The server still refuses it. That test exists because metadata checks are an optimisation and the digest comparison is the guarantee, and it is worth proving rather than asserting.

### What this prototype deliberately is not

It is a prototype, not production code. It does not implement the filesystem watcher, the reconciliation scan, the local manifest, deletions and tombstones, per-chunk compression, garbage collection of abandoned partial uploads, an abort endpoint, or `Transfer-Encoding: chunked` on the server. Those are described in the design document or are plumbing; leaving them out keeps the reliability and integrity story readable. The server is `http.server` with a hard-coded bearer token so that it runs with nothing installed. A real deployment needs TLS, real client authentication, quotas, and structured audit logging.

Two deliberate scoping calls worth naming, because they look like omissions and are not:

- **Deduplication is scoped per client.** Blobs are stored once globally, so a cross-client check would save more bandwidth, but it would turn a digest into a capability: anyone who obtained a hash could have it linked into their own namespace and learn that another client holds that file. Cross-tenant dedup needs a proof-of-possession step first.
- **The size recorded in the index always comes from the blob, never from the request.** The declared size in a plan request is a hint used to reject absurd files early; it is never treated as evidence.

## Standards this leans on

- SHA-256, NIST FIPS 180-4
- `PUT` idempotency and `Retry-After`, RFC 9110 §9.2.2 and §10.2.3
- `Content-Digest`, RFC 9530
- Event-loss behaviour: `inotify(7)` (`IN_Q_OVERFLOW`), Microsoft `ReadDirectoryChangesW` (buffer overflow, `ERROR_NOTIFY_ENUM_DIR`)
- Atomic commit under power loss: SQLite *Atomic Commit*; POSIX `rename()`
- The resumable-upload shape follows the same pattern as the IETF `draft-ietf-httpbis-resumable-upload` work

Chunk size, backoff parameters and the quiescence window are engineering choices, not requirements, and would be tuned against real link and workload data.

## Licence

MIT. See [LICENSE](LICENSE).
