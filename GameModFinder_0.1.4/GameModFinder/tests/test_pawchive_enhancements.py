from __future__ import annotations

import unittest

from pawchive_app.downloads import DownloadError, resolve_file_url
from pawchive_app.enhancements import pawchive_image_url, render_post_html
from pawchive_app.models import Attachment


class PawchivePathTests(unittest.TestCase):
    def test_api_relative_paths_receive_data_prefix(self):
        self.assertEqual(
            resolve_file_url("/f6/bb/archive.zip"),
            "https://file.pawchive.pw/data/f6/bb/archive.zip",
        )
        self.assertEqual(
            resolve_file_url("f6/bb/archive.zip"),
            "https://file.pawchive.pw/data/f6/bb/archive.zip",
        )

    def test_existing_data_prefix_is_not_duplicated(self):
        self.assertEqual(
            resolve_file_url("/data/f6/bb/archive.zip"),
            "https://file.pawchive.pw/data/f6/bb/archive.zip",
        )

    def test_filename_is_added_as_pawchive_file_parameter(self):
        self.assertEqual(
            resolve_file_url("/f6/bb/archive.zip", filename="my archive.zip"),
            "https://file.pawchive.pw/data/f6/bb/archive.zip?f=my+archive.zip",
        )

    def test_absolute_file_host_url_is_normalized(self):
        self.assertEqual(
            resolve_file_url("https://file.pawchive.pw/f6/bb/archive.zip"),
            "https://file.pawchive.pw/data/f6/bb/archive.zip",
        )

    def test_traversal_and_untrusted_hosts_are_rejected(self):
        for value in ("/../secret.zip", "/f6/%2e%2e/secret.zip"):
            with self.subTest(value=value):
                with self.assertRaises(DownloadError):
                    resolve_file_url(value)
        with self.assertRaises(DownloadError):
            resolve_file_url("https://example.com/data/file.zip")


class PostRenderingTests(unittest.TestCase):
    def test_attachment_thumbnail_uses_img_host_and_data_prefix(self):
        self.assertEqual(
            pawchive_image_url("/f6/bb/abc.jpg", "preview.jpg"),
            "https://img.pawchive.pw/thumbnail/data/f6/bb/abc.jpg?f=preview.jpg",
        )

    def test_post_markdown_keeps_formatting_and_embeds_images(self):
        post_html = render_post_html(
            "# Update\n\n**Important** text\n\n- first\n- second",
            [Attachment(name="preview.png", path="/f6/bb/abc.png")],
        )
        self.assertIn("<h1>Update</h1>", post_html)
        self.assertIn("<strong>Important</strong>", post_html)
        self.assertIn("<ul>", post_html)
        self.assertIn("https://img.pawchive.pw/thumbnail/data/f6/bb/abc.png?f=preview.png", post_html)
        self.assertNotIn("<figcaption>", post_html)
        self.assertNotIn("preview.png</figcaption>", post_html)
        self.assertIn("font-size:12pt", post_html)

    def test_html_sanitizer_removes_scripts_but_keeps_markup(self):
        post_html = render_post_html("<p><strong>Hello</strong></p><script>alert(1)</script>")
        self.assertIn("<strong>Hello</strong>", post_html)
        self.assertNotIn("alert(1)", post_html)
        self.assertNotIn("<script", post_html)


if __name__ == "__main__":
    unittest.main()
