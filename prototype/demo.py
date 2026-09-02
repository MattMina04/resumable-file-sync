"""End-to-end demonstration. Run it with: python3 demo.py

Starts a throwaway collection server, then walks through the six scenarios
the design is built around, printing the file bytes actually put on the wire.
The small control-plane requests (plan, finalise) are a few hundred bytes each
and are not counted; the numbers below are file content.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading

import sync_server
from sync_agent import Job, sync_all
from sync_client import SyncClient, TransientError, sha256_file

CHUNK = 64 * 1024
sync_server.CHUNK_SIZE = CHUNK


def line(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def main() -> None:
    root = tempfile.mkdtemp(prefix="sync-demo-")
    local = os.path.join(root, "watched")
    os.makedirs(local)
    httpd = sync_server.serve(os.path.join(root, "server"), 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    store = httpd.RequestHandlerClass.store
    client = SyncClient(f"http://127.0.0.1:{httpd.server_address[1]}", "demo-token", base_delay=0.01)

    path = os.path.join(local, "capture.bin")
    payload = os.urandom(CHUNK * 8)
    with open(path, "wb") as fh:
        fh.write(payload)
    print(f"watching {local}   chunk size {CHUNK // 1024} KiB   file {len(payload) // 1024} KiB")

    line("1. First sync of a new file")
    before = client.bytes_sent
    r = client.sync_file(path, "capture.bin")
    print(f"   {r.action:9} {r.detail}")
    print(f"   sha256 {r.sha256[:16]}...   file bytes sent: {client.bytes_sent - before:,}")

    line("2. Nothing changed, sync again")
    before = client.bytes_sent
    r = client.sync_file(path, "capture.bin")
    print(f"   {r.action:9} {r.detail}")
    print(f"   file bytes sent: {client.bytes_sent - before:,}   (one small plan request, no content)")

    line("3. File renamed (same content, new path)")
    renamed = os.path.join(local, "capture-2026-08.bin")
    os.rename(path, renamed)
    before = client.bytes_sent
    r = client.sync_file(renamed, "capture-2026-08.bin")
    print(f"   {r.action:9} {r.detail}")
    print(f"   file bytes sent: {client.bytes_sent - before:,}   (one small plan request, no content)")

    line("4. New file, connection dies after 3 of 8 chunks, then resumes")
    big = os.path.join(local, "archive.bin")
    with open(big, "wb") as fh:
        fh.write(os.urandom(CHUNK * 8))

    class Dropped(Exception):
        pass

    def die(index):
        if index == 3:
            raise Dropped("link went down")

    client.on_chunk = die
    before = client.bytes_sent
    try:
        client.sync_file(big, "archive.bin")
    except Dropped as e:
        print(f"   interrupted: {e}   file bytes sent so far: {client.bytes_sent - before:,}")

    client.on_chunk = None
    resume_from = client.bytes_sent
    r = client.sync_file(big, "archive.bin")
    print(f"   {r.action:9} {r.detail}")
    print(f"   resumed and sent only: {client.bytes_sent - resume_from:,} bytes "
          f"(a restart would have cost {CHUNK * 8:,})")

    line("5. File edited mid-upload, with its mtime restored to hide the change")
    live = os.path.join(local, "live.log")
    with open(live, "wb") as fh:
        fh.write(b"A" * (CHUNK * 4))
    st = os.stat(live)
    declared = sha256_file(live)

    def tamper(index):
        if index == 1:
            with open(live, "r+b") as fh:
                fh.seek(CHUNK * 3)
                fh.write(b"B" * CHUNK)
            os.utime(live, ns=(st.st_atime_ns, st.st_mtime_ns))

    client.on_chunk = tamper
    r = client.sync_file(live, "live.log")
    print(f"   {r.action:9} {r.detail}")
    print(f"   published? {store.has_blob(declared)}   (the torn read is discarded)")

    client.on_chunk = None
    r = client.sync_file(live, "live.log")
    print(f"   next pass: {r.action}  sha256 {r.sha256[:16]}...")

    line("6. A batch of files, with the link dropping once, run by the agent")
    batch = []
    for i in range(3):
        p = os.path.join(local, f"batch-{i}.bin")
        with open(p, "wb") as fh:
            fh.write(os.urandom(CHUNK * 3))
        batch.append(Job(p, f"batch-{i}.bin"))

    dropped = []

    def flaky(index):
        if index == 1 and not dropped:
            dropped.append(index)
            raise TransientError("connection reset by peer")

    client.on_chunk = flaky
    outcomes = sync_all(client, batch, workers=2, base_delay=0.6,
                        log=lambda m: print(f"   agent: {m}"))
    client.on_chunk = None
    for o in sorted(outcomes, key=lambda x: x.remote_path):
        print(f"   {o.action:9} {o.remote_path:14} attempt {o.attempts}   "
              f"file bytes sent: {o.bytes_sent:,}")

    line("Result")
    print(f"   server holds {len(os.listdir(store.blobs))} verified blobs")
    for row in store.db.execute("SELECT path, sha256, size FROM paths ORDER BY path"):
        print(f"   {row[0]:24} {row[1][:16]}...  {row[2]:>10,} bytes")
    print("\n   Note: the pre-rename path is still listed. Deletions and tombstones")
    print("   are part of the design but out of scope for this prototype.")

    httpd.shutdown()
    httpd.server_close()
    store.close()
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
