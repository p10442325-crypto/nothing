from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QPlainTextEdit, QProgressBar,
    QSpinBox, QSplitter, QVBoxLayout, QWidget, QTabWidget,
)

from gamemodfinder.api import PawchiveClient
from gamemodfinder.config import app_data_dir, load_aliases, load_settings, save_settings
from gamemodfinder.database import FinderDatabase
from gamemodfinder.search import SearchService


APP_STYLE = """
QWidget { background: #11151d; color: #e8edf5; font-family: 'Segoe UI', 'Malgun Gothic'; font-size: 10pt; }
QMainWindow { background: #0d1118; }
QFrame#Header { background: #171e2a; border: 1px solid #273244; border-radius: 14px; }
QFrame#Panel { background: #151c27; border: 1px solid #273244; border-radius: 12px; }
QLabel#HeroTitle { font-size: 22pt; font-weight: 700; color: #f4f7fb; }
QLabel#Muted { color: #9ba9bd; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #0e141d; border: 1px solid #334156; border-radius: 8px; padding: 8px; selection-background-color: #466a9c; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #73a5e8; }
QPushButton { background: #25364d; border: 1px solid #3b5373; border-radius: 8px; padding: 8px 13px; font-weight: 600; }
QPushButton:hover { background: #304866; }
QPushButton:disabled { color: #7d8999; background: #1c2633; }
QPushButton#Primary { background: #3974c6; border-color: #548bd7; color: white; padding: 10px 22px; }
QPushButton#Primary:hover { background: #4685dc; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { background: #151d29; border: 1px solid #2a394d; border-radius: 10px; padding: 10px; margin: 4px 1px; }
QListWidget::item:selected { background: #20334b; border: 1px solid #6d9bd2; }
QTabWidget::pane { border: 1px solid #2a394d; border-radius: 8px; background: #151c27; }
QTabBar::tab { background: #171e2a; padding: 8px 14px; margin-right: 3px; border-top-left-radius: 7px; border-top-right-radius: 7px; }
QTabBar::tab:selected { background: #2b405c; }
QCheckBox { spacing: 7px; color: #c4cfde; }
QProgressBar { border: 1px solid #334156; border-radius: 6px; background: #0e141d; height: 12px; text-align: center; }
QProgressBar::chunk { background: #4789d8; border-radius: 5px; }
"""


class SearchWorker(QObject):
    progress = Signal(str)
    finished = Signal(object, object)

    def __init__(self, query: str, game_key: str, options: dict[str, Any], aliases: dict[str, Any], settings: dict[str, Any]) -> None:
        super().__init__()
        self.query = query
        self.game_key = game_key
        self.options = options
        self.aliases = aliases
        self.settings = settings

    def run(self) -> None:
        client = PawchiveClient(
            self.settings["api_base"],
            timeout=self.settings.get("request_timeout", 20),
            min_interval=1 / max(0.2, float(self.settings.get("requests_per_second", 1.0))),
        )
        try:
            service = SearchService(self.aliases, client)
            results, notes = service.search(
                self.query,
                self.game_key,
                use_aliases=self.options["aliases"],
                multilingual=self.options["multilingual"],
                analyze_body=self.options["body"],
                analyze_attachments=self.options["attachments"],
                pages_per_query=int(self.settings.get("results_per_query_pages", 1)),
                max_queries=int(self.settings.get("max_expanded_queries", 7)),
                max_posts=int(self.settings.get("max_posts_per_search", 250)),
                progress=self.progress.emit,
            )
            self.finished.emit(results, notes)
        except Exception as exc:
            self.finished.emit([], [str(exc)])
        finally:
            client.close()


