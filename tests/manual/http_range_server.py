"""Local range fixture server. Data defaults to the OS temporary directory.

Run with Python 3.12; stop with Ctrl+C. No IDM-Clon modules are imported.
"""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit


HOST, PORT = "127.0.0.1", 8765
SIZE = 100 * 1024 * 1024
BLOCK_SIZE = 65536
NAME = "fixture-100MiB.bin"


def fixture_block(index):
    prefix = b"IDM-Clon-fixture-v1" + index.to_bytes(8, "big")
    return b"".join(hashlib.sha256(prefix + j.to_bytes(4, "big")).digest()
                    for j in range(BLOCK_SIZE // 32))


def prepare_fixture(directory):
    """Create exclusively, or verify every byte of an existing fixture."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / NAME
    digest = hashlib.sha256()
    try:
        stream = path.open("xb")
    except FileExistsError:
        if path.stat().st_size != SIZE:
            raise ValueError(f"Existing fixture has wrong size: {path}")
        with path.open("rb") as stream:
            for index in range(SIZE // BLOCK_SIZE):
                expected = fixture_block(index)
                if stream.read(BLOCK_SIZE) != expected:
                    raise ValueError(f"Existing fixture differs at block {index}: {path}")
                digest.update(expected)
    else:
        # An interrupted creation remains visible and is rejected on the next run.
        with stream:
            for index in range(SIZE // BLOCK_SIZE):
                block = fixture_block(index)
                stream.write(block)
                digest.update(block)
    return path, digest.hexdigest()


def select_range(value):
    """Return inclusive bounds, False for unsatisfiable, None for ignored."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value or "")
    if not match or not any(match.groups()):
        return None
    first, last = match.groups()
    # Bound integer parsing even for hostile, very long header values.
    if len(first) > 20 or len(last) > 20:
        return None
    if not first:
        count = int(last)
        return (max(0, SIZE - count), SIZE - 1) if count else False
    start = int(first)
    end = int(last) if last else SIZE - 1
    if last and end < start:
        return None  # Invalid syntax/semantics: ignore the Range field.
    if start >= SIZE:
        return False
    return start, min(end, SIZE - 1)


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.serve_file(head=True)

    def do_GET(self):
        self.serve_file(head=False)

    def serve_file(self, head):
        self.close_connection = True
        try:
            if urlsplit(self.path).path != "/" + NAME:
                self.send_error(404)
                return
            selected = None
            ranges = self.headers.get_all("Range", [])
            if not head and len(ranges) == 1:
                if_range = self.headers.get("If-Range")
                if if_range is None or if_range == self.server.etag:
                    selected = select_range(ranges[0].strip())
            if selected is False:
                self.send_response(416)
                self.common_headers(0)
                self.send_header("Content-Range", f"bytes */{SIZE}")
                self.end_headers()
                return
            start, end = selected if selected is not None else (0, SIZE - 1)
            self.send_response(206 if selected is not None else 200)
            self.common_headers(end - start + 1)
            if selected is not None:
                self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
            self.end_headers()
            if head:
                return
            with self.server.fixture.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(16384, remaining))
                    if not chunk:
                        raise OSError("Fixture changed while serving")
                    # Pace each chunk separately; blocked writes earn no credit.
                    if self.server.rate:
                        time.sleep(len(chunk) / self.server.rate)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    remaining -= len(chunk)
        except OSError as exc:
            self.log_message("connection ended: %s", exc)

    def common_headers(self, length):
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", self.server.etag)
        self.send_header("Connection", "close")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path,
                        default=Path(tempfile.gettempdir()) / "idm-clon-http-tests")
    parser.add_argument("--rate-kib", type=int, default=128,
                        help="KiB/s per connection; 0 disables pacing")
    args = parser.parse_args()
    if args.rate_kib < 0:
        parser.error("--rate-kib must be nonnegative")
    # Refuse repository-local generated files, including a custom work directory.
    repository = Path(__file__).resolve().parents[2]
    if args.work_dir.resolve().is_relative_to(repository):
        parser.error("--work-dir must be outside the repository")
    fixture, digest = prepare_fixture(args.work_dir)
    with ThreadingHTTPServer((HOST, PORT), Handler) as server:
        server.fixture = fixture
        server.etag = f'"{digest}"'
        server.rate = args.rate_kib * 1024
        print(f"Serving http://{HOST}:{PORT}/{NAME}", flush=True)
        print(f"File: {fixture}\nSHA-256: {digest}\nRate: {args.rate_kib} KiB/s/connection",
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
