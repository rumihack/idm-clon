import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import listen
from orchestrator import media


class MultimediaTests(unittest.TestCase):
    def test_hosts(self):
        for url in ("https://youtube.com/watch?v=a", "https://m.youtube.com/shorts/a", "https://youtu.be/a"):
            self.assertTrue(listen.youtube_url(url))
        for url in ("https://youtube.com.evil.test/a", "https://example.com/youtube.com"):
            self.assertFalse(listen.youtube_url(url))

    @patch.dict(media.os.environ, {"IDM_CLON_YT_DLP": "", "IDM_CLON_EJS_REMOTE": ""})
    @patch("orchestrator.media.shutil.which")
    def test_path_and_node(self, which):
        which.side_effect = lambda name: {"yt-dlp": "C:/tools/yt-dlp.exe", "node": "C:/Program Files/node.exe"}.get(name)
        command = media.ytdlp_command()
        self.assertEqual(command[0], "C:/tools/yt-dlp.exe")
        self.assertIn("node:C:/Program Files/node.exe", command)
        self.assertNotIn("--remote-components", command)

    @patch.dict(media.os.environ, {"IDM_CLON_YT_DLP": "", "IDM_CLON_EJS_REMOTE": ""})
    @patch("orchestrator.media.shutil.which", return_value=None)
    @patch("orchestrator.media.Path.is_file", return_value=True)
    @patch("orchestrator.media.os.access", return_value=True)
    def test_legacy_linux_without_node(self, *_):
        command = media.ytdlp_command()
        self.assertEqual(command[0], str(Path.home() / ".local/bin/yt-dlp"))
        self.assertNotIn("--js-runtimes", command)

    @patch.dict(media.os.environ, {"IDM_CLON_YT_DLP": "", "IDM_CLON_EJS_REMOTE": ""})
    @patch("orchestrator.media.shutil.which", return_value=None)
    @patch("orchestrator.media.Path.is_file", return_value=False)
    @patch("orchestrator.media.importlib.util.find_spec", return_value=object())
    def test_module_fallback(self, *_):
        self.assertEqual(media.ytdlp_command()[:3], [media.sys.executable, "-m", "yt_dlp"])

    @patch.dict(media.os.environ, {"IDM_CLON_YT_DLP": "", "IDM_CLON_EJS_REMOTE": "1"})
    @patch("orchestrator.media.shutil.which", return_value="tool")
    def test_ejs_opt_in(self, *_):
        self.assertIn("ejs:github", media.ytdlp_command())

    def handler(self, url):
        handler = object.__new__(listen.DownloadRequestHandler)
        body = json.dumps({"url": url}).encode()
        handler.path = "/download"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._reply = Mock()
        return handler

    @patch("listen.subprocess.Popen")
    def test_youtube_opens_selector_without_starting_transfers(self, popen):
        handler = self.handler("https://youtu.be/a")
        handler.do_POST()
        popen.assert_called_once()
        args = popen.call_args.args[0]
        self.assertEqual(args[2:], ["https://youtu.be/a", "--select-youtube"])
        self.assertEqual(handler._reply.call_args.args[0], 200)

    @patch("listen.subprocess.Popen")
    def test_generic_download_unchanged(self, popen):
        handler = self.handler("https://example.com/file.zip")
        handler.do_POST()
        self.assertEqual(popen.call_args.args[0][2:], ["https://example.com/file.zip"])

    @patch("listen.subprocess.Popen", side_effect=OSError("GUI failed"))
    def test_gui_launch_failure_is_reported(self, popen):
        handler = self.handler("https://youtu.be/a")
        handler.do_POST()
        self.assertEqual(handler._reply.call_args.args[0], 502)

    @patch("listen.subprocess.Popen")
    def test_invalid_url_never_starts_process(self, popen):
        handler = self.handler("file:///page.html")
        handler.do_POST()
        popen.assert_not_called()



class GuiAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from orchestrator import gui
        cls.gui = gui

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