class SettingsDialog(QDialog):
    def __init__(self, settings: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("검색 설정")
        self.setMinimumWidth(480)
        self.settings = dict(settings)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.api_edit = QLineEdit(str(settings.get("api_base", "")))
        self.pages = QSpinBox(); self.pages.setRange(1, 5); self.pages.setValue(int(settings.get("results_per_query_pages", 1)))
        self.max_queries = QSpinBox(); self.max_queries.setRange(1, 15); self.max_queries.setValue(int(settings.get("max_expanded_queries", 7)))
        self.max_posts = QSpinBox(); self.max_posts.setRange(50, 1000); self.max_posts.setSingleStep(50); self.max_posts.setValue(int(settings.get("max_posts_per_search", 250)))
        self.rate = QSpinBox(); self.rate.setRange(1, 4); self.rate.setSuffix("초/요청"); self.rate.setValue(max(1, round(1 / max(.25, float(settings.get("requests_per_second", 1))))) )
        form.addRow("Pawchive API 주소", self.api_edit)
        form.addRow("검색어당 페이지 수", self.pages)
        form.addRow("확장 검색어 최대 개수", self.max_queries)
        form.addRow("검색당 최대 수집 게시물", self.max_posts)
        form.addRow("요청 간격", self.rate)
        layout.addLayout(form)
        note = QLabel("Pawchive 데이터는 설치된 Google Chrome 또는 Microsoft Edge를 백그라운드로 실행해 가져옵니다. 일반 Chrome 프로필이나 로그인 정보는 사용하지 않습니다. 요청 간격은 서버에 부담을 주지 않도록 보수적으로 설정합니다.")
        note.setWordWrap(True); note.setObjectName("Muted")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> dict[str, Any]:
        result = dict(self.settings)
        result["api_base"] = self.api_edit.text().strip().rstrip("/")
        result["results_per_query_pages"] = self.pages.value()
        result["max_expanded_queries"] = self.max_queries.value()
        result["max_posts_per_search"] = self.max_posts.value()
        result["requests_per_second"] = 1 / self.rate.value()
        return result


class PostDetailDialog(QDialog):
    def __init__(self, result: dict[str, Any], favorite: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.result = result
        self.post = result["post"]
        self.favorite = favorite
        self.setWindowTitle("게시물 분석")
        self.resize(720, 640)
        layout = QVBoxLayout(self)
        title = QLabel(self.post.title or "(제목 없음)")
        title.setWordWrap(True); title.setStyleSheet("font-size: 16pt; font-weight: 700;")
        layout.addWidget(title)
        meta = QLabel(f"{result['category']}  ·  {result['verdict']}  ·  판별 점수 {result['score']}점\n{self.post.service} / 작성자 {self.post.user} / 게시물 ID {self.post.id}")
        meta.setObjectName("Muted"); meta.setWordWrap(True)
        layout.addWidget(meta)
        reasons = QPlainTextEdit("\n".join("• " + reason for reason in result.get("reasons", [])) or "추가 판별 근거 없음")
        reasons.setReadOnly(True); reasons.setMaximumHeight(130)
        layout.addWidget(QLabel("분석 근거")); layout.addWidget(reasons)
        file_lines = []
        for attachment in self.post.all_attachments:
            file_lines.append(f"{attachment.name or '(파일명 없음)'}  [{attachment.extension or '확장자 없음'}]")
        files = QPlainTextEdit("\n".join(file_lines) or "첨부 파일 정보 없음")
        files.setReadOnly(True); files.setMaximumHeight(110)
        layout.addWidget(QLabel("첨부 파일")); layout.addWidget(files)
        content = QPlainTextEdit(result.get("clean_content", "") or "본문을 표시하지 않도록 설정했거나 본문이 없습니다.")
        content.setReadOnly(True)
        layout.addWidget(QLabel("본문")); layout.addWidget(content, 1)
        buttons = QHBoxLayout()
        self.favorite_button = QPushButton("★ 즐겨찾기 해제" if favorite else "☆ 즐겨찾기")
        self.favorite_button.clicked.connect(self._toggle)
        open_button = QPushButton("원본 게시물 열기")
        open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.post.post_url)))
        close_button = QPushButton("닫기"); close_button.clicked.connect(self.accept)
        buttons.addWidget(self.favorite_button); buttons.addStretch(); buttons.addWidget(open_button); buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _toggle(self) -> None:
        self.favorite = not self.favorite
        self.favorite_button.setText("★ 즐겨찾기 해제" if self.favorite else "☆ 즐겨찾기")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Game Mod Finder")
        self.resize(1180, 820)
        self.aliases = load_aliases()
        self.settings = load_settings()
        self.db = FinderDatabase(app_data_dir() / "finder.sqlite3")
        self.results: list[dict[str, Any]] = []
        self.thread: QThread | None = None
        self.worker: SearchWorker | None = None
        self._closing = False
        self.active_filter = "전체"
        self.current_options: dict[str, Any] = {}
        self.cached_results: list[dict[str, Any]] | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget(); self.setCentralWidget(central)
        root = QVBoxLayout(central); root.setContentsMargins(22, 18, 22, 18); root.setSpacing(14)

        header = QFrame(); header.setObjectName("Header")
        header_layout = QHBoxLayout(header); header_layout.setContentsMargins(20, 15, 20, 15)
        left = QVBoxLayout()
        title = QLabel("GAME MOD FINDER"); title.setObjectName("HeroTitle")
        subtitle = QLabel("검색어를 넓게 찾고, 게시물의 모드 근거를 따져서 정렬합니다."); subtitle.setObjectName("Muted")
        left.addWidget(title); left.addWidget(subtitle)
        header_layout.addLayout(left); header_layout.addStretch()
        settings_button = QPushButton("⚙ 검색 설정"); settings_button.clicked.connect(self.open_settings)
        header_layout.addWidget(settings_button)
        root.addWidget(header)

        search_panel = QFrame(); search_panel.setObjectName("Panel")
        search_layout = QVBoxLayout(search_panel); search_layout.setContentsMargins(18, 15, 18, 15); search_layout.setSpacing(10)
        row = QHBoxLayout()
        self.game_combo = QComboBox()
        self.game_combo.addItem("전체 게임", "")
        for key, game in self.aliases.items():
            self.game_combo.addItem(game.get("name", key), key)
        self.query_edit = QLineEdit(); self.query_edit.setPlaceholderText("예: 라이덴 수영복 / Ellen outfit / Kafka skin")
        self.query_edit.returnPressed.connect(self.start_search)
        self.search_button = QPushButton("검색 시작"); self.search_button.setObjectName("Primary"); self.search_button.clicked.connect(self.start_search)
        row.addWidget(QLabel("게임")); row.addWidget(self.game_combo, 1); row.addWidget(self.query_edit, 4); row.addWidget(self.search_button)
        search_layout.addLayout(row)
        checks = QHBoxLayout()
        self.alias_check = QCheckBox("별칭 자동 확장"); self.alias_check.setChecked(True)
        self.lang_check = QCheckBox("다국어 검색"); self.lang_check.setChecked(True)
        self.body_check = QCheckBox("본문 분석"); self.body_check.setChecked(True)
        self.attachment_check = QCheckBox("첨부 파일 분석"); self.attachment_check.setChecked(True)
        for check in (self.alias_check, self.lang_check, self.body_check, self.attachment_check): checks.addWidget(check)
        checks.addStretch(); search_layout.addLayout(checks)
        root.addWidget(search_panel)

        bar = QHBoxLayout()
        self.status_label = QLabel("검색어를 입력해 시작하세요."); self.status_label.setObjectName("Muted")
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.hide(); self.progress.setFixedWidth(170)
        self.count_label = QLabel("결과 0개")
        bar.addWidget(self.status_label, 1); bar.addWidget(self.progress); bar.addWidget(self.count_label)
        root.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        list_panel = QFrame(); list_panel.setObjectName("Panel")
        list_layout = QVBoxLayout(list_panel); list_layout.setContentsMargins(12, 12, 12, 12)
        self.tabs = QTabWidget()
        for name in ("전체", "게임 모드", "모드 관련 자료", "3D 모델", "일러스트", "영상", "기타"):
            page = QWidget(); page_layout = QVBoxLayout(page); page_layout.setContentsMargins(6, 8, 6, 6)
            list_widget = QListWidget()
            list_widget.itemDoubleClicked.connect(self.open_item_detail)
            list_widget.itemClicked.connect(self._list_item_clicked)
            page_layout.addWidget(list_widget)
            self.tabs.addTab(page, name)
        self.tabs.currentChanged.connect(self._tab_changed)
        list_layout.addWidget(self.tabs)
        splitter.addWidget(list_panel)

        detail_panel = QFrame(); detail_panel.setObjectName("Panel")
        detail_layout = QVBoxLayout(detail_panel); detail_layout.setContentsMargins(18, 18, 18, 18)
        self.detail_title = QLabel("검색 결과를 선택하세요")
        self.detail_title.setWordWrap(True); self.detail_title.setStyleSheet("font-size: 15pt; font-weight: 700;")
        self.detail_meta = QLabel("선택한 게시물의 판별 점수와 분석 근거가 여기에 표시됩니다.")
        self.detail_meta.setWordWrap(True); self.detail_meta.setObjectName("Muted")
        self.detail_reasons = QPlainTextEdit(); self.detail_reasons.setReadOnly(True)
        self.detail_files = QPlainTextEdit(); self.detail_files.setReadOnly(True); self.detail_files.setMaximumHeight(150)
        self.open_button = QPushButton("원본 게시물 열기"); self.open_button.setEnabled(False); self.open_button.clicked.connect(self.open_selected_url)
        self.favorite_button = QPushButton("☆ 즐겨찾기"); self.favorite_button.setEnabled(False); self.favorite_button.clicked.connect(self.toggle_selected_favorite)
        detail_layout.addWidget(self.detail_title); detail_layout.addWidget(self.detail_meta)
        detail_layout.addWidget(QLabel("분석 근거")); detail_layout.addWidget(self.detail_reasons, 1)
        detail_layout.addWidget(QLabel("첨부 파일")); detail_layout.addWidget(self.detail_files)
        detail_layout.addWidget(self.open_button); detail_layout.addWidget(self.favorite_button)
        splitter.addWidget(detail_panel); splitter.setSizes([650, 470])
        root.addWidget(splitter, 1)

        self.statusBar().showMessage("설치된 Chrome을 백그라운드에서 사용해 Pawchive를 검색합니다. 요청은 설정한 간격으로 순차 전송됩니다.")

    def _current_list(self) -> QListWidget:
        return self.tabs.currentWidget().findChild(QListWidget)

    def _tab_changed(self, _: int) -> None:
        self._populate_current_list()

    def start_search(self) -> None:
        query = self.query_edit.text().strip()
        if len(query) < 2:
            QMessageBox.information(self, "검색어 필요", "게임 이름이나 캐릭터 이름을 입력해 주세요.")
            return
        if self.thread and self.thread.isRunning():
            return
        options = {
            "aliases": self.alias_check.isChecked(),
            "multilingual": self.lang_check.isChecked(),
            "body": self.body_check.isChecked(),
            "attachments": self.attachment_check.isChecked(),
        }
        self.current_options = options
        cached = self.db.load_search_cache(query, self.game_combo.currentData() or "", options)
        self.cached_results = cached
        self.results = cached or []
        self._clear_detail()
        self._populate_current_list()
        self.search_button.setEnabled(False); self.progress.show()
        self.game_combo.setEnabled(False); self.query_edit.setEnabled(False)
        for control in (self.alias_check, self.lang_check, self.body_check, self.attachment_check):
            control.setEnabled(False)
        self.status_label.setText("저장된 검색 결과 표시 중 · 최신 결과 확인 중..." if cached is not None else "검색 준비 중...")
        self.count_label.setText(f"캐시 결과 {len(self.results)}개" if cached is not None else "검색 중")
        self.thread = QThread()
        self.worker = SearchWorker(query, self.game_combo.currentData(), options, self.aliases, self.settings)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.status_label.setText)
        self.worker.finished.connect(self.search_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        # Keep a strong reference to QThread until it has actually stopped.
        # Clearing self.thread in search_finished() can destroy a still-running
        # QThread and trigger: "QThread: Destroyed while thread is still running".
        self.thread.finished.connect(self._on_search_thread_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def search_finished(self, results: list[dict[str, Any]], notes: list[str]) -> None:
        used_cache_fallback = not results and bool(notes) and self.cached_results is not None
        if used_cache_fallback:
            results = self.cached_results or []
        self.results = results
        if not used_cache_fallback:
            self.db.upsert_results(results)
            self.db.save_search_cache(self.query_edit.text().strip(), self.game_combo.currentData() or "", self.current_options, results)
        if self.query_edit.text().strip():
            self.db.add_history(self.query_edit.text().strip(), self.game_combo.currentData() or "")
        self.search_button.setEnabled(True); self.progress.hide()
        self.game_combo.setEnabled(True); self.query_edit.setEnabled(True)
        for control in (self.alias_check, self.lang_check, self.body_check, self.attachment_check):
            control.setEnabled(True)
        self._populate_current_list()
        self.count_label.setText(f"결과 {len(results)}개")
        if results and used_cache_fallback:
            self.status_label.setText("네트워크 확인 실패 · 저장된 검색 결과를 표시합니다.")
        elif results:
            self.status_label.setText("분석 완료 · 점수와 분류는 규칙 기반 추정치입니다.")
            self._select_result(results[0])
        else:
            self.status_label.setText("검색 결과가 없습니다.")
        if notes:
            QMessageBox.warning(self, "일부 검색 요청 실패", "\n\n".join(notes[:5]))
        # Do not release the QThread here. The worker's finished signal is
        # emitted just before the thread's event loop exits. The reference is
        # released by _on_search_thread_finished() instead.
        self.cached_results = None

    def _on_search_thread_finished(self) -> None:
        """Release thread references only after QThread.finished is emitted."""
        self.worker = None
        self.thread = None
        if self._closing:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Defer window destruction until an active search thread has stopped."""
        if self.thread is not None and self.thread.isRunning():
            self._closing = True
            self.search_button.setEnabled(False)
            self.status_label.setText("검색 작업을 안전하게 마친 뒤 프로그램을 종료합니다...")
            self.statusBar().showMessage("검색이 끝나면 자동으로 종료됩니다.")
            event.ignore()
            return
        event.accept()

    def _populate_current_list(self) -> None:
        widget = self._current_list()
        widget.clear()
        tab_name = self.tabs.tabText(self.tabs.currentIndex())
        selected = [item for item in self.results if tab_name == "전체" or item["category"] == tab_name]
        for result in selected:
            post = result["post"]
            favorite = self.db.is_favorite(post.key)
            title = post.title or "(제목 없음)"
            line = f"{result['category']}  ·  {result['score']}점  ·  {result['verdict']}"
            file_text = ", ".join(a.extension.upper() for a in post.all_attachments if a.extension) or "첨부 정보 없음"
            label = f"{'★ ' if favorite else ''}{title}\n{line}\n{post.service} / 파일: {file_text}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, result)
            item.setToolTip(post.post_url)
            widget.addItem(item)
        self.count_label.setText(f"결과 {len(selected)}개 / 전체 {len(self.results)}개")

    def _list_item_clicked(self, item: QListWidgetItem) -> None:
        result = item.data(Qt.UserRole)
        if result:
            self._select_result(result)

    def _select_result(self, result: dict[str, Any]) -> None:
        self.selected_result = result
        post = result["post"]
        self.detail_title.setText(post.title or "(제목 없음)")
        game_name = self.aliases.get(result.get("game_key", ""), {}).get("name", "미확인")
        self.detail_meta.setText(
            f"{result['category']}  ·  {result['verdict']}  ·  {result['score']}점\n"
            f"게임: {game_name}  /  캐릭터: {result.get('character') or '미확인'}\n"
            f"작성자 ID: {post.user}  /  게시 날짜: {post.published or '정보 없음'}"
        )
        self.detail_reasons.setPlainText("\n".join("• " + reason for reason in result.get("reasons", [])) or "분석 근거 없음")
        self.detail_files.setPlainText("\n".join(f"{a.name or a.path} [{a.extension or '-'}]" for a in post.all_attachments) or "첨부 파일 정보 없음")
        self.open_button.setEnabled(True); self.favorite_button.setEnabled(True)
        self.favorite_button.setText("★ 즐겨찾기 해제" if self.db.is_favorite(post.key) else "☆ 즐겨찾기")

    def _clear_detail(self) -> None:
        self.detail_title.setText("검색 결과를 선택하세요")
        self.detail_meta.setText("선택한 게시물의 판별 점수와 분석 근거가 여기에 표시됩니다.")
        self.detail_reasons.clear(); self.detail_files.clear()
        self.open_button.setEnabled(False); self.favorite_button.setEnabled(False)
        self.selected_result = None

    def open_item_detail(self, item: QListWidgetItem) -> None:
        result = item.data(Qt.UserRole)
        if not result:
            return
        post = result["post"]
        dialog = PostDetailDialog(result, self.db.is_favorite(post.key), self)
        dialog.exec()
        if dialog.favorite != self.db.is_favorite(post.key):
            self.db.toggle_favorite(post.key)
            self._populate_current_list()
            self._select_result(result)

    def open_selected_url(self) -> None:
        result = getattr(self, "selected_result", None)
        if result:
            QDesktopServices.openUrl(QUrl(result["post"].post_url))

    def toggle_selected_favorite(self) -> None:
        result = getattr(self, "selected_result", None)
        if not result:
            return
        post = result["post"]
        enabled = self.db.toggle_favorite(post.key)
        self.favorite_button.setText("★ 즐겨찾기 해제" if enabled else "☆ 즐겨찾기")
        self._populate_current_list()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            updated = dialog.value()
            if not updated["api_base"].startswith("https://"):
                QMessageBox.warning(self, "API 주소 오류", "API 주소는 https:// 로 시작해야 합니다.")
                return
            self.settings = updated
            save_settings(updated)
            self.statusBar().showMessage("설정을 저장했습니다.", 4000)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Game Mod Finder")
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
