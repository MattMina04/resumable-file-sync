# Resumable file sync

A design and a working prototype for a utility that watches a set of directories and syncs them to a remote collection server over a limited, unreliable link.

| Requirement | How this design meets it |
|---|---|
| **Change detection** | Filesystem events for latency, a periodic `stat()` scan for correctness, a local SQLite record of what the server has confirmed, and SHA-256 computed only when metadata says something moved. |
| **Bandwidth** | Only changed files are candidates. A pre-flight request lets the server answer *skip*, *I already have that content under another name*, or *upload from byte N*. Transfers are chunked and resumable. |
| **Reliability** | Every endpoint is idempotent, the server is the only authority on how much it has received, requests retry with exponential backoff and full jitter, and a file that still will not send is reported with the reason rather than retried forever. |
| **Integrity** | Per-chunk SHA-256 on arrival, then a whole-file SHA-256 computed *by the server* and matched against the digest the client declared before it started reading. Publication is atomic and only happens after that check passes. |

**[Design document (PDF)](docs/Data-Synchronisation-Utility.pdf)**, two pages. Also available as [markdown](docs/design.md) and [.docx](docs/Data-Synchronisation-Utility.docx).

---

## The prototype

`prototype/` implements the part worth showing in code: moving one file version correctly, and the agent loop around it. Standard library only, no dependencies, Python 3.9 or newer.

```bash
cd prototype
python3 demo.py                       # end-to-end walkthrough
python3 -m unittest discover . -v     # 35 tests
```

`demo.py` output:

```
1. First sync of a new file
   uploaded  verified and committed by server
   sha256 8f94e98e10ce236f...   file bytes sent: 524,288

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

6. A batch of files, with the link dropping once, run by the agent
   agent: batch-1.bin: attempt 1 of 3 failed (connection reset by peer), retrying in 0.3s
   agent: batch-0.bin: uploaded, verified and committed by server
   agent: batch-2.bin: uploaded, verified and committed by server
   agent: batch-1.bin: uploaded, verified and committed by server (attempt 2)
   uploaded  batch-0.bin    attempt 1   file bytes sent: 196,608
   uploaded  batch-1.bin    attempt 2   file bytes sent: 196,608
   uploaded  batch-2.bin    attempt 1   file bytes sent: 196,608
```

Worth noting in scenario 6: the file that failed and was retried sent 196,608 bytes in total, the same as the two that went first time. The retry resumed, it did not start again.

### Files

| File | What it is |
|---|---|
| `sync_client.py` | Client. Hashing, chunking, retry with backoff and jitter, resume, the pre-finalise metadata check. |
| `sync_server.py` | Minimal collection server. Chunk verification, resumable offset, whole-file verification, atomic publish. |
| `sync_agent.py` | The agent loop. An asyncio queue of files, a small number of workers, per-file retries, and a plain error when one will not go. |
| `test_sync.py` | 26 tests: integrity, the modification race, reliability, bandwidth, security. |
| `test_agent.py` | 9 tests: retry and resume, permanent failures, one bad file not stopping the batch, workers overlapping. |
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

Chunks are addressed by the byte offset they start at rather than by index. An index has to be interpreted against a chunk size both sides must agree on. An offset means exactly one thing, and it is the same number the server reports for resume.

### Two ideas in the code worth a look

**The server's resume offset is the length of the partial file.** There is no session table that can fall out of step with the bytes it describes, so a server restart mid-upload cannot corrupt a resume. Recovering the offset is a `stat()`:

```python
def _offset(self, upload_id: str) -> int:
    try:
        return os.path.getsize(self.store.part_path(upload_id))
    except FileNotFoundError:
        return 0
```

**The client cannot assert that a sync succeeded, only ask.** It declares a digest before it reads a byte, and the server hashes what it actually received and compares. A file edited mid-transfer gives a torn byte stream, which cannot match, so nothing is published:

```python
if actual_size != size or actual != digest:
    ...
    return self._json(409, {"error": "digest_mismatch", ...})

os.replace(part, self.store.blob_path(digest))   # atomic
_fsync_dir(self.store.blobs)
self.store.link(client, path, digest, actual_size)
```

`test_file_modified_mid_upload_is_not_published` edits a file mid-upload *and restores its mtime*, so the client's cheap metadata check is fooled into finalising. The server still refuses it. The metadata check is an optimisation, the digest comparison is the guarantee, and that is worth proving rather than asserting.

### What this prototype deliberately is not

It is a prototype, not production code. It does not implement the filesystem watcher, the periodic scan, the local SQLite manifest, deletions and tombstones, per-chunk compression, garbage collection of abandoned uploads, or an abort endpoint. Those are described in the design document. The server is `http.server` with a hard-coded bearer token so that it runs with nothing installed. A real deployment needs TLS, real client authentication, quotas and audit logging.

Two scoping decisions worth naming, because they look like omissions and are not:

- **Deduplication is scoped per client.** Blobs are stored once globally, so a cross-client check would save more bandwidth, but it would turn a digest into a capability: anyone who obtained a hash could have it linked into their own namespace and learn that another client holds that file. Cross-tenant dedup needs a proof-of-possession step first.
- **The size recorded in the index always comes from the blob, never from the request.** The declared size in a plan request is a hint used to reject absurd files early. It is never treated as evidence.

## Standards this leans on

- SHA-256, NIST FIPS 180-4
- `PUT` idempotency and `Retry-After`, RFC 9110
- `Content-Digest`, RFC 9530
- Event loss: `inotify(7)` (`IN_Q_OVERFLOW`), Microsoft `ReadDirectoryChangesW` (buffer overflow, `ERROR_NOTIFY_ENUM_DIR`)
- Atomic commit under power loss: SQLite *Atomic Commit*, POSIX `rename()`
- The resumable upload shape follows the same pattern as the IETF `draft-ietf-httpbis-resumable-upload` work

Chunk size, backoff parameters and the scan interval are engineering choices, not requirements, and would be tuned against a real link and workload.

## Licence

MIT. See [LICENSE](LICENSE).
