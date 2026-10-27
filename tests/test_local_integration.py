"""Integración local opcional: no accede a YouTube ni modifica archivos del usuario."""

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from orchestrator import orchestrator as core, media, download_job, selection_dialog


class QuietProgress:
    def __init__(self, total, done):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def update(self, *args):
        pass

    def log(self, *args):
        pass


class LocalIntegrationTests(unittest.TestCase):
    def test_tk_widgets_load_metadata_and_confirm_without_downloading(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk no está disponible en esta sesión: {exc}")
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                choice = media.Choice("137", "140", 1920, 1080, 30, "avc1")
                completed = Mock()
                with patch.object(media, "fetch_metadata", return_value={"title": "Título de prueba"}), patch.object(media, "available_choices", return_value=[choice]):
                    dialog = selection_dialog.SelectionDialog(root, "https://youtu.be/test", temporary, completed)
                    dialog.window.withdraw()
                    deadline = time.monotonic() + 5
                    while not dialog.choices and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.01)
                    self.assertEqual(dialog.choices, [choice])
                    self.assertEqual(dialog.name.get(), "Título de prueba.mp4")
                    dialog.name.set("Nombre editado")
                    dialog.confirm()
                    completed.assert_called_once_with((choice, Path(temporary).resolve() / "Nombre editado.mp4"))
                    self.assertEqual(list(Path(temporary).iterdir()), [])
        finally:
            root.destroy()

    @unittest.skipUnless(core.DEFAULT_EXE.is_file(), "No está compilado el motor C++")
    def test_real_cpp_downloads_and_joins_four_ranges(self):
        payload = bytes(range(256)) * 4096
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                request_range = self.headers.get("Range", "")
                match = re.fullmatch(r"bytes=(\d+)-(\d+)", request_range)
                start, end = (int(v) for v in match.groups()) if match else (0, len(payload) - 1)
                received.append((start, end))
                self.send_response(206 if match else 200)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("ETag", '"test-data"')
                if match:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                self.wfile.write(payload[start:end + 1])

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / "output.bin"
                result = core.download(f"http://127.0.0.1:{server.server_port}/file",
                                       output=target, connections=4, progress=QuietProgress, log=Mock())
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(result[1], len(payload))
                self.assertEqual(len([r for r in received if r != (0, 0)]), 4)
                self.assertFalse(target.with_name("output.bin.parts").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Faltan FFmpeg/ffprobe")
    def test_real_ffmpeg_job_produces_mp4_with_video_and_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source = parent / "fixture.mp4"
            ffmpeg = shutil.which("ffmpeg")
            subprocess.run([ffmpeg, "-nostdin", "-n", "-f", "lavfi", "-i",
                            "color=c=blue:s=320x180:r=10", "-f", "lavfi", "-i",
                            "sine=frequency=440:sample_rate=44100", "-t", "0.5",
                            "-c:v", "mpeg4", "-c:a", "aac", str(source)],
                           check=True, capture_output=True, timeout=30)
            # Fuente sintética: la elección prueba la orquestación y el remux,
            # no la compatibilidad del extractor remoto.
            fmt = {"format_id": "test", "url": "https://media.example/video",
                   "protocol": "https", "ext": "mp4", "vcodec": "avc1",
                   "acodec": "mp4a.40.2", "width": 320, "height": 180, "fps": 10}
            info = {"formats": [fmt]}
            fake_core = Mock()
            fake_core.download.side_effect = lambda url, **kw: shutil.copyfile(source, kw["output"])
            target = parent / "selected.mp4"
            with patch.object(media, "fetch_metadata", return_value=info), patch.object(media, "validate_stream"):
                result = download_job.download_mp4("https://youtu.be/test", media.mp4_choices(info)[0],
                           target, threading.Event(), fake_core, Mock(), 4, QuietProgress, Mock())
            probe = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_streams",
                                    "-show_format", "-of", "json", str(target)],
                                   check=True, capture_output=True, text=True, timeout=20)
            actual = json.loads(probe.stdout)
            self.assertEqual({s["codec_type"] for s in actual["streams"]}, {"video", "audio"})
            picture = next(s for s in actual["streams"] if s["codec_type"] == "video")
            self.assertEqual((picture["width"], picture["height"]), (320, 180))
            self.assertIn("mp4", actual["format"]["format_name"])
            self.assertEqual(result[1], target.stat().st_size)
            self.assertEqual(set(parent.iterdir()), {source, target})


if __name__ == "__main__":
    unittest.main()
