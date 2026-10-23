"""Orquestador de idm-clon: descarga un archivo en fragmentos paralelos.

Flujo:
  1. Sondea el servidor (petición con Range: bytes=0-0) para conocer el tamaño
     total y saber si admite descargas por rangos.
  2. Divide el archivo en N rangos y lanza una instancia de core_downloader.exe
     por rango, en paralelo, con reintentos.
  3. Une los fragmentos en orden, verifica el tamaño final y borra los temporales.

Si el servidor no admite rangos (o dice admitirlos pero no los respeta),
descarga el archivo completo en una sola conexión desde Python.

Uso:
  python orchestrator.py <url> [-o salida] [-n conexiones] [--retries N]
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

USER_AGENT = "idm-clon-orchestrator/0.1"
DEFAULT_EXE = Path(__file__).resolve().parent.parent / "core_downloader" / "bin" / "core_downloader.exe"
MIN_PART_SIZE = 256 * 1024  # no tiene sentido abrir conexiones para trozos diminutos

# Códigos de salida de core_downloader (ver core_downloader/main.cpp).
EXIT_OK = 0
EXIT_NETWORK = 3
EXIT_RANGE_REJECTED = 4
EXIT_SIZE_MISMATCH = 5
RETRYABLE_EXITS = {EXIT_NETWORK, EXIT_SIZE_MISMATCH}


class DownloadError(Exception):
    pass


class RangesNotHonored(DownloadError):
    """El servidor anunció rangos pero no los respetó (códigos 4 o 5)."""


def probe(url):
    """Devuelve (url_final, tamaño_total o None, admite_rangos)."""
    request = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        final_url = response.geturl()
        if response.status == 206:
            match = re.match(r"bytes \d+-\d+/(\d+)", response.headers.get("Content-Range", ""))
            if match:
                return final_url, int(match.group(1)), True
        length = response.headers.get("Content-Length")
        # 200: el servidor ignoró el Range; Content-Length es el tamaño completo.
        size = int(length) if response.status == 200 and length else None
        return final_url, size, False


def split_ranges(total, connections):
    """Divide [0, total) en rangos inclusivos (inicio, fin) de tamaño parecido."""
    parts = max(1, min(connections, total // MIN_PART_SIZE))
    base, extra = divmod(total, parts)
    ranges, start = [], 0
    for i in range(parts):
        size = base + (1 if i < extra else 0)
        ranges.append((start, start + size - 1))
        start += size
    return ranges


def download_part(exe, url, index, start, end, part_path, retries):
    """Descarga un fragmento con core_downloader, reintentando fallos transitorios."""
    for attempt in range(1, retries + 1):
        result = subprocess.run(
            [str(exe), url, str(start), str(end), str(part_path)],
            capture_output=True,
            encoding="utf-8",  # main.cpp está en UTF-8 y sus mensajes también
            errors="replace",
        )
        if result.returncode == EXIT_OK:
            return index
        message = result.stderr.strip() or result.stdout.strip()
        if result.returncode not in RETRYABLE_EXITS or attempt == retries:
            error = f"fragmento {index} ({start}-{end}): código {result.returncode}: {message}"
            if result.returncode in (EXIT_RANGE_REJECTED, EXIT_SIZE_MISMATCH):
                raise RangesNotHonored(error)
            raise DownloadError(error)
        print(f"  fragmento {index}: intento {attempt} falló ({message}); reintentando...", file=sys.stderr)
        time.sleep(attempt)  # espera creciente entre reintentos


def download_parallel(exe, url, total, output, connections, retries):
    ranges = split_ranges(total, connections)
    parts_dir = output.with_name(output.name + ".parts")
    parts_dir.mkdir(exist_ok=True)
    part_paths = [parts_dir / f"part{i:03d}.tmp" for i in range(len(ranges))]

    print(f"Descargando {total} bytes en {len(ranges)} fragmentos...")
    try:
        with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
            futures = [
                pool.submit(download_part, exe, url, i, start, end, part_paths[i], retries)
                for i, (start, end) in enumerate(ranges)
            ]
            try:
                for done, future in enumerate(as_completed(futures), 1):
                    index = future.result()
                    start, end = ranges[index]
                    print(f"  [{done}/{len(ranges)}] fragmento {index} listo ({end - start + 1} bytes)")
            except BaseException:
                for f in futures:
                    f.cancel()
                raise

        print("Uniendo fragmentos...")
        with open(output, "wb") as out:
            for path in part_paths:
                with open(path, "rb") as part:
                    shutil.copyfileobj(part, out)

        if output.stat().st_size != total:
            raise DownloadError(f"tamaño final {output.stat().st_size} distinto del esperado {total}")
    finally:
        # Sin soporte de reanudación todavía: los fragmentos no sirven tras un fallo.
        shutil.rmtree(parts_dir, ignore_errors=True)


def download_single(url, output, expected):
    """Descarga sin rangos, en una sola conexión (servidores que no los admiten)."""
    print("El servidor no admite rangos: descargando en una sola conexión...")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response, open(output, "wb") as out:
        shutil.copyfileobj(response, out)
    size = output.stat().st_size
    if expected is not None and size != expected:
        raise DownloadError(f"tamaño final {size} distinto del esperado {expected}")


def default_output_name(url):
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
    return name or "descarga.bin"


def main():
    # UTF-8 y volcado por línea: acentos correctos en pipes (Git Bash) y
    # mensajes de stdout/stderr en el orden en que se emiten.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", line_buffering=True)

    parser = argparse.ArgumentParser(description="Descarga un archivo en fragmentos paralelos.")
    parser.add_argument("url")
    parser.add_argument("-o", "--output", help="archivo de salida (por defecto, el nombre de la URL)")
    parser.add_argument("-n", "--connections", type=int, default=8, help="conexiones paralelas (8)")
    parser.add_argument("--retries", type=int, default=3, help="intentos por fragmento (3)")
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE, help="ruta a core_downloader.exe")
    args = parser.parse_args()

    if args.connections < 1 or args.retries < 1:
        parser.error("--connections y --retries deben ser >= 1")

    try:
        final_url, total, ranges_ok = probe(args.url)
        output = Path(args.output or default_output_name(final_url)).resolve()
        began = time.monotonic()

        if ranges_ok and total > 0:
            if not args.exe.is_file():
                raise DownloadError(f"no se encontró {args.exe}; ejecuta core_downloader\\build.bat")
            try:
                download_parallel(args.exe, final_url, total, output, args.connections, args.retries)
            except RangesNotHonored as exc:
                print(f"Aviso: el servidor no respeta los rangos ({exc}).", file=sys.stderr)
                # El tamaño anunciado no es fiable, así que no se verifica.
                download_single(final_url, output, None)
        else:
            download_single(final_url, output, total)

        elapsed = time.monotonic() - began
        size = output.stat().st_size
        speed = size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        print(f"OK {output} ({size} bytes en {elapsed:.1f} s, {speed:.2f} MB/s)")
        return 0
    except (DownloadError, OSError) as exc:
        # urllib.error.URLError hereda de OSError.
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
