from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QObject, QThread, Qt, Signal, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QPlainTextEdit,
    QProgressBar, QSpinBox, QSplitter, QVBoxLayout, QWidget, QTabWidget,
)

from gamemodfinder.analyzer import clean_html
from gamemodfinder.api import PawchiveClient
from gamemodfinder.config import app_data_dir, load_aliases, load_settings, save_settings
from gamemodfinder.database import FinderDatabase
from gamemodfinder.search import SearchService
from gamemodfinder.sync import sync_creator


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
    """Remote keyword search is now used only to discover creator candidates."""
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
                self.query, self.game_key,
                use_aliases=self.options.get("aliases", True),
                multilingual=self.options.get("multilingual", True),
                analyze_body=self.options.get("body", True),
                analyze_attachments=self.options.get("attachments", True),
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


class CreatorSyncWorker(QObject):
    progress = Signal(str)
    finished = Signal(object, object)

    def __init__(
        self, database_path: Path, creators: list[dict[str, Any]],
        aliases: dict[str, Any], settings: dict[str, Any], max_pages: int = 5,
    ) -> None:
        super().__init__()
        self.database_path = database_path
        self.creators = creators
        self.aliases = aliases
        self.settings = settings
        self.max_pages = max_pages

    def run(self) -> None:
        db = FinderDatabase(self.database_path)
        client = PawchiveClient(
            self.settings["api_base"],
            timeout=self.settings.get("request_timeout", 20),
            min_interval=1 / max(0.2, float(self.settings.get("requests_per_second", 1.0))),
        )
        summaries: list[dict[str, Any]] = []
        try:
            for index, creator in enumerate(self.creators, start=1):
                label = creator.get("name") or creator["user_id"]
                self.progress.emit(f"제작자 동기화 {index}/{len(self.creators)} · {label}")
                try:
                    result = sync_creator(
                        db, client, self.aliases,
                        creator["service"], creator["user_id"],
                        max_pages=self.max_pages, progress=self.progress.emit,
                    )
                    summaries.append(result)
                except Exception as exc:
                    summaries.append({
                        "service": creator["service"],
                        "user_id": creator["user_id"],
                        "new_posts": 0,
                        "error": str(exc),
                    })
            self.finished.emit(summaries, [])
        except Exception as exc:
            self.finished.emit(summaries, [str(exc)])
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
        self.sync_pages = QSpinBox(); self.sync_pages.setRange(1, 100); self.sync_pages.setValue(int(settings.get("creator_sync_pages", 5)))
        self.rate = QSpinBox(); self.rate.setRange(1, 4); self.rate.setSuffix("초/요청"); self.rate.setValue(max(1, round(1 / max(.25, float(settings.get("requests_per_second", 1))))) )
        form.addRow("Pawchive API 주소", self.api_edit)
        form.addRow("발굴 검색어당 페이지 수", self.pages)
        form.addRow("발굴 확장 검색어 최대 개수", self.max_queries)
        form.addRow("발굴 검색당 최대 게시물", self.max_posts)
        form.addRow("제작자 1회 동기화 페이지", self.sync_pages)
        form.addRow("요청 간격", self.rate)
        layout.addLayout(form)
        note = QLabel("제작자 게시물은 Pawchive 공개 API의 제작자 타임라인을 사용합니다. 초기 동기화 페이지 수를 제한해 요청량을 제어하며, 이후에는 마지막으로 본 게시물에서 멈춥니다. 브라우저는 임시 세션을 사용합니다.")
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
        result["creator_sync_pages"] = self.sync_pages.value()
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
        file_lines = [
            f"{a.name or '(파일명 없음)'}  [{a.extension or '확장자 없음'}]"
            for a in self.post.all_attachments
        ]
        files = QPlainTextEdit("\n".join(file_lines) or "첨부 파일 정보 없음")
        files.setReadOnly(True); files.setMaximumHeight(110)
        layout.addWidget(QLabel("첨부 파일")); layout.addWidget(files)
        content = QPlainTextEdit(result.get("clean_content", "") or "본문이 없습니다.")
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
        self.resize(1240, 860)
        self.aliases = load_aliases()
        self.settings = load_settings()
        self.db_path = app_data_dir() / "finder.sqlite3"
        self.db = FinderDatabase(self.db_path)
        self.results: list[dict[str, Any]] = []
        self.thread: QThread | None = None
        self.worker: QObject | None = None
        self._closing = False
        self._worker_mode = ""
        self.current_options: dict[str, Any] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget(); self.setCentralWidget(central)
        root = QVBoxLayout(central); root.setContentsMargins(22, 18, 22, 18); root.setSpacing(14)

        header = QFrame(); header.setObjectName("Header")
        header_layout = QHBoxLayout(header); header_layout.setContentsMargins(20, 15, 20, 15)
        left = QVBoxLayout()
        title = QLabel("GAME MOD FINDER"); title.setObjectName("HeroTitle")
        subtitle = QLabel("제작자를 팔로우하고, 게시물을 로컬에서 빠르게 검색합니다."); subtitle.setObjectName("Muted")
        left.addWidget(title); left.addWidget(subtitle)
        header_layout.addLayout(left); header_layout.addStretch()
        settings_button = QPushButton("⚙ 설정"); settings_button.clicked.connect(self.open_settings)
        header_layout.addWidget(settings_button)
        root.addWidget(header)

        self.workspace = QTabWidget()
        root.addWidget(self.workspace, 1)
        self._build_search_page()
        self._build_creators_page()
        self._build_discovery_page()
        self.statusBar().showMessage("검색은 로컬 DB를 사용합니다. 제작자 동기화와 발굴만 Pawchive API에 요청합니다.")

    def _build_search_page(self) -> None:
        page = QWidget(); layout = QVBoxLayout(page); layout.setContentsMargins(8, 10, 8, 8); layout.setSpacing(12)
        search_panel = QFrame(); search_panel.setObjectName("Panel")
        search_layout = QVBoxLayout(search_panel); search_layout.setContentsMargins(18, 15, 18, 15); search_layout.setSpacing(10)
        row = QHBoxLayout()
        self.game_combo = QComboBox(); self.game_combo.addItem("전체 게임", "")
        for key, game in self.aliases.items():
            self.game_combo.addItem(game.get("name", key), key)
        self.query_edit = QLineEdit(); self.query_edit.setPlaceholderText("로컬 검색: 라이덴 수영복 / Ellen outfit / Kafka skin")
        self.query_edit.returnPressed.connect(self.start_search)
        self.search_button = QPushButton("로컬 검색"); self.search_button.setObjectName("Primary"); self.search_button.clicked.connect(self.start_search)
        row.addWidget(QLabel("게임")); row.addWidget(self.game_combo, 1); row.addWidget(self.query_edit, 4); row.addWidget(self.search_button)
        search_layout.addLayout(row)
        checks = QHBoxLayout()
        self.alias_check = QCheckBox("별칭 확장"); self.alias_check.setChecked(True)
        self.favorite_boost_check = QCheckBox("즐겨찾기 가산"); self.favorite_boost_check.setChecked(True)
        self.mod_boost_check = QCheckBox("제작자 점수 가산"); self.mod_boost_check.setChecked(True)
        for check in (self.alias_check, self.favorite_boost_check, self.mod_boost_check): checks.addWidget(check)
        checks.addStretch(); search_layout.addLayout(checks)
        layout.addWidget(search_panel)

        bar = QHBoxLayout()
        self.status_label = QLabel("로컬 게시물 DB에서 검색합니다."); self.status_label.setObjectName("Muted")
        self.count_label = QLabel("결과 0개")
        bar.addWidget(self.status_label, 1); bar.addWidget(self.count_label)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        list_panel = QFrame(); list_panel.setObjectName("Panel")
        list_layout = QVBoxLayout(list_panel); list_layout.setContentsMargins(12, 12, 12, 12)
        self.tabs = QTabWidget()
        for name in ("전체", "게임 모드", "모드 관련 자료", "3D 모델", "일러스트", "영상", "기타"):
            tab_page = QWidget(); page_layout = QVBoxLayout(tab_page); page_layout.setContentsMargins(6, 8, 6, 6)
            list_widget = QListWidget()
            list_widget.itemDoubleClicked.connect(self.open_item_detail)
            list_widget.itemClicked.connect(self._list_item_clicked)
            page_layout.addWidget(list_widget)
            self.tabs.addTab(tab_page, name)
        self.tabs.currentChanged.connect(self._tab_changed)
        list_layout.addWidget(self.tabs); splitter.addWidget(list_panel)

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
        layout.addWidget(splitter, 1)
        self.workspace.addTab(page, "로컬 검색")

    def _build_creators_page(self) -> None:
        page = QWidget(); layout = QVBoxLayout(page); layout.setContentsMargins(8, 10, 8, 8); layout.setSpacing(12)
        panel = QFrame(); panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel); panel_layout.setContentsMargins(16, 14, 16, 14)
        url_row = QHBoxLayout()
        self.creator_url_edit = QLineEdit()
        self.creator_url_edit.setPlaceholderText("Pawchive 제작자 URL 붙여넣기: https://pawchive.pw/patreon/user/12345")
        self.add_creator_button = QPushButton("제작자 추가"); self.add_creator_button.clicked.connect(self.add_creator_from_url)
        url_row.addWidget(self.creator_url_edit, 1); url_row.addWidget(self.add_creator_button)
        panel_layout.addLayout(url_row)
        actions = QHBoxLayout()
        self.follow_button = QPushButton("팔로우/해제"); self.follow_button.clicked.connect(self.toggle_creator_follow)
        self.sync_selected_button = QPushButton("선택 제작자 동기화"); self.sync_selected_button.clicked.connect(self.sync_selected_creator)
        self.sync_all_button = QPushButton("팔로우 전체 동기화"); self.sync_all_button.setObjectName("Primary"); self.sync_all_button.clicked.connect(self.sync_all_creators)
        self.favorite_suggest_button = QPushButton("즐겨찾기 작성자 가져오기"); self.favorite_suggest_button.clicked.connect(self.add_favorite_creator_suggestions)
        self.remove_creator_button = QPushButton("목록에서 제거"); self.remove_creator_button.clicked.connect(self.remove_selected_creator)
        self.import_button = QPushButton("JSON 가져오기"); self.import_button.clicked.connect(self.import_creators)
        self.export_button = QPushButton("JSON 내보내기"); self.export_button.clicked.connect(self.export_creators)
        for button in (self.follow_button, self.sync_selected_button, self.sync_all_button,
                       self.favorite_suggest_button, self.remove_creator_button,
                       self.import_button, self.export_button):
            actions.addWidget(button)
        panel_layout.addLayout(actions)
        self.followed_only_check = QCheckBox("팔로우 중인 제작자만 보기")
        self.followed_only_check.stateChanged.connect(self.refresh_creator_list)
        panel_layout.addWidget(self.followed_only_check)
        layout.addWidget(panel)
        self.creator_list = QListWidget()
        self.creator_list.itemDoubleClicked.connect(lambda _: self.sync_selected_creator())
        layout.addWidget(self.creator_list, 1)
        self.creator_status = QLabel("제작자를 추가하거나 발굴 탭에서 후보를 찾아보세요.")
        self.creator_status.setObjectName("Muted")
        layout.addWidget(self.creator_status)
        self.workspace.addTab(page, "제작자")

    def _build_discovery_page(self) -> None:
        page = QWidget(); layout = QVBoxLayout(page); layout.setContentsMargins(8, 10, 8, 8); layout.setSpacing(12)
        panel = QFrame(); panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel); panel_layout.setContentsMargins(16, 14, 16, 14)
        row = QHBoxLayout()
        self.discover_game_combo = QComboBox(); self.discover_game_combo.addItem("전체 게임", "")
        for key, game in self.aliases.items():
            self.discover_game_combo.addItem(game.get("name", key), key)
        self.discover_query_edit = QLineEdit(); self.discover_query_edit.setPlaceholderText("발굴 키워드 예: Genshin Impact Raiden mod")
        self.discover_query_edit.returnPressed.connect(self.start_discovery)
        self.discover_button = QPushButton("제작자 후보 발굴"); self.discover_button.setObjectName("Primary"); self.discover_button.clicked.connect(self.start_discovery)
        row.addWidget(self.discover_game_combo); row.addWidget(self.discover_query_edit, 1); row.addWidget(self.discover_button)
        panel_layout.addLayout(row)
        note = QLabel("기존 Pawchive 키워드 검색 결과를 제작자별로 집계합니다. 결과 게시물은 로컬 DB에 저장되며, 발굴은 서버 요청 간격을 지켜 진행합니다.")
        note.setWordWrap(True); note.setObjectName("Muted")
        panel_layout.addWidget(note)
        layout.addWidget(panel)
        action_row = QHBoxLayout()
        self.follow_candidate_button = QPushButton("선택 후보 팔로우"); self.follow_candidate_button.clicked.connect(self.follow_selected_candidate)
        self.follow_all_candidates_button = QPushButton("표시된 후보 모두 팔로우"); self.follow_all_candidates_button.clicked.connect(self.follow_all_candidates)
        self.refresh_candidates_button = QPushButton("후보 새로고침"); self.refresh_candidates_button.clicked.connect(self.refresh_candidate_list)
        action_row.addWidget(self.follow_candidate_button); action_row.addWidget(self.follow_all_candidates_button); action_row.addWidget(self.refresh_candidates_button); action_row.addStretch()
        layout.addLayout(action_row)
        self.discovery_list = QListWidget()
        layout.addWidget(self.discovery_list, 1)
        self.discovery_status = QLabel("검색어를 입력해 관련 제작자 후보를 발굴하세요.")
        self.discovery_status.setObjectName("Muted")
        layout.addWidget(self.discovery_status)
        self.workspace.addTab(page, "발굴")

    def _active_thread(self) -> bool:
        return self.thread is not None and self.thread.isRunning()

    def _launch_worker(self, worker: QObject, mode: str) -> None:
        if self._active_thread():
            return
        self._worker_mode = mode
        self.thread = QThread()
        self.worker = worker
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        worker.progress.connect(self._worker_progress)
        worker.finished.connect(self._worker_finished)
        worker.finished.connect(self.thread.quit)
        worker.finished.connect(worker.deleteLater)
        self.thread.finished.connect(self._on_worker_thread_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _worker_progress(self, message: str) -> None:
        self.statusBar().showMessage(message)
        if hasattr(self, "discovery_status") and self._worker_mode == "discover":
            self.discovery_status.setText(message)
        if hasattr(self, "creator_status") and self._worker_mode == "sync":
            self.creator_status.setText(message)

    def _worker_finished(self, payload: Any, notes: Any) -> None:
        if self._worker_mode == "discover":
            results = payload if isinstance(payload, list) else []
            self.db.upsert_results(results)
            self.refresh_candidate_list()
            self.discovery_status.setText(
                f"발굴 완료 · 게시물 {len(results)}개에서 제작자 후보를 집계했습니다."
            )
            if notes:
                QMessageBox.warning(self, "일부 발굴 요청 실패", "\n\n".join(str(x) for x in notes[:5]))
        elif self._worker_mode == "sync":
            summaries = payload if isinstance(payload, list) else []
            total_new = sum(int(item.get("new_posts", 0)) for item in summaries)
            errors = [f"{x.get('service')}/{x.get('user_id')}: {x['error']}" for x in summaries if x.get("error")]
            self.creator_status.setText(
                f"동기화 완료 · 제작자 {len(summaries)}명 · 신규 게시물 {total_new}개"
            )
            self.refresh_creator_list()
            self.refresh_candidate_list()
            if errors:
                QMessageBox.warning(self, "일부 제작자 동기화 실패", "\n".join(errors[:8]))
            if notes:
                QMessageBox.warning(self, "동기화 오류", "\n".join(str(x) for x in notes[:5]))

    def _on_worker_thread_finished(self) -> None:
        self.worker = None
        self.thread = None
        self._worker_mode = ""
        if self._closing:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._active_thread():
            self._closing = True
            self.statusBar().showMessage("진행 중인 네트워크 작업을 마친 뒤 종료합니다.")
            event.ignore()
            return
        event.accept()

    def start_search(self) -> None:
        query = self.query_edit.text().strip()
        if len(query) < 2:
            QMessageBox.information(self, "검색어 필요", "두 글자 이상의 검색어를 입력해 주세요.")
            return
        game_key = self.game_combo.currentData() or ""
        search_service = SearchService(self.aliases, None)  # expansion only; no network client is used
        queries = search_service.expand_local_queries(
            query, game_key, use_aliases=self.alias_check.isChecked(),
            multilingual=True,
        )
        self.results = self.db.search_local(queries, game_key=game_key, limit=500)
        for result in self.results:
            result["clean_content"] = clean_html(result["post"].content)
            if not self.mod_boost_check.isChecked():
                result["score"] = result.get("base_score", result["score"])
            if not self.favorite_boost_check.isChecked() and self.db.is_favorite(result["post"].key):
                result["score"] = max(0, result["score"] - 8)
        self.results.sort(key=lambda r: (r["score"], r["post"].published or r["post"].added), reverse=True)
        self._clear_detail()
        self._populate_current_list()
        self.count_label.setText(f"결과 {len(self.results)}개")
        self.status_label.setText(
            f"로컬 검색 완료 · 확장 검색어 {len(queries)}개 · 네트워크 요청 없음"
            if self.results else "로컬 DB에 일치하는 게시물이 없습니다. 발굴 탭에서 제작자를 먼저 찾아보세요."
        )
        self.db.add_history(query, game_key)
        if self.results:
            self._select_result(self.results[0])

    def start_discovery(self) -> None:
        if self._active_thread():
            return
        query = self.discover_query_edit.text().strip()
        if len(query) < 3:
            QMessageBox.information(self, "발굴어 필요", "발굴 검색어를 세 글자 이상 입력해 주세요.")
            return
        options = {
            "aliases": True, "multilingual": True, "body": True, "attachments": True,
        }
        self.discover_button.setEnabled(False)
        self.discovery_status.setText("Pawchive에서 제작자 후보를 발굴합니다...")
        worker = SearchWorker(
            query, self.discover_game_combo.currentData() or "",
            options, self.aliases, self.settings,
        )
        self._launch_worker(worker, "discover")
        self.thread.finished.connect(lambda: self.discover_button.setEnabled(True))

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
            item = QListWidgetItem(f"{'★ ' if favorite else ''}{title}\n{line}\n{post.service}/{post.user} · {file_text}")
            item.setData(Qt.UserRole, result)
            item.setToolTip(post.post_url)
            widget.addItem(item)
        self.count_label.setText(f"결과 {len(selected)}개 / 전체 {len(self.results)}개")

    def _current_list(self) -> QListWidget:
        return self.tabs.currentWidget().findChild(QListWidget)

    def _tab_changed(self, _: int) -> None:
        self._populate_current_list()

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
            f"작성자: {post.service}/{post.user}  /  게시 날짜: {post.published or '정보 없음'}"
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

    @staticmethod
    def parse_creator_url(value: str) -> tuple[str, str]:
        parsed = urlparse(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Pawchive 제작자 URL을 입력해 주세요.")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[1] == "user":
            return parts[0], parts[2]
        # Also accept the legacy /user/{service}/{creator_id} URL form.
        if len(parts) >= 3 and parts[0] == "user":
            return parts[1], parts[2]
        raise ValueError("URL에서 service와 제작자 ID를 찾지 못했습니다.")

    def add_creator_from_url(self) -> None:
        try:
            service, user_id = self.parse_creator_url(self.creator_url_edit.text())
            self.db.add_creator(service, user_id, followed=True)
            self.creator_url_edit.clear()
            self.refresh_creator_list()
            self.creator_status.setText(f"{service}/{user_id} 제작자를 팔로우 목록에 추가했습니다.")
        except Exception as exc:
            QMessageBox.warning(self, "제작자 URL 오류", str(exc))

    def _selected_creator(self) -> dict[str, Any] | None:
        item = self.creator_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def refresh_creator_list(self, *_: Any) -> None:
        followed_only = self.followed_only_check.isChecked()
        self.creator_list.clear()
        for creator in self.db.get_creators(followed_only=followed_only):
            games = creator.get("games", {})
            game_names = [
                self.aliases.get(key, {}).get("name", key)
                for key in sorted(games, key=games.get, reverse=True)[:3]
            ]
            ratio = f"{creator['mod_ratio'] * 100:.0f}%"
            sync_time = creator.get("last_synced") or "동기화 전"
            state = "팔로우 중" if creator["followed"] else "미팔로우"
            new_count = int(creator.get("new_post_count") or 0)
            label = (
                f"{'★' if creator['followed'] else '☆'} {creator['name'] or creator['user_id']} "
                f"({creator['service']}/{creator['user_id']})\n"
                f"게시물 {creator['post_count']}개 · 모드 비율 {ratio} · 게임 {', '.join(game_names) or '미분류'}\n"
                f"{state} · 신규 {new_count}개 · 마지막 동기화 {sync_time}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, creator)
            self.creator_list.addItem(item)

    def toggle_creator_follow(self) -> None:
        creator = self._selected_creator()
        if not creator:
            return
        followed = not bool(creator["followed"])
        self.db.set_creator_followed(creator["service"], creator["user_id"], followed)
        self.refresh_creator_list()

    def remove_selected_creator(self) -> None:
        creator = self._selected_creator()
        if not creator:
            return
        answer = QMessageBox.question(
            self, "제작자 제거",
            "팔로우/제작자 목록에서만 제거합니다. 이미 저장된 게시물과 즐겨찾기는 유지됩니다. 계속할까요?",
        )
        if answer == QMessageBox.Yes:
            self.db.remove_creator(creator["service"], creator["user_id"])
            self.refresh_creator_list()
            self.refresh_candidate_list()

    def add_favorite_creator_suggestions(self) -> None:
        suggestions = self.db.favorite_creator_suggestions()
        added = 0
        for item in suggestions:
            self.db.add_creator(
                item["service"], item["user_id"],
                item.get("name") or item["user_id"], followed=True,
            )
            added += 1
        self.refresh_creator_list()
        self.creator_status.setText(f"즐겨찾기에서 작성자 {added}명을 팔로우 목록에 반영했습니다.")

    def import_creators(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "제작자 JSON 가져오기", "", "JSON (*.json)")
        if not filename:
            return
        try:
            payload = json.loads(Path(filename).read_text(encoding="utf-8"))
            count = self.db.import_creators(payload)
            self.refresh_creator_list()
            self.creator_status.setText(f"제작자 {count}명을 가져왔습니다.")
        except Exception as exc:
            QMessageBox.warning(self, "JSON 가져오기 실패", str(exc))

    def export_creators(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "제작자 JSON 내보내기", "creators.json", "JSON (*.json)")
        if not filename:
            return
        try:
            Path(filename).write_text(
                json.dumps(self.db.export_creators(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.creator_status.setText(f"제작자 목록을 저장했습니다: {filename}")
        except Exception as exc:
            QMessageBox.warning(self, "JSON 내보내기 실패", str(exc))

    def sync_selected_creator(self) -> None:
        creator = self._selected_creator()
        if not creator:
            QMessageBox.information(self, "제작자 선택", "동기화할 제작자를 선택해 주세요.")
            return
        self.db.set_creator_followed(creator["service"], creator["user_id"], True)
        self._start_creator_sync([creator])

    def sync_all_creators(self) -> None:
        creators = self.db.get_creators(followed_only=True)
        if not creators:
            QMessageBox.information(self, "팔로우 목록 비어 있음", "먼저 제작자를 팔로우해 주세요.")
            return
        self._start_creator_sync(creators)

    def _start_creator_sync(self, creators: list[dict[str, Any]]) -> None:
        if self._active_thread():
            return
        worker = CreatorSyncWorker(
            self.db_path, creators, self.aliases, self.settings,
            max_pages=int(self.settings.get("creator_sync_pages", 5)),
        )
        self._launch_worker(worker, "sync")
        self.creator_status.setText("제작자별 최신 게시물을 확인 중입니다...")

    def refresh_candidate_list(self) -> None:
        self.discovery_list.clear()
        candidates = [
            c for c in self.db.get_creators()
            if not c["followed"] and int(c["post_count"]) > 0
        ]
        for creator in candidates:
            games = creator.get("games", {})
            top_games = [
                self.aliases.get(key, {}).get("name", key)
                for key in sorted(games, key=games.get, reverse=True)[:3]
            ]
            label = (
                f"{creator['name'] or creator['user_id']} · {creator['service']}/{creator['user_id']}\n"
                f"게시물 {creator['post_count']}개 · 모드 비율 {creator['mod_ratio'] * 100:.0f}% · "
                f"게임 {', '.join(top_games) or '미분류'}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, creator)
            self.discovery_list.addItem(item)
        self.discovery_status.setText(f"미팔로우 제작자 후보 {len(candidates)}명")

    def _selected_candidate(self) -> dict[str, Any] | None:
        item = self.discovery_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def follow_selected_candidate(self) -> None:
        creator = self._selected_candidate()
        if not creator:
            return
        self.db.set_creator_followed(creator["service"], creator["user_id"], True)
        self.refresh_candidate_list()
        self.refresh_creator_list()

    def follow_all_candidates(self) -> None:
        candidates = [
            c for c in self.db.get_creators()
            if not c["followed"] and int(c["post_count"]) > 0
        ]
        for creator in candidates:
            self.db.set_creator_followed(creator["service"], creator["user_id"], True)
        self.refresh_candidate_list()
        self.refresh_creator_list()
        self.discovery_status.setText(f"후보 {len(candidates)}명을 모두 팔로우했습니다.")

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
