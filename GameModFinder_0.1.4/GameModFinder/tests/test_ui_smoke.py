import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pawchive_app import ui
from pawchive_app.enhancements import install_ui_enhancements
from pawchive_app.models import Creator, Post


install_ui_enhancements(ui)
MainWindow = ui.MainWindow


class DesktopSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.image_queue_patch = patch(
            "pawchive_app.enhancements.PostTextBrowser._pump_image_queue"
        )
        cls.image_queue_patch.start()
        cls.addClassCleanup(cls.image_queue_patch.stop)

    def test_main_window_exposes_expected_navigation(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"APPDATA": temp_dir, "LOCALAPPDATA": temp_dir}
        ):
            with patch.object(ui.MainWindow, "_load_creators"), patch.object(
                ui.MainWindow, "_load_following_local"
            ):
                window = MainWindow()
            self.assertEqual(
                [window.tabs.tabText(i) for i in range(window.tabs.count())],
                ["팔로잉", "크리에이터", "크리에이터 찾기", "전체 탐색", "제작자 후보", "다운로드", "설정"],
            )
            self.assertTrue(hasattr(window.following_browser.content, "setHtml"))
            self.assertTrue(hasattr(window.following_browser, "image_preview"))
            self.assertTrue(hasattr(window.following_browser, "_thumbnail_workers"))
            self.assertTrue(hasattr(window, "creator_tag_combo"))
            self.assertEqual(window.creator_tag_combo.itemText(0), "전체 게시물")
            self.assertTrue(window.explore_search_title.isChecked())
            self.assertTrue(window.explore_search_content.isChecked())
            self.assertFalse(hasattr(window.explore_browser, "analyze_requested"))
            self.assertEqual(window.mod_candidate_list.count(), 0)
            self.assertTrue(hasattr(window, "mod_scan_queries"))
            self.assertEqual(window.mod_scan_pages.value(), 1)
            self.assertIsNone(window.findChild(ui.QFrame, "Header"))
            self.assertEqual(window.settings_page.check_updates_button.text(), "업데이트 확인")
            self.assertEqual(window.settings_page.apply_update_button.text(), "업데이트 적용 후 재시작")
            self.assertFalse(window.settings_page.apply_update_button.isEnabled())
            window.close()
            self.app.processEvents()

    def test_startup_does_not_lock_ui_with_an_automatic_roster_sync(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"APPDATA": temp_dir, "LOCALAPPDATA": temp_dir}
        ):
            with patch.object(ui.MainWindow, "_load_creators"), patch.object(
                ui.MainWindow, "_load_following_local"
            ), patch.object(ui.QTimer, "singleShot") as single_shot:
                window = MainWindow()
            single_shot.assert_not_called()
            self.assertTrue(window.sync_timer.isActive())
            window.close()
            self.app.processEvents()

    def test_post_browser_opens_full_width_detail_and_returns_to_title_list(self):
        browser = ui.PostBrowser()
        image_only = Post.from_api({
            "id": "post-image-only",
            "service": "fanbox",
            "user": "creator-1",
            "title": "이미지만 있는 게시물",
            "content": "이미지 게시물",
            "file": {"name": "preview.jpg", "path": "/data/cd/preview.jpg"},
        })
        post = Post.from_api({
            "id": "post-1",
            "service": "fanbox",
            "user": "creator-1",
            "title": "제목만 보이는 게시물",
            "content": "본문 전체 내용",
            "file": {"name": "sample-mod.zip", "path": "/data/ab/sample-mod.zip"},
            "attachments": [
                {"name": "preview.jpg", "path": "/data/cd/preview.jpg"},
                {"name": "readme.txt", "path": "/data/ef/readme.txt"},
            ],
        })
        rar_post = Post.from_api({
            "id": "post-rar",
            "service": "fanbox",
            "user": "creator-1",
            "title": "RAR 첨부 게시물",
            "file": {"name": "preview.jpg", "path": "/data/gh/preview.jpg"},
            "attachments": [
                {"name": "character-pack.rar", "path": None, "deferred": True},
            ],
        })
        with patch.object(ui.PostBrowser, "_thumbnail_url_for_post", return_value=""):
            browser.set_posts([image_only, post, rar_post])

        self.assertEqual(browser.stack.currentWidget(), browser.list_page)
        self.assertEqual(len(browser.posts), 2)
        self.assertEqual(browser.items.count(), 2)
        self.assertEqual(browser.items.item(0).text(), "제목만 보이는 게시물")
        self.assertEqual(browser.items.item(1).text(), "RAR 첨부 게시물")
        browser._populate_attachment_cards(post)
        self.assertFalse(hasattr(browser, "analysis_buttons"))
        self.assertEqual(browser.items.viewMode(), ui.QListView.IconMode)
        self.assertEqual(browser.items.verticalScrollMode(), ui.QListView.ScrollPerPixel)
        self.assertEqual(browser.items.verticalScrollBar().singleStep(), 24)
        browser.resize(1200, 760)
        browser._update_card_grid()
        self.assertGreater(browser.items.gridSize().width(), 200)
        self.assertEqual(browser.items.spacing(), 0)
        self.assertEqual(
            browser.items.item(0).sizeHint().width(),
            browser.items.gridSize().width(),
        )
        self.assertTrue(browser.search_input.isEnabled())
        browser.resize(1200, 760)
        browser.show()
        self.app.processEvents()
        search_geometry = browser.search_input.geometry()

        browser.search_input.setText("sample-mod.zip")
        self.assertFalse(browser.items.item(0).isHidden())
        self.assertIn("1", browser.list_count.text())
        browser.search_input.setText("not-a-real-post")
        self.assertTrue(browser.items.item(0).isHidden())
        self.assertEqual(browser.list_empty.text(), "검색 결과가 없습니다.")
        self.assertEqual(browser.list_content.currentWidget(), browser.list_empty)
        self.app.processEvents()
        self.assertEqual(browser.search_input.geometry(), search_geometry)
        browser.search_input.clear()
        self.assertEqual(browser.list_content.currentWidget(), browser.items)
        self.assertFalse(browser.items.item(0).isHidden())

        browser.items.itemClicked.emit(browser.items.item(0))
        self.assertEqual(browser.stack.currentWidget(), browser.detail_page)
        self.assertEqual(browser.title.text(), "제목만 보이는 게시물")
        self.assertTrue(browser.title.isHidden())
        self.assertTrue(browser.meta.isHidden())
        self.assertEqual(len(browser.attachment_buttons), 1)
        self.assertTrue(all(button.text() == "다운로드" for button in browser.attachment_buttons))
        self.assertFalse(hasattr(browser, "files"))

        browser.back_button.click()
        self.assertEqual(browser.stack.currentWidget(), browser.list_page)
        browser.close()


    def test_creator_selection_does_not_start_bulk_metadata_hydration(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"APPDATA": temp_dir, "LOCALAPPDATA": temp_dir}
        ):
            with patch.object(ui.MainWindow, "_load_creators"), patch.object(
                ui.MainWindow, "_load_following_local"
            ):
                window = MainWindow()

            creator = Creator(service="fanbox", creator_id="creator-rar", name="RAR Creator")
            window.database.save_creator(creator)
            window.database.follow(creator.service, creator.creator_id)
            window.database.save_posts([Post.from_api({
                "id": "cached-deferred-rar",
                "service": "fanbox",
                "user": creator.creator_id,
                "title": "Cached deferred RAR",
                "attachments": [
                    {"name": "sample.rar", "deferred": True},
                ],
            })])
            item = ui.QListWidgetItem("RAR Creator")
            item.setData(ui.Qt.UserRole, (creator.service, creator.creator_id))
            window.creator_list.addItem(item)

            with patch.object(window, "_load_creator_tags"), patch.object(
                window, "_run_task"
            ) as run_task:
                window._creator_selected(0)

            run_task.assert_not_called()
            self.assertEqual(len(window.creator_browser.posts), 1)
            self.assertEqual(window.creator_browser.posts[0].files[0].name, "sample.rar")
            window.close()
            self.app.processEvents()

    def test_post_browser_shows_mega_link_post_as_external_link_card(self):
        url = "https://mega.nz/file/BzcjlbzZ#uUyY2jXxQ8eg5bhf111h46KPwnkU5Dwtjh_jBLiyYYw"
        post = Post.from_api({
            "id": "101845109",
            "service": "patreon",
            "user": "124138802",
            "title": "MEGA 링크 게시물",
            "content": f'<a href="{url}">Download</a>',
        })
        browser = ui.PostBrowser()
        with patch.object(ui.PostBrowser, "_thumbnail_url_for_post", return_value=""):
            browser.set_posts([post])
        self.assertEqual(len(browser.posts), 1)
        self.assertEqual(browser.list_count.text(), "1개")
        browser._populate_attachment_cards(post)
        self.assertEqual(len(browser.attachment_buttons), 1)
        self.assertEqual(browser.attachment_buttons[0].text(), "링크 열기")
        browser.close()

    def test_post_browser_appends_next_page_without_resetting_local_search(self):
        first = Post.from_api({
            "id": "page-1",
            "service": "fanbox",
            "user": "creator-1",
            "title": "첫 번째 페이지 게시물",
            "file": {"name": "first.zip", "path": "/data/aa/first.zip"},
        })
        second = Post.from_api({
            "id": "page-2",
            "service": "fanbox",
            "user": "creator-1",
            "title": "두 번째 페이지 게시물",
            "file": {"name": "second.zip", "path": "/data/bb/second.zip"},
        })
        browser = ui.PostBrowser()
        with patch.object(ui.PostBrowser, "_thumbnail_url_for_post", return_value=""):
            browser.set_posts([first])
            browser.set_pagination(True)
            self.assertFalse(browser.load_more_button.isHidden())
            browser.search_input.setText("두 번째")
            browser.append_posts([second, second])

        self.assertEqual(len(browser.posts), 2)
        self.assertEqual(browser.search_input.text(), "두 번째")
        self.assertTrue(browser.items.item(0).isHidden())
        self.assertFalse(browser.items.item(1).isHidden())

        requested = []
        browser.load_more_requested.connect(lambda: requested.append(True))
        browser.load_more_button.click()
        self.assertEqual(requested, [True])

        browser.set_pagination(False)
        self.assertTrue(browser.load_more_button.isHidden())
        browser.close()


if __name__ == "__main__":
    unittest.main()
