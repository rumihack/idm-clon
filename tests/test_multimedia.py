import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import listen


class MultimediaTests(unittest.TestCase):
    def test_hosts(self):
        for url in ("https://youtube.com/watch?v=a", "https://m.youtube.com/shorts/a", "https://youtu.be/a"):
            self.assertTrue(listen.youtube_url(url))
        for url in ("https://youtube.com.evil.test/a", "https://example.com/youtube.com"):
            self.assertFalse(listen.youtube_url(url))

    @patch.dict(listen.os.environ, {"IDM_CLON_YT_DLP": "", "IDM_CLON_EJS_REMOTE": ""})
    @patch("listen.shutil.which")
    def test_path_and_node(self, which):
        which.side_effect = lambda name: {"yt-dlp": "C:/tools/yt-dlp.exe", "node": "C:/Program Files/node.exe"}.get(name)
        command = listen.ytdlp_command()
        self.assertEqual(command[0], "C:/tools/yt-dlp.exe")
        self.assertIn("node:C:/Program Files/node.exe", command)
        self.assertNotIn("--remote-components", command)

    @patch.dict(listen.os.environ, {"IDM_CLON_YT_DLP": "", "IDM_CLON_EJS_REMOTE": ""})
    @patch("listen.shutil.which", return_value=None)
    @patch("listen.Path.is_file", return_value=True)
    @patch("listen.os.access", return_value=True)
    def test_legacy_linux_without_node(self, *_):
        command = listen.ytdlp_command()
        self.assertEqual(command[0], str(Path.home() / ".local/bin/yt-dlp"))
        self.assertNotIn("--js-runtimes", command)

    @patch.dict(listen.os.environ, {"IDM_CLON_YT_DLP": "", "IDM_CLON_EJS_REMOTE": ""})
    @patch("listen.shutil.which", return_value=None)
    @patch("listen.Path.is_file", return_value=False)
    @patch("listen.importlib.util.find_spec", return_value=object())
    def test_module_fallback(self, *_):
        self.assertEqual(listen.ytdlp_command()[:3], [listen.sys.executable, "-m", "yt_dlp"])

    @patch.dict(listen.os.environ, {"IDM_CLON_YT_DLP": "", "IDM_CLON_EJS_REMOTE": "1"})
    @patch("listen.shutil.which", return_value="tool")
    def test_ejs_opt_in(self, *_):
        self.assertIn("ejs:github", listen.ytdlp_command())

    def handler(self, url):
        handler = object.__new__(listen.DownloadRequestHandler)
        body = json.dumps({"url": url}).encode()
        handler.path = "/download"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._reply = Mock()
        return handler

    @patch("listen.subprocess.Popen")
    @patch("listen.resolve_video", side_effect=ValueError("extraction failed"))
    @patch("listen.ytdlp_command", return_value=["yt-dlp"])
    @patch("listen.shutil.which", return_value="ffmpeg")
    def test_extraction_failure_never_launches_gui_or_audio(self, which, command, resolve, popen):
        handler = self.handler("https://youtube.com/watch?v=a")
        handler.do_POST()
        popen.assert_not_called()
        self.assertEqual(handler._reply.call_args.args[0], 502)

    @patch("listen.subprocess.Popen")
    def test_generic_download_unchanged(self, popen):
        handler = self.handler("https://example.com/file.zip")
        handler.do_POST()
        self.assertEqual(popen.call_args.args[0][2], "https://example.com/file.zip")
        self.assertEqual(handler._reply.call_args.args[0], 200)

    @patch("listen.urllib.request.urlopen")
    @patch("listen.subprocess.run")
    def test_direct_media_and_html_rejection(self, run, urlopen):
        run.return_value.stdout = json.dumps({"url": "https://media.example/video", "protocol": "https", "ext": "mp4", "vcodec": "avc1"})
        response = urlopen.return_value.__enter__.return_value
        response.headers = {"Content-Type": "video/mp4"}
        response.geturl.return_value = "https://media.example/video"
        response.read.return_value = b"video bytes"
        self.assertEqual(listen.resolve_video(["yt-dlp"], "https://youtu.be/a"), "https://media.example/video")
        response.headers = {"Content-Type": "text/html"}
        with self.assertRaises(ValueError):
            listen.resolve_video(["yt-dlp"], "https://youtu.be/a")
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    @patch("listen.urllib.request.urlopen")
    @patch("listen.subprocess.run")
    def test_manifest_rejected_before_network(self, run, urlopen):
        run.return_value.stdout = json.dumps({"url": "https://media.example/manifest", "protocol": "m3u8_native", "ext": "mp4", "vcodec": "avc1"})
        with self.assertRaises(ValueError):
            listen.resolve_video(["yt-dlp"], "https://youtu.be/a")
        urlopen.assert_not_called()

    def test_audio_supervisor_stops_and_cleans_on_release(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "audio"
            directory.mkdir()
            (directory / "release").touch()
            process = Mock()
            process.poll.side_effect = [None, None, 1]
            process.returncode = 1
            error_file = (directory / "audio.stderr.log").open("wb")
            listen.supervise_audio(process, Mock(), directory, error_file)
            process.terminate.assert_called_once()
            self.assertFalse(directory.exists())

    @patch("listen.subprocess.Popen")
    @patch("listen.resolve_video", return_value="https://media.example/video")
    @patch("listen.ytdlp_command", return_value=["yt-dlp"])
    @patch("listen.shutil.which", return_value="ffmpeg")
    def test_gui_launch_failure_stops_audio_and_cleans(self, which, command, resolve, popen):
        process = Mock()
        process.poll.return_value = None
        popen.side_effect = [process, OSError("GUI failed")]
        handler = self.handler("https://youtu.be/a")
        handler.do_POST()
        process.terminate.assert_called_once()
        args = popen.call_args_list[0].args[0]
        directory = Path(args[args.index("-o") + 1]).parent
        self.assertFalse(directory.exists())
        self.assertEqual(handler._reply.call_args.args[0], 502)


class GuiAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("multimedia_gui", Path(__file__).resolve().parents[1] / "orchestrator/gui.py")
        core_spec = importlib.util.spec_from_file_location("multimedia_core", Path(__file__).resolve().parents[1] / "orchestrator/orchestrator.py")
        core = importlib.util.module_from_spec(core_spec)
        core_spec.loader.exec_module(core)
        cls.gui = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"orchestrator": core}):
            spec.loader.exec_module(cls.gui)

    def run_audio_failure(self, cancelled=False, status=None):
        import queue
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent)
            app = object.__new__(self.gui.App)
            app.audio_dir = directory
            app.audio_status = directory / "audio.status"
            app.cancelled = cancelled
            app.active = Mock()
            app.events = queue.Queue()
            if status is not None:
                app.audio_status.write_text(status, encoding="ascii")
                (directory / "audio.stderr.log").write_text("audio diagnostic", encoding="utf-8")
            with patch.object(self.gui.core, "download", return_value=(directory / "video", 1, 1)), patch.object(self.gui.time, "monotonic", side_effect=[0, 3601]):
                app._run_download("https://media.example/video", directory, 8)
            kind, error = app.events.get_nowait()
            self.assertEqual(kind, "failed")
            self.assertTrue((directory / "release").exists())
            return str(error)

    def test_audio_wait_has_deadline(self):
        self.assertIn("agotado", self.run_audio_failure())

    def test_cancelled_download_releases_audio(self):
        self.assertIn("cancelada", self.run_audio_failure(cancelled=True))

    def test_audio_failure_includes_diagnostic(self):
        self.assertIn("audio diagnostic", self.run_audio_failure(status="1"))


if __name__ == "__main__":
    unittest.main()
