from __future__ import annotations

import io
import json
import tempfile
import tarfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pawchive_app.api import PawchiveClient
from pawchive_app.downloads import DownloadError, resolve_file_url, safe_extract_zip, safe_extract_archive
from pawchive_app.models import Creator, Post, parse_creator_url
from pawchive_app.repository import LocalRepository


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
        self.assertIn("GameModFinder-Pawchive", request.get_header("User-agent"))

    @patch("pawchive_app.api.urlopen")
    def test_creator_post_route_and_offset(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(json.dumps([
            {"id": "88", "service": "fanbox", "user": "1234", "title": "One"}
        ]).encode())
        posts = PawchiveClient().creator_posts("fanbox", "1234", offset=50)
        self.assertEqual(posts[0].post_id, "88")
        self.assertIn("/fanbox/user/1234/posts?o=50", mock_urlopen.call_args.args[0].full_url)

    def test_invalid_offset_rejected(self):
        with self.assertRaises(ValueError):
            PawchiveClient().creator_posts("fanbox", "1234", offset=1)


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
        with self.assertRaises(DownloadError):
            resolve_file_url("https://evil.example/data/example.zip")
        with self.assertRaises(DownloadError):
            resolve_file_url("/profile/example.zip")

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


if __name__ == "__main__":
    unittest.main()
