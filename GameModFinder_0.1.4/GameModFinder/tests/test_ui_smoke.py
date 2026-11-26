import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pawchive_app import ui
from pawchive_app.enhancements import install_ui_enhancements


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
        window.close()


if __name__ == "__main__":
    unittest.main()
