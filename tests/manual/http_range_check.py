"""HTTP-only checks for http_range_server.py; no downloads are saved to disk."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import time

from http_range_server import BLOCK_SIZE, HOST, NAME, PORT, SIZE, fixture_block


def expected_hash(start, end):
    digest = hashlib.sha256()
    for index in range(start // BLOCK_SIZE, end // BLOCK_SIZE + 1):
        block = fixture_block(index)
        left = max(start - index * BLOCK_SIZE, 0)
        right = min(end - index * BLOCK_SIZE + 1, BLOCK_SIZE)
        digest.update(block[left:right])
    return digest.hexdigest()


def check(method="GET", range_value=None, status=200, bounds=None,
          extra_headers=None, pause=0):
    connection = http.client.HTTPConnection(HOST, PORT, timeout=30)
    headers = dict(extra_headers or {})
    if range_value is not None:
        headers["Range"] = range_value
    try:
        connection.request(method, "/" + NAME, headers=headers)
        response = connection.getresponse()
        assert response.status == status, (range_value, response.status, status)
        assert response.getheader("Accept-Ranges") == "bytes"
        assert response.getheader("ETag") == ETAG
        if status == 416:
            length = 0
            assert response.getheader("Content-Range") == f"bytes */{SIZE}"
        else:
            start, end = bounds or (0, SIZE - 1)
            length = end - start + 1
            expected_range = f"bytes {start}-{end}/{SIZE}" if status == 206 else None
            assert response.getheader("Content-Range") == expected_range
        assert response.getheader("Content-Length") == str(length)
        if pause:
            time.sleep(pause)
        digest = hashlib.sha256()
        received = 0
        while chunk := response.read(65536):
            received += len(chunk)
            digest.update(chunk)
        assert received == (0 if method == "HEAD" else length), received
        if method != "HEAD" and length:
            assert digest.hexdigest() == expected_hash(start, end), range_value
    finally:
        connection.close()
    print(f"OK {method} Range={range_value!r} status={status}", flush=True)


def main():
    global ETAG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pause-seconds", type=float, default=0,
                        help="Pause a range reader before receiving its body")
    args = parser.parse_args()
    if not 0 <= args.pause_seconds <= 3600:
        parser.error("--pause-seconds must be between 0 and 3600")
    ETAG = '"' + expected_hash(0, SIZE - 1) + '"'
    check("HEAD")
    check("HEAD", "bytes=0-0")
    check(range_value="bytes=0-0", status=206, bounds=(0, 0))
    check(range_value=f"bytes={SIZE-1}-", status=206, bounds=(SIZE-1, SIZE-1))
    check(range_value="bytes=-17", status=206, bounds=(SIZE-17, SIZE-1))
    check(range_value=f"bytes={SIZE-8}-{SIZE+100}", status=206, bounds=(SIZE-8, SIZE-1))
    check(range_value=f"bytes={SIZE}-", status=416)
    check(range_value="bytes=-0", status=416)
    check(range_value="bytes=0-31", status=206, bounds=(0, 31),
          extra_headers={"If-Range": ETAG})
    # Full bodies are streamed and hashed, never retained in memory.
    check()
    check(range_value="bytes=bad")
    check(range_value="bytes=0-1,4-5")
    check(range_value="bytes=9-3")
    check(range_value="bytes=0-31", extra_headers={"If-Range": '"different"'})
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for index in range(8):
            start = index * (SIZE // 8)
            end = start + SIZE // 8 - 1
            futures.append(pool.submit(check, range_value=f"bytes={start}-{end}",
                                       status=206, bounds=(start, end)))
        for future in futures:
            future.result()
    if args.pause_seconds:
        check(range_value="bytes=0-4194303", status=206, bounds=(0, 4194303),
              pause=args.pause_seconds)
    # Disconnect an active response, then prove the server still responds.
    connection = http.client.HTTPConnection(HOST, PORT, timeout=30)
    try:
        connection.request("GET", "/" + NAME)
        response = connection.getresponse()
        response.read(1)
        response.close()
    finally:
        connection.close()
    check(range_value="bytes=0-0", status=206, bounds=(0, 0))
    print(f"PASS: HTTP, concurrent ranges and SHA-256 {ETAG}", flush=True)


if __name__ == "__main__":
    main()
