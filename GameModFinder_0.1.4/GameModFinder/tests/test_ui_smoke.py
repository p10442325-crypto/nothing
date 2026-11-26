import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pawchive_app import ui
from pawchive_app.enhancements import install_ui_enhancements
from pawchive_app.models import Post


install_ui_enhancements(ui)
MainWindow = ui.MainWindow


class DesktopSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_exposes_expected_navigation(self):
        window = MainWindow()
        self.assertEqual(
            [window.tabs.tabText(i) for i in range(window.tabs.count())],
            ["팔로잉", "크리에이터", "크리에이터 찾기", "전체 탐색", "다운로드", "설정"],
        )
        self.assertTrue(hasattr(window.following_browser.content, "setHtml"))
        self.assertTrue(hasattr(window.following_browser, "image_preview"))
        self.assertTrue(hasattr(window.following_browser, "_thumbnail_workers"))
        window.close()

    def test_post_browser_opens_full_width_detail_and_returns_to_title_list(self):
        browser = ui.PostBrowser()
        post = Post.from_api({
            "id": "post-1",
            "service": "fanbox",
            "user": "creator-1",
            "title": "제목만 보이는 게시물",
            "content": "본문 전체 내용",
        })
        browser.set_posts([post])

        self.assertEqual(browser.stack.currentWidget(), browser.list_page)
        self.assertEqual(browser.items.item(0).text(), "제목만 보이는 게시물")

        browser.items.itemClicked.emit(browser.items.item(0))
        self.assertEqual(browser.stack.currentWidget(), browser.detail_page)
        self.assertEqual(browser.title.text(), "제목만 보이는 게시물")

        browser.back_button.click()
        self.assertEqual(browser.stack.currentWidget(), browser.list_page)
        browser.close()


if __name__ == "__main__":
    unittest.main()
