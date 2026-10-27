import json
import queue
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import Mock, patch

from orchestrator import media, download_job, gui, selection_dialog


def video(format_id="137", height=1080, **extra):
    return dict(format_id=format_id, url="https://media.example/" + format_id,
                protocol="https", ext="mp4", vcodec="avc1.640028", acodec="none",
                width=height * 16 // 9, height=height, fps=30, **extra)


def metadata():
    return {"id": "test", "title": 'Música: ¿qué tal? / prueba 🎵', "formats": [
        {"format_id": "140", "url": "https://media.example/audio", "protocol": "https",
         "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2"},
        video("136", 720), video()]}


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()

    @patch.object(media, "ytdlp_command", return_value=["yt-dlp"])
    @patch.object(media, "run_process")
    def test_metadata_query_returns_title_and_formats_without_downloading(self, run, command):
        run.return_value = json.dumps(metadata())
        info = media.fetch_metadata("https://youtu.be/test", self.cancel)
        self.assertEqual(info["title"], metadata()["title"])
        self.assertIn("--skip-download", run.call_args.args[0])
        self.assertEqual(run.call_args.args[2], 120)

    def test_only_actual_mp4_resolutions(self):
        info = metadata()
        unsupported = video("webm", 2160)
        unsupported.update(ext="webm", vcodec="vp9")
        manifest = video("hls", 1440)
        manifest["protocol"] = "m3u8_native"
        info["formats"] += [unsupported, manifest]
        choices = media.mp4_choices(info)
        self.assertEqual([c.height for c in choices], [1080, 720])
        self.assertEqual(choices[0].audio_id, "140")

    def test_mp4_is_not_assumed_to_mean_h264(self):
        info = metadata()
        info["formats"][-1]["vcodec"] = "av01.0.08M.08"
        self.assertIn("av01", media.mp4_choices(info)[0].label)

    def test_video_without_available_audio_is_not_offered(self):
        self.assertEqual(media.mp4_choices({"formats": [video()]}), [])

    def test_progressive_mp4_does_not_request_another_audio_track(self):
        fmt = video()
        fmt["acodec"] = "mp4a.40.2"
        self.assertIsNone(media.mp4_choices({"formats": [fmt]})[0].audio_id)

    @patch.object(media, "validate_stream")
    def test_unreachable_quality_is_not_offered(self, validate):
        def access(fmt, cancel):
            if fmt["format_id"] == "137":
                raise media.MediaError("HTTP 403")
        validate.side_effect = access
        self.assertEqual([c.height for c in media.available_choices(metadata(), self.cancel)], [720])

    def test_changed_format_is_not_silently_replaced(self):
        info = metadata()
        choice = media.mp4_choices(info)[0]
        info["formats"].pop()
        with self.assertRaises(media.MediaError):
            media.resolve_choice(info, choice)

    @patch.object(media, "ytdlp_command", return_value=["yt-dlp"])
    @patch.object(media, "run_process", side_effect=media.MediaError("yt-dlp falló"))
    def test_ytdlp_error_is_propagated(self, *_):
        with self.assertRaisesRegex(media.MediaError, "yt-dlp"):
            media.fetch_metadata("https://youtu.be/test", self.cancel)

    @patch.object(media, "ytdlp_command", return_value=["yt-dlp"])
    @patch.object(media, "run_process", return_value="<html>error</html>")
    def test_html_instead_of_metadata_is_an_error(self, *_):
        with self.assertRaisesRegex(media.MediaError, "metadatos"):
            media.fetch_metadata("https://youtu.be/test", self.cancel)

    @patch.object(media.urllib.request, "urlopen")
    def test_html_stream_is_rejected(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.headers = {"Content-Type": "text/html"}
        response.geturl.return_value = "https://media.example/video"
        response.read.return_value = b"<!DOCTYPE html><html>"
        with self.assertRaisesRegex(media.MediaError, "HTML"):
            media.validate_stream(video(), self.cancel)

    @patch.object(media.urllib.request, "urlopen")
    def test_manifest_never_reaches_network(self, urlopen):
        fmt = video()
        fmt["protocol"] = "m3u8_native"
        with self.assertRaises(media.MediaError):
            media.validate_stream(fmt, self.cancel)
        urlopen.assert_not_called()

    @patch.object(media.subprocess, "Popen")
    def test_cancel_before_query_does_not_start_a_process(self, popen):
        self.cancel.set()
        with self.assertRaises(media.Cancelled):
            media.fetch_metadata("https://youtu.be/test", self.cancel)
        popen.assert_not_called()

    @patch.object(media.subprocess, "Popen")
    def test_running_query_is_killed_when_cancelled(self, popen):
        process = popen.return_value
        process.poll.return_value = None
        def communicate(**kwargs):
            if kwargs:
                self.cancel.set()
                raise subprocess.TimeoutExpired("yt-dlp", 0.2)
            return "", ""
        process.communicate.side_effect = communicate
        with self.assertRaises(media.Cancelled):
            media.run_process(["yt-dlp"], self.cancel, 120)
        process.kill.assert_called_once()

    @patch.object(media.subprocess, "Popen")
    def test_nonzero_exit_is_not_success(self, popen):
        process = popen.return_value
        process.communicate.return_value = ("", "extraction failed")
        process.returncode = 1
        process.poll.return_value = 1
        with self.assertRaisesRegex(media.MediaError, "extraction failed"):
            media.run_process(["yt-dlp"], self.cancel, 120)


class DestinationTests(unittest.TestCase):
    def test_special_title_is_safe_on_windows_and_linux(self):
        name = media.safe_filename(metadata()["title"])
        self.assertIn("Música", name)
        self.assertEqual(PureWindowsPath(name).name, name)
        self.assertEqual(PurePosixPath(name).name, name)
        self.assertNotIn(":", name)
        self.assertNotIn("?", name)
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith(".mp4"))

    def test_reserved_and_empty_names(self):
        self.assertEqual(media.safe_filename("CON.mp4"), "_CON.mp4")
        self.assertEqual(media.safe_filename("..."), "video.mp4")
        self.assertEqual(media.safe_filename("clip.mp4"), "clip.mp4")

    def test_destination_cannot_escape_selected_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = media.destination(temporary, "../../elsewhere\\video")
            self.assertEqual(target.parent, Path(temporary).resolve())

    def test_existing_file_remains_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "video.mp4"
            target.write_bytes(b"original")
            with self.assertRaises(media.MediaError):
                media.destination(temporary, "video")
            self.assertEqual(target.read_bytes(), b"original")

    def test_atomic_publish_never_replaces_racing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "stage.mp4"
            source.write_bytes(b"new")
            target = Path(temporary) / "video.mp4"
            target.write_bytes(b"original")
            with self.assertRaises(media.MediaError):
                media.publish(source, target)
            self.assertEqual(target.read_bytes(), b"original")

    def test_publish_complete_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "stage.mp4"
            source.write_bytes(b"complete")
            target = Path(temporary) / "Música.mp4"
            media.publish(source, target)
            source.unlink(missing_ok=True)
            self.assertEqual(target.read_bytes(), b"complete")


class JobTests(unittest.TestCase):
    def execute(self, folder, info, run_process, cancel=None):
        cancel = cancel or threading.Event()
        core = Mock()
        core.download.side_effect = lambda url, **kwargs: kwargs["output"].write_bytes(b"video")
        target = Path(folder) / "chosen.mp4"
        with patch.object(media, "fetch_metadata", return_value=info), patch.object(media, "validate_stream"), patch.object(media, "ytdlp_command", return_value=["yt-dlp"]), patch.object(download_job.shutil, "which", return_value="ffmpeg"), patch.object(media, "run_process", side_effect=run_process):
            result = download_job.download_mp4("https://youtu.be/test", media.mp4_choices(info)[0],
                       target, cancel, core, Mock(), 8, Mock(), Mock())
        return result, core

    def test_separate_audio_and_video_publish_only_after_ffmpeg(self):
        info = metadata()
        calls = []
        def process(args, cancel, timeout):
            calls.append(args)
            output = Path(args[args.index("-o") + 1] if "-o" in args else args[-1])
            output.write_bytes(b"complete")
        with tempfile.TemporaryDirectory() as temporary:
            result, core = self.execute(temporary, info, process)
            self.assertEqual(result[0].read_bytes(), b"complete")
            self.assertEqual(list(Path(temporary).iterdir()), [result[0]])
            self.assertEqual(core.download.call_args.kwargs["connections"], 8)
            self.assertEqual(calls[0][calls[0].index("-f") + 1], "140")
            self.assertIn("1:a:0", calls[1])

    def test_progressive_video_needs_no_audio_download(self):
        fmt = video()
        fmt["acodec"] = "mp4a.40.2"
        info = {"formats": [fmt]}
        calls = []
        def process(args, cancel, timeout):
            calls.append(args)
            Path(args[-1]).write_bytes(b"complete")
        with tempfile.TemporaryDirectory() as temporary:
            self.execute(temporary, info, process)
            self.assertEqual(len(calls), 1)
            self.assertIn("0:a:0", calls[0])

    def test_ffmpeg_failure_never_publishes_final_or_leaves_stage(self):
        fmt = video()
        fmt["acodec"] = "mp4a.40.2"
        def process(args, cancel, timeout):
            Path(args[-1]).write_bytes(b"incomplete")
            raise media.MediaError("FFmpeg falló")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(media.MediaError, "FFmpeg"):
                self.execute(temporary, {"formats": [fmt]}, process)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    @patch.object(media, "fetch_metadata")
    def test_cancel_before_job_does_not_download(self, fetch):
        cancel = threading.Event()
        cancel.set()
        core = Mock()
        with self.assertRaises(media.Cancelled):
            download_job.download_mp4("https://youtu.be/test", None, Path("unused.mp4"),
                                     cancel, core, Mock(), 8, Mock(), Mock())
        core.download.assert_not_called()
        fetch.assert_not_called()

    def test_existing_destination_does_not_start_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "chosen.mp4"
            target.write_bytes(b"original")
            with self.assertRaises(media.MediaError):
                self.execute(temporary, metadata(), Mock())
            self.assertEqual(target.read_bytes(), b"original")


class DialogTests(unittest.TestCase):
    def test_cancelled_dialog_cannot_confirm_download(self):
        dialog = object.__new__(selection_dialog.SelectionDialog)
        dialog.cancel = threading.Event()
        dialog.cancel.set()
        dialog.finished = Mock()
        dialog.confirm()
        dialog.finished.assert_not_called()

    def test_cancel_waits_for_query_and_returns_no_selection(self):
        dialog = object.__new__(selection_dialog.SelectionDialog)
        dialog.cancel = threading.Event()
        dialog.download = Mock()
        dialog.status = Mock()
        dialog.window = Mock()
        dialog.worker = Mock()
        dialog.worker.is_alive.return_value = False
        dialog.finished = Mock()
        dialog.on_cancel()
        dialog.poll()
        dialog.finished.assert_called_once_with(None)

    @patch.object(selection_dialog.media, "fetch_metadata", side_effect=media.MediaError("consulta fallida"))
    def test_metadata_failure_only_sends_error_event(self, fetch):
        dialog = object.__new__(selection_dialog.SelectionDialog)
        dialog.cancel = threading.Event()
        dialog.events = queue.Queue()
        dialog.query("https://youtu.be/test")
        info, choices, error = dialog.events.get_nowait()
        self.assertIsNone(info)
        self.assertIsNone(choices)
        self.assertIsInstance(error, media.MediaError)


if __name__ == "__main__":
    unittest.main()
