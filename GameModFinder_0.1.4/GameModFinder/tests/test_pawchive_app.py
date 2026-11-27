from __future__ import annotations

import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
import tempfile
import tarfile
import unittest
import zipfile
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

from pawchive_app import downloads
from pawchive_app.api import PawchiveClient
from pawchive_app.archive_analysis import ArchiveInspectionError, analyze_archive_for_ini
from pawchive_app.downloads import (DownloadError, _curl_transfer, download_file, fetch_thumbnail_bytes, resolve_file_url, resolve_thumbnail_url, safe_extract_zip, safe_extract_archive)
from pawchive_app.enhancements import pawchive_image_url
from pawchive_app.models import Creator, Post, parse_creator_url, post_matches_search_fields
from pawchive_app.repository import LocalRepository
from pawchive_app.updater import BASELINE_COMMIT, UpdateManager, _APPLY_HELPER


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ModelTests(unittest.TestCase):
    def test_creator_and_post_are_keyed_with_service(self):
        creator = Creator.from_api({
            "id": "1234", "service": "fanbox", "name": "Sample Creator"
        })
        self.assertEqual(creator.key, "fanbox:1234")
        self.assertEqual(
            creator.profile_url, "https://pawchive.pw/fanbox/user/1234"
        )

        post = Post.from_api({
            "id": "88", "service": "fanbox", "user": "1234",
            "title": "Sample post",
            "file": {"name": "main.zip", "path": "/data/ab/main.zip"},
            "attachments": [{"name": "preview.png", "path": "/data/cd/preview.png"}],
        }, creator)
        self.assertEqual(post.key, "fanbox:1234:88")
        self.assertEqual([item.name for item in post.files], ["main.zip", "preview.png"])

    def test_creator_url_parsing(self):
        self.assertEqual(
            parse_creator_url("https://pawchive.pw/fanbox/user/1234"),
            ("fanbox", "1234"),
        )
        self.assertEqual(
            parse_creator_url("https://pawchive.pw/fanbox/user/1234/post/88"),
            ("fanbox", "1234"),
        )
        with self.assertRaises(ValueError):
            parse_creator_url("https://example.com/fanbox/user/1234")
        with self.assertRaises(ValueError):
            parse_creator_url("https://pawchive.pw/creator/1234")


