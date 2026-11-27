from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pawchive_app.creator_discovery import (
    parse_search_queries,
    scan_mod_creator_candidates,
)
from pawchive_app.models import Post


def make_zip(entries: dict[str, str]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return stream.getvalue()


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def recent_posts(self, query, offset=0):
        self.calls.append((query, offset))
        return self.pages.get((query, offset), [])


class FakeRepository:
    def __init__(self):
        self.evidence = []

    def save_mod_creator_evidence(self, post, archive_name, ini_files, ini_count):
        self.evidence.append((post.key, archive_name, tuple(ini_files), ini_count))


class CreatorDiscoveryTests(unittest.TestCase):
    def test_search_queries_split_and_deduplicate_without_splitting_phrases(self):
        self.assertEqual(
            parse_search_queries("Tifa Lockhart, 2B; tifa lockhart\nRaiden Shogun"),
            ["Tifa Lockhart", "2B", "Raiden Shogun"],
        )

    def test_scanner_checks_archive_members_and_groups_only_matching_creators(self):
        ini_post = Post.from_api({
            "id": "1",
            "service": "fanbox",
            "user": "creator-ini",
            "title": "Tifa Lockhart outfit",
            "content": "new release",
            "file": {"name": "tifa-mod.zip", "path": "/data/aa/tifa-mod.zip"},
        })
        content_only_post = Post.from_api({
            "id": "2",
            "service": "patreon",
            "user": "creator-content",
            "title": "New outfit bundle",
            "content": "Tifa Lockhart character mod",
            "file": {"name": "bundle.zip", "path": "/data/bb/bundle.zip"},
        })
        pages = {
            ("Tifa Lockhart", 0): [ini_post] * 50,
            ("Tifa Lockhart", 50): [content_only_post],
        }
        client = FakeClient(pages)
        repository = FakeRepository()
        payload = make_zip({"Tifa/Tifa.ini": "[Constants]", "README.txt": "info"})

        def fake_download(_path, destination, **_kwargs):
            target = Path(destination)
            target.write_bytes(payload)
            return target

        with tempfile.TemporaryDirectory() as temporary, patch(
            "pawchive_app.creator_discovery.download_file",
            side_effect=fake_download,
        ) as downloader:
            result = scan_mod_creator_candidates(
                client,
                repository,
                ["Tifa Lockhart"],
                page_limit=3,
                include_title=True,
                include_content=False,
                temporary_root=temporary,
            )

        self.assertEqual(client.calls, [("Tifa Lockhart", 0), ("Tifa Lockhart", 50)])
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(result.archive_posts_found, 1)
        self.assertEqual(result.archives_scanned, 1)
        self.assertEqual(result.ini_archives_found, 1)
        self.assertEqual(result.creators_found, 1)
        self.assertEqual(len(repository.evidence), 1)
        self.assertEqual(repository.evidence[0][0], "fanbox:creator-ini:1")
        self.assertEqual(repository.evidence[0][2], ("Tifa/Tifa.ini",))
        downloader.assert_called_once()

    def test_scanner_can_search_content_only_and_continues_after_bad_archive(self):
        post = Post.from_api({
            "id": "3",
            "service": "fanbox",
            "user": "creator-bad",
            "title": "New character release",
            "content": "2B outfit",
            "file": {"name": "bad.zip", "path": "/data/cc/bad.zip"},
        })
        client = FakeClient({("2B", 0): [post]})
        repository = FakeRepository()

        with tempfile.TemporaryDirectory() as temporary, patch(
            "pawchive_app.creator_discovery.download_file",
            side_effect=OSError("network unavailable"),
        ):
            result = scan_mod_creator_candidates(
                client,
                repository,
                ["2B"],
                page_limit=1,
                include_title=False,
                include_content=True,
                temporary_root=temporary,
            )

        self.assertEqual(result.archive_posts_found, 1)
        self.assertEqual(result.archives_scanned, 1)
        self.assertEqual(result.ini_archives_found, 0)
        self.assertEqual(result.creators_found, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(repository.evidence, [])

    def test_scanner_requires_at_least_one_field_scope(self):
        with self.assertRaisesRegex(ValueError, "제목 또는 내용을"):
            scan_mod_creator_candidates(
                FakeClient({}),
                FakeRepository(),
                ["Tifa"],
                include_title=False,
                include_content=False,
            )


if __name__ == "__main__":
    unittest.main()
