import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pawchive_app.updater import (
    BASELINE_COMMIT,
    UpdateManager,
    _APPLY_HELPER,
)


LATEST = "a" * 40


def make_update_zip(extra_entries=None):
    prefix = "nothing-feature/GameModFinder_0.1.4/GameModFinder/"
    files = {
        "main_v2.py": "print('app')",
        "run.bat": "@echo off",
        "requirements.txt": "PySide6",
        "pawchive_app/ui.py": "# ui",
        "pawchive_app/updater.py": "# updater",
    }
    if extra_entries:
        files.update(extra_entries)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(prefix + name, text)
    return buffer.getvalue()


class UpdateManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = UpdateManager(root / "roaming", root / "local")

    def tearDown(self):
        self.temp.cleanup()

    def test_check_detects_new_branch_commit(self):
        with patch.object(
            self.manager,
            "_request_bytes",
            return_value=json.dumps({"sha": LATEST}).encode("utf-8"),
        ) as request:
            result = self.manager.check()

        self.assertEqual(result.installed_commit, BASELINE_COMMIT)
        self.assertEqual(result.latest_commit, LATEST)
        self.assertTrue(result.available)
        self.assertIn("feature%2Fpawchive-mod-finder-v2", request.call_args.args[0])

    def test_check_reports_up_to_date_after_commit_marker_is_written(self):
        self.manager.installed_commit_path.write_text(LATEST, encoding="ascii")
        with patch.object(
            self.manager,
            "_request_bytes",
            return_value=json.dumps({"sha": LATEST}).encode("utf-8"),
        ):
            result = self.manager.check()
        self.assertFalse(result.available)

    def test_check_rejects_invalid_github_response(self):
        with patch.object(
            self.manager, "_request_bytes", return_value=b'{"sha":"not-a-sha"}'
        ):
            with self.assertRaisesRegex(RuntimeError, "올바른 업데이트 버전"):
                self.manager.check()

    def test_prepare_extracts_only_the_app_folder(self):
        payload = make_update_zip({"tests/test_example.py": "pass"})
        with patch.object(self.manager, "_request_bytes", return_value=payload):
            package = self.manager.prepare(LATEST)

        self.assertEqual(package.commit, LATEST)
        self.assertTrue((package.staged_app / "main_v2.py").is_file())
        self.assertTrue((package.staged_app / "pawchive_app" / "updater.py").is_file())
        self.assertTrue((package.staged_app / "tests" / "test_example.py").is_file())
        self.assertFalse((package.staged_app.parent / "outside.txt").exists())

    def test_prepare_rejects_path_traversal(self):
        payload = make_update_zip({"../outside.txt": "not allowed"})
        with patch.object(self.manager, "_request_bytes", return_value=payload):
            with self.assertRaisesRegex(RuntimeError, "안전하지 않은 파일 경로"):
                self.manager.prepare(LATEST)

    def test_prepare_rejects_unexpected_archive_layout(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("wrong-root/main_v2.py", "print('no')")
        with patch.object(self.manager, "_request_bytes", return_value=buffer.getvalue()):
            with self.assertRaisesRegex(RuntimeError, "GameModFinder 폴더"):
                self.manager.prepare(LATEST)

    def test_helper_continues_if_app_exited_before_open_process(self):
        import ctypes
        from types import SimpleNamespace

        class FakeFunction:
            def __init__(self, result=None):
                self.result = result
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.result

        kernel32 = SimpleNamespace(
            OpenProcess=FakeFunction(None),
            WaitForSingleObject=FakeFunction(0),
            CloseHandle=FakeFunction(None),
        )
        namespace = {"__name__": "apply_update_test"}
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "update.log"
            namespace["log_path"] = log_path
            with patch.object(ctypes, "WinDLL", return_value=kernel32, create=True), patch.object(
                ctypes, "get_last_error", return_value=87, create=True
            ):
                exec(_APPLY_HELPER, namespace)
                namespace["wait_for_parent"](1234)

            self.assertIn(
                "has already exited; continuing update",
                log_path.read_text(encoding="utf-8"),
            )

    def test_helper_rejects_non_positive_process_id(self):
        import ctypes
        from types import SimpleNamespace

        class FakeFunction:
            def __init__(self):
                self.argtypes = None
                self.restype = None

        kernel32 = SimpleNamespace(
            OpenProcess=FakeFunction(),
            WaitForSingleObject=FakeFunction(),
            CloseHandle=FakeFunction(),
        )
        namespace = {"__name__": "apply_update_test"}
        with patch.object(ctypes, "WinDLL", return_value=kernel32, create=True):
            exec(_APPLY_HELPER, namespace)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                namespace["wait_for_parent"](0)

    def test_windows_update_helper_is_valid_python(self):
        compile(_APPLY_HELPER, "apply_update.py", "exec")


if __name__ == "__main__":
    unittest.main()