class ApiTests(unittest.TestCase):
    @patch("pawchive_app.api.urlopen")
    def test_creator_index_parses_api_array(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(json.dumps([
            {"id": "1", "service": "fanbox", "name": "Alice"},
            {"id": "2", "service": "patreon", "name": "Bob"},
        ]).encode())
        client = PawchiveClient(min_interval=1)
        creators = client.search_creators(query="ali", service="fanbox")
        self.assertEqual(len(creators), 1)
        self.assertEqual(creators[0].creator_id, "1")
        request = mock_urlopen.call_args.args[0]
        self.assertIn("/creators", request.full_url)
        self.assertIn("PawchiveModFinder", request.get_header("User-agent"))

    @patch.object(
        PawchiveClient,
        "_browser_get_text",
        return_value='[{"id":"9","service":"fanbox","name":"Browser Fallback"}]',
    )
    @patch(
        "pawchive_app.api.urlopen",
        side_effect=URLError(ConnectionResetError(10054, "connection reset")),
    )
    def test_windows_connection_reset_uses_browser_fallback(self, _mock_urlopen, mock_browser):
        with PawchiveClient() as client:
            creators = client.list_creators()
        self.assertEqual(creators[0].name, "Browser Fallback")
        mock_browser.assert_called_once()

    @patch("pawchive_app.api.urlopen")
    def test_creator_post_route_and_offset(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(json.dumps([
            {"id": "88", "service": "fanbox", "user": "1234", "title": "One"}
        ]).encode())
        posts = PawchiveClient().creator_posts("fanbox", "1234", offset=50)
        self.assertEqual(posts[0].post_id, "88")
        self.assertIn("/fanbox/user/1234?o=50", mock_urlopen.call_args.args[0].full_url)

    @patch("pawchive_app.api.urlopen")
    def test_creator_tags_endpoint_parses_tag_names(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(json.dumps([
            "AI", {"name": "3D"}, {"tag": "AI"}, {"label": "Portrait"}
        ]).encode())
        tags = PawchiveClient().creator_tags("fanbox", "1234")
        self.assertEqual(tags, ["3D", "AI", "Portrait"])
        request = mock_urlopen.call_args.args[0]
        self.assertIn("/fanbox/user/1234/tags", request.full_url)

    @patch("pawchive_app.api.urlopen")
    def test_creator_posts_support_tag_query(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(json.dumps([
            {"id": "88", "service": "fanbox", "user": "1234", "title": "Tagged"}
        ]).encode())
        posts = PawchiveClient().creator_posts("fanbox", "1234", tag="AI")
        self.assertEqual(posts[0].title, "Tagged")
        self.assertIn("tag=AI", mock_urlopen.call_args.args[0].full_url)

    def test_invalid_offset_rejected(self):
        with self.assertRaises(ValueError):
            PawchiveClient().creator_posts("fanbox", "1234", offset=1)

    def test_all_creator_posts_fetches_past_three_pages(self):
        calls = []
        def page(_service, _creator_id, offset=0, query=None, tag=None):
            calls.append((offset, tag))
            count = 50 if offset < 100 else 17
            return [
                Post(service="fanbox", creator_id="1234", post_id=str(offset + index))
                for index in range(count)
            ]

        with patch.object(PawchiveClient, "creator_posts", side_effect=page):
            posts = PawchiveClient().all_creator_posts("fanbox", "1234")

        self.assertEqual(calls, [(0, None), (50, None), (100, None)])
        self.assertEqual(len(posts), 117)

    def test_all_creator_posts_preserves_tag_across_pages(self):
        calls = []
        def page(_service, _creator_id, offset=0, query=None, tag=None):
            calls.append((offset, tag))
            count = 50 if offset == 0 else 3
            return [
                Post(service="fanbox", creator_id="1234", post_id=str(offset + index))
                for index in range(count)
            ]

        with patch.object(PawchiveClient, "creator_posts", side_effect=page):
            posts = PawchiveClient().all_creator_posts("fanbox", "1234", tag="AI")

        self.assertEqual(calls, [(0, "AI"), (50, "AI")])
        self.assertEqual(len(posts), 53)

    def test_all_recent_posts_fetches_until_a_short_page(self):
        calls = []
        def page(query=None, offset=0):
            calls.append(offset)
            count = 50 if offset < 150 else 9
            return [
                Post(service="fanbox", creator_id="creator", post_id=str(offset + index))
                for index in range(count)
            ]

        with patch.object(PawchiveClient, "recent_posts", side_effect=page):
            posts = PawchiveClient().all_recent_posts()

        self.assertEqual(calls, [0, 50, 100, 150])
        self.assertEqual(len(posts), 159)


class RepositoryTests(unittest.TestCase):
    def test_following_feed_and_deduplicated_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalRepository(Path(tmp) / "data.sqlite3")
            creator = Creator(service="fanbox", creator_id="1234", name="Alice")
            repo.save_creator(creator)
            repo.follow("fanbox", "1234")
            post = Post(
                service="fanbox", creator_id="1234", post_id="88",
                title="New post", published="2026-01-01",
                raw={"title": "New post"},
            )
            repo.save_posts([post, post])
            feed = repo.following_feed()
            self.assertEqual(len(feed), 1)
            self.assertEqual(feed[0]["creator_name"], "Alice")
            repo.unfollow("fanbox", "1234")
            self.assertEqual(repo.following_feed(), [])

    def test_following_and_creator_queries_return_all_pages_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalRepository(Path(tmp) / "data.sqlite3")
            repo.save_creator(Creator(service="fanbox", creator_id="1234", name="Alice"))
            repo.follow("fanbox", "1234")
            posts = [
                Post(service="fanbox", creator_id="1234", post_id=str(index),
                     title=f"Post {index}", published=f"2026-01-{index % 28 + 1:02d}")
                for index in range(175)
            ]
            repo.save_posts(posts)

            self.assertEqual(len(repo.following_feed()), 175)
            self.assertEqual(len(repo.posts_for_creator("fanbox", "1234")), 175)
            self.assertEqual(len(repo.following_feed(limit=25)), 25)

    def test_follow_unknown_creator_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalRepository(Path(tmp) / "data.sqlite3")
            with self.assertRaises(KeyError):
                repo.follow("fanbox", "missing")


class DownloadSafetyTests(unittest.TestCase):
    def test_only_dedicated_pawchive_file_host_is_allowed(self):
        self.assertEqual(
            resolve_file_url("/data/ab/example.zip"),
            "https://file.pawchive.pw/data/ab/example.zip",
        )
        self.assertEqual(
            resolve_file_url("/data/ab/example.zip", filename="example.zip"),
            "https://file.pawchive.pw/data/ab/example.zip?f=example.zip",
        )
        with self.assertRaises(DownloadError):
            resolve_file_url("https://evil.example/data/example.zip")
        with self.assertRaises(DownloadError):
            resolve_file_url("/profile/example.zip")

    def test_thumbnail_url_uses_pawchive_thumbnail_host(self):
        self.assertEqual(
            resolve_thumbnail_url("/data/ab/example.jpg"),
            "https://img.pawchive.pw/thumbnail/data/ab/example.jpg?f=example.jpg",
        )
        self.assertEqual(
            resolve_thumbnail_url("https://file.pawchive.pw/data/ab/example.jpg"),
            "https://img.pawchive.pw/thumbnail/data/ab/example.jpg?f=example.jpg",
        )
        self.assertEqual(
            resolve_thumbnail_url("https://img.pawchive.pw/thumbnail/data/ab/example.jpg"),
            "https://img.pawchive.pw/thumbnail/data/ab/example.jpg?f=example.jpg",
        )
        with self.assertRaises(DownloadError):
            resolve_thumbnail_url("https://evil.example/data/image.png")
        with self.assertRaises(DownloadError):
            resolve_thumbnail_url("/data/../secret.jpg")

    def test_thumbnail_url_includes_original_filename(self):
        self.assertEqual(
            resolve_thumbnail_url("/data/ab/image.jpg", filename="original name.jpg"),
            "https://img.pawchive.pw/thumbnail/data/ab/image.jpg?f=original+name.jpg",
        )

    @patch("pawchive_app.downloads._curl_transfer")
    def test_file_download_uses_identified_http1_curl_transport_and_filename(self, mock_curl):
        def save_test_payload(url, destination, timeout, session_cookie=None, **kwargs):
            Path(destination).write_bytes(b"pawchive-test")

        mock_curl.side_effect = save_test_payload
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "original.bin"
            saved = download_file(
                "/data/ab/original.bin",
                target,
                filename="original.bin",
            )
            self.assertEqual(saved.read_bytes(), b"pawchive-test")
            self.assertIn("?f=original.bin", mock_curl.call_args.args[0])

    def test_html_attachment_path_maps_to_thumbnail_host_with_filename(self):
        self.assertEqual(
            pawchive_image_url("/f6/bb/image.jpg", "preview image.jpg"),
            "https://img.pawchive.pw/thumbnail/data/f6/bb/image.jpg?f=preview+image.jpg",
        )
        self.assertEqual(
            pawchive_image_url("https://pawchive.pw/data/f6/bb/image.jpg", "image.jpg"),
            "https://img.pawchive.pw/thumbnail/data/f6/bb/image.jpg?f=image.jpg",
        )

    def test_thumbnail_fetch_uses_curl_and_reuses_cache(self):
        url = "https://img.pawchive.pw/thumbnail/data/test/cache-image-v2.jpg?f=cache-image-v2.jpg"
        with patch("pawchive_app.downloads._curl_transfer") as curl_transfer:
            curl_transfer.side_effect = lambda _url, destination, _timeout, session_cookie=None, **kwargs: Path(destination).write_bytes(b"thumbnail")
            result = fetch_thumbnail_bytes(url)
            again = fetch_thumbnail_bytes(url)
        self.assertEqual(result, b"thumbnail")
        self.assertEqual(again, b"thumbnail")
        curl_transfer.assert_called_once()

    def test_thumbnail_fetch_persists_cache_between_sessions(self):
        url = "https://img.pawchive.pw/thumbnail/data/test/disk-cache-speed-v3.jpg?f=disk-cache-speed-v3.jpg"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
            with patch("pawchive_app.downloads._curl_transfer") as transfer:
                transfer.side_effect = lambda _url, destination, _timeout, **kwargs: Path(destination).write_bytes(b"cached-thumbnail")
                self.assertEqual(fetch_thumbnail_bytes(url), b"cached-thumbnail")
                transfer.assert_called_once()

            key = downloads._thumbnail_cache_key(url, None)
            cache_path = downloads._thumbnail_cache_path(key)
            self.assertTrue(cache_path.is_file())

            with downloads._THUMB_CACHE_LOCK:
                removed = downloads._THUMB_CACHE.pop(key, None)
                if removed is not None:
                    downloads._THUMB_CACHE_SIZE -= len(removed)
            with patch("pawchive_app.downloads._curl_transfer", side_effect=AssertionError("disk cache should be used")):
                self.assertEqual(fetch_thumbnail_bytes(url), b"cached-thumbnail")

    def test_simultaneous_thumbnail_requests_share_one_transfer(self):
        url = "https://img.pawchive.pw/thumbnail/data/test/shared-thumbnail-v3.jpg?f=shared-thumbnail-v3.jpg"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
            with patch("pawchive_app.downloads._curl_transfer") as transfer:
                def save_payload(_url, destination, _timeout, **kwargs):
                    time.sleep(0.2)
                    Path(destination).write_bytes(b"shared-thumbnail")
                transfer.side_effect = save_payload
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _index: fetch_thumbnail_bytes(url), range(2)))
                self.assertEqual(results, [b"shared-thumbnail", b"shared-thumbnail"])
                transfer.assert_called_once()

    @patch("pawchive_app.downloads._wait_download_slot")
    @patch("pawchive_app.downloads.subprocess.run")
    @patch("pawchive_app.downloads._find_curl", return_value="curl.exe")
    def test_thumbnail_transfer_skips_file_host_request_gate(
        self, _mock_curl, mock_run, mock_request_gate
    ):
        def fake_run(command, **_kwargs):
            header_path = Path(command[command.index("-D") + 1])
            output_path = Path(command[command.index("-o") + 1])
            header_path.write_text(
                "HTTP/1.1 200 OK\r\nContent-Length: 6\r\n",
                encoding="ascii",
            )
            output_path.write_bytes(b"abcdef")

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        mock_run.side_effect = fake_run
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "thumbnail.bin"
            from pawchive_app.downloads import _curl_transfer
            _curl_transfer(
                "https://img.pawchive.pw/thumbnail/data/ab/image.jpg?f=image.jpg",
                target,
                30,
                resume=False,
                attempts=1,
            )
            self.assertEqual(target.read_bytes(), b"abcdef")
        mock_request_gate.assert_not_called()

    @patch("pawchive_app.downloads.subprocess.run")
    @patch("pawchive_app.downloads.shutil.which", return_value="curl.exe")
    def test_curl_transport_uses_http11_and_identifiable_user_agent(
        self, _mock_which, mock_run
    ):
        def fake_run(command, **_kwargs):
            header_path = Path(command[command.index("-D") + 1])
            output_path = Path(command[command.index("-o") + 1])
            header_path.write_text(
                "HTTP/1.1 200 OK\r\nContent-Length: 6\r\n",
                encoding="ascii",
            )
            output_path.write_bytes(b"abcdef")

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        mock_run.side_effect = fake_run
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "curl.bin"
            from pawchive_app.downloads import _curl_transfer
            _curl_transfer(
                "https://file.pawchive.pw/data/ab/curl.bin?f=curl.bin",
                target,
                60,
                resume=False,
            )
            self.assertEqual(target.read_bytes(), b"abcdef")
        command = mock_run.call_args.args[0]
        self.assertIn("--http1.1", command)
        self.assertIn("-4", command)
        self.assertTrue(any(str(item).startswith("User-Agent: PawchiveModFinder/") for item in command))
        self.assertIn("-f", command)

    @patch("pawchive_app.downloads.subprocess.run")
    @patch("pawchive_app.downloads.shutil.which", return_value="curl.exe")
    def test_curl_376_byte_antibot_placeholder_is_not_saved(self, _mock_which, mock_run):
        def fake_run(command, **_kwargs):
            header_path = Path(command[command.index("-D") + 1])
            output_path = Path(command[command.index("-o") + 1])
            header_path.write_text(
                "HTTP/1.1 200 OK\r\nContent-Length: 376\r\n",
                encoding="ascii",
            )
            output_path.write_bytes(b"x" * 376)

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        mock_run.side_effect = fake_run
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "blocked.bin"
            from pawchive_app.downloads import _curl_transfer
            with self.assertRaisesRegex(DownloadError, "376바이트"):
                _curl_transfer(
                    "https://file.pawchive.pw/data/ab/blocked.bin?f=blocked.bin",
                    target,
                    60,
                    resume=False,
                )
            self.assertFalse(target.exists())

    @patch("pawchive_app.downloads._browser_transfer")
    @patch("pawchive_app.downloads.subprocess.run")
    @patch("pawchive_app.downloads._find_curl", return_value="curl.exe")
    def test_curl_connection_reset_falls_back_to_identified_browser_transport(
        self, _mock_curl, mock_run, mock_browser
    ):
        class Result:
            returncode = 35
            stdout = ""
            stderr = "curl: (35) Recv failure: Connection was reset"

        mock_run.return_value = Result()

        def save_test_payload(url, destination, timeout, session_cookie=None):
            Path(destination).write_bytes(b"browser-payload")

        mock_browser.side_effect = save_test_payload
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fallback.bin"
            _curl_transfer(
                "https://file.pawchive.pw/data/ab/fallback.bin?f=fallback.bin",
                target,
                10,
                resume=False,
                attempts=1,
            )
            self.assertEqual(target.read_bytes(), b"browser-payload")
        mock_browser.assert_called_once()
        self.assertEqual(mock_browser.call_args.args[0],
                         "https://file.pawchive.pw/data/ab/fallback.bin?f=fallback.bin")

    def test_zip_extracts_inside_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "safe.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("folder/mod.txt", "safe")
            output = Path(tmp) / "out"
            extracted = safe_extract_zip(archive, output)
            self.assertEqual(len(extracted), 1)
            self.assertEqual((output / "folder" / "mod.txt").read_text(), "safe")

    def test_zip_traversal_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("../escape.txt", "unsafe")
            with self.assertRaises(DownloadError):
                safe_extract_zip(archive, Path(tmp) / "out")
            self.assertFalse((Path(tmp) / "escape.txt").exists())

    def test_tar_extraction_and_traversal_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "safe.tar.gz"
            source_file = Path(tmp) / "source.txt"
            source_file.write_text("safe")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(source_file, arcname="nested/source.txt")
            output = Path(tmp) / "out"
            extracted = safe_extract_archive(archive, output)
            self.assertEqual(len(extracted), 1)
            self.assertEqual((output / "nested" / "source.txt").read_text(), "safe")

    def test_tar_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "unsafe.tar"
            source_file = Path(tmp) / "payload.txt"
            source_file.write_text("unsafe")
            with tarfile.open(archive, "w") as tar:
                info = tarfile.TarInfo("../escape.txt")
                info.size = source_file.stat().st_size
                with source_file.open("rb") as payload:
                    tar.addfile(info, payload)
            with self.assertRaises(DownloadError):
                safe_extract_archive(archive, Path(tmp) / "out")
            self.assertFalse((Path(tmp) / "escape.txt").exists())


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = UpdateManager(root / "roaming", root / "local")

    def tearDown(self):
        self.temp.cleanup()

    def _make_archive(self, extra_entries=None):
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

    def test_check_detects_new_branch_commit(self):
        latest = "a" * 40
        with patch.object(
            self.manager,
            "_request_bytes",
            return_value=json.dumps({"sha": latest}).encode("utf-8"),
        ) as request:
            result = self.manager.check()
        self.assertEqual(result.installed_commit, BASELINE_COMMIT)
        self.assertEqual(result.latest_commit, latest)
        self.assertTrue(result.available)
        self.assertIn("feature%2Fpawchive-mod-finder-v2", request.call_args.args[0])

    def test_check_reports_up_to_date_after_commit_marker_is_written(self):
        latest = "b" * 40
        self.manager.installed_commit_path.write_text(latest, encoding="ascii")
        with patch.object(
            self.manager,
            "_request_bytes",
            return_value=json.dumps({"sha": latest}).encode("utf-8"),
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
        payload = self._make_archive({"tests/test_example.py": "pass"})
        with patch.object(self.manager, "_request_bytes", return_value=payload):
            package = self.manager.prepare("c" * 40)
        self.assertTrue((package.staged_app / "main_v2.py").is_file())
        self.assertTrue((package.staged_app / "pawchive_app" / "updater.py").is_file())
        self.assertTrue((package.staged_app / "tests" / "test_example.py").is_file())
        self.assertFalse((package.staged_app.parent / "outside.txt").exists())

    def test_prepare_rejects_path_traversal(self):
        payload = self._make_archive({"../outside.txt": "not allowed"})
        with patch.object(self.manager, "_request_bytes", return_value=payload):
            with self.assertRaisesRegex(RuntimeError, "안전하지 않은 파일 경로"):
                self.manager.prepare("d" * 40)

    def test_prepare_rejects_unexpected_archive_layout(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("wrong-root/main_v2.py", "print('no')")
        with patch.object(self.manager, "_request_bytes", return_value=buffer.getvalue()):
            with self.assertRaisesRegex(RuntimeError, "GameModFinder 폴더"):
                self.manager.prepare("e" * 40)

    def test_windows_update_helper_is_valid_python(self):
        compile(_APPLY_HELPER, "apply_update.py", "exec")


if __name__ == "__main__":
    unittest.main()


class SearchFieldScopeTests(unittest.TestCase):
    def setUp(self):
        self.post = Post.from_api({
            "id": "search-1",
            "service": "fanbox",
            "user": "creator-1",
            "title": "Raiden Shogun Outfit",
            "content": "<p>New texture pack for Furina</p>",
        })

    def test_title_only_uses_title(self):
        self.assertTrue(post_matches_search_fields(
            self.post, "Raiden Shogun", include_title=True, include_content=False
        ))
        self.assertFalse(post_matches_search_fields(
            self.post, "Furina", include_title=True, include_content=False
        ))

    def test_content_only_uses_body(self):
        self.assertTrue(post_matches_search_fields(
            self.post, "Furina texture", include_title=False, include_content=True
        ))
        self.assertFalse(post_matches_search_fields(
            self.post, "Raiden", include_title=False, include_content=True
        ))

    def test_both_fields_preserve_combined_search(self):
        self.assertTrue(post_matches_search_fields(
            self.post, "Shogun Furina", include_title=True, include_content=True
        ))
        self.assertFalse(post_matches_search_fields(
            self.post, "Furina", include_title=True, include_content=False
        ))


class ArchiveIniInspectionTests(unittest.TestCase):
    def test_zip_ini_is_found_without_extracting(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "mod.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("Character/Character.ini", "[Constants]")
                zipped.writestr("README.txt", "Install guide")
            result = analyze_archive_for_ini(archive)
            self.assertEqual(result.archive_format, "ZIP")
            self.assertEqual(result.member_count, 2)
            self.assertEqual(result.ini_count, 1)
            self.assertEqual(result.ini_files, ("Character/Character.ini",))
            self.assertEqual(set(Path(tmp).iterdir()), {archive})

    def test_ini_extension_check_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "mod.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("mods/CONFIG.INI", "data")
            result = analyze_archive_for_ini(archive)
            self.assertEqual(result.ini_count, 1)

    def test_windows_backslash_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr(r"..\\outside.ini", "bad")
            with self.assertRaises(ArchiveInspectionError):
                analyze_archive_for_ini(archive)

    def test_unsafe_member_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("../outside.ini", "bad")
            with self.assertRaises(ArchiveInspectionError):
                analyze_archive_for_ini(archive)

    def test_tar_ini_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "mod.tar.gz"
            source = Path(tmp) / "sample.ini"
            source.write_text("[General]", encoding="utf-8")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(source, arcname="Game/sample.ini")
            result = analyze_archive_for_ini(archive)
            self.assertEqual(result.archive_format, "TAR")
            self.assertEqual(result.ini_count, 1)


class ModCreatorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = LocalRepository(Path(self.temp.name) / "test.sqlite3")
        self.post = Post.from_api({
            "id": "post-ini-1",
            "service": "fanbox",
            "user": "creator-ini",
            "title": "Character mod pack",
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_ini_evidence_is_grouped_by_creator_and_is_idempotent(self):
        self.repository.save_mod_creator_evidence(
            self.post, "character.zip", ["Game/Character.ini"], 1
        )
        self.repository.save_mod_creator_evidence(
            self.post, "character.zip", ["Game/Character.ini"], 1
        )
        candidates = self.repository.mod_creator_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["service"], "fanbox")
        self.assertEqual(candidates[0]["creator_id"], "creator-ini")
        self.assertEqual(candidates[0]["evidence_count"], 1)
        evidence = self.repository.mod_creator_evidence_for_creator(
            "fanbox", "creator-ini"
        )
        self.assertEqual(len(evidence), 1)
        self.assertIn("Character.ini", evidence[0]["ini_files_json"])
