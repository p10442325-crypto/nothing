from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QUrl, QSize
from PySide6.QtGui import QDesktopServices, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QPlainTextEdit, QTextBrowser, QProgressBar, QSpinBox, QSplitter, QTabWidget,
    QVBoxLayout, QWidget, QInputDialog,
)

from .api import PawchiveClient, PawchiveError
from .downloads import (DownloadError, download_file, fetch_thumbnail_bytes, resolve_thumbnail_url, safe_extract_archive)
from .models import Attachment, Creator, Post, parse_creator_url
from .repository import LocalRepository


APP_STYLE = """
QWidget { background:#11151d; color:#e8edf5; font-family:'Segoe UI','Malgun Gothic'; font-size:10pt; }
QMainWindow { background:#0c1017; }
QFrame#Header { background:#171f2b; border:1px solid #2a3749; border-radius:14px; }
QLabel#Hero { font-size:22pt; font-weight:800; color:#f5f7fb; }
QLabel#Subtle { color:#94a4ba; }
QLabel#Section { font-size:13pt; font-weight:700; color:#eaf1fb; }
QFrame#Panel { background:#151d28; border:1px solid #2b394d; border-radius:11px; }
QLineEdit,QComboBox,QSpinBox,QPlainTextEdit,QListWidget {
 background:#0e141d; border:1px solid #334258; border-radius:8px; padding:8px;
 selection-background-color:#355b87;
}
QLineEdit:focus,QComboBox:focus { border:1px solid #74a9e9; }
QLabel#PostPreview { background:#0e141d; border:1px solid #334258; border-radius:8px; color:#94a4ba; padding:6px; }
QPushButton { background:#26374d; border:1px solid #405672; border-radius:8px; padding:8px 13px; font-weight:600; }
QPushButton:hover { background:#324a67; }
QPushButton:disabled { color:#778598; background:#1a2430; }
QPushButton#Primary { background:#3976c8; border-color:#5a91d4; color:white; }
QPushButton#Primary:hover { background:#4b89db; }
QListWidget { border:0; background:transparent; padding:2px; }
QListWidget::item { background:#182230; border:1px solid #2b3b50; border-radius:8px; padding:10px; margin:3px 1px; }
QListWidget::item:selected { background:#213a56; border-color:#6596cd; }
QTabWidget::pane { border:1px solid #2a394c; border-radius:10px; background:#151d28; }
QTabBar::tab { background:#161e29; padding:10px 16px; margin-right:3px; border-top-left-radius:7px; border-top-right-radius:7px; }
QTabBar::tab:selected { background:#2a405b; color:white; }
QProgressBar { border:1px solid #34455b; border-radius:6px; background:#0e141d; height:12px; text-align:center; }
QProgressBar::chunk { background:#4a8bd8; border-radius:5px; }
"""


def data_dir() -> Path:
    root = Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    path = root / "PawchiveModFinder"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_settings() -> dict:
    path = data_dir() / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return {
        "download_dir": str(Path.home() / "Downloads" / "Pawchive Mod Finder"),
        "game_paths": {},
        "sync_minutes": 60,
    }


def save_settings(settings: dict) -> None:
    path = data_dir() / "settings.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class TaskThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, task: Callable[[], object]) -> None:
        super().__init__()
        self.task = task

    def run(self) -> None:
        try:
            self.succeeded.emit(self.task())
        except Exception as exc:
            self.failed.emit(str(exc))


class PostBrowser(QWidget):
    download_requested = Signal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        split = QSplitter(Qt.Horizontal)
        self.items = QListWidget()
        self.items.setIconSize(QSize(72, 72))
        self.items.currentRowChanged.connect(self._show_selected)
        split.addWidget(self.items)

        detail = QFrame()
        detail.setObjectName("Panel")
        right = QVBoxLayout(detail)
        self.title = QLabel("게시물을 선택하세요")
        self.title.setObjectName("Section")
        self.title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("Subtle")
        self.meta.setWordWrap(True)
        self.content = QTextBrowser()
        self.content.setOpenExternalLinks(False)
        self.content.setPlaceholderText("게시물 본문이 여기에 표시됩니다.")
        self.image_preview = QLabel("이미지 미리보기")
        self.image_preview.setObjectName("PostPreview")
        self.image_preview.setAlignment(Qt.AlignCenter)
        self.image_preview.setMinimumHeight(130)
        self.image_preview.setMaximumHeight(210)
        self._current_thumbnail_url = ""
        self._thumbnail_workers: set[TaskThread] = set()
        self.session_cookie = ""
        self.files = QComboBox()
        self.download_button = QPushButton("선택한 파일 다운로드")
        self.download_button.setObjectName("Primary")
        self.download_button.setEnabled(False)
        self.open_button = QPushButton("Pawchive 원본 열기")
        self.open_button.setEnabled(False)
        self.download_button.clicked.connect(self._download)
        self.open_button.clicked.connect(self._open_post)
        right.addWidget(self.title)
        right.addWidget(self.meta)
        right.addWidget(QLabel("본문"))
        right.addWidget(self.content, 2)
        right.addWidget(QLabel("이미지 미리보기"))
        right.addWidget(self.image_preview)
        right.addWidget(QLabel("첨부 파일"))
        right.addWidget(self.files)
        row = QHBoxLayout()
        row.addWidget(self.download_button)
        row.addWidget(self.open_button)
        right.addLayout(row)
        split.addWidget(detail)
        split.setSizes([360, 640])
        layout.addWidget(split, 1)
        self.posts: list[Post] = []
        self.current_post: Post | None = None

    def set_session_cookie(self, value: str | None) -> None:
        self.session_cookie = value or ""
        setter = getattr(self.content, "set_session_cookie", None)
        if callable(setter):
            setter(self.session_cookie)

    def set_posts(self, posts: list[Post]) -> None:
        self._current_thumbnail_url = ""
        self.posts = posts
        self.items.clear()
        self.current_post = None
        for post in posts:
            date = post.published or post.added or "날짜 정보 없음"
            item = QListWidgetItem(f"{post.title}\n{post.service} · {post.creator_id} · {date}")
            item.setData(Qt.UserRole, post)
            self.items.addItem(item)
        if posts:
            self.items.setCurrentRow(0)
        else:
            self.title.setText("표시할 게시물이 없습니다")
            self.meta.setText("")
            self.content.clear()
            self.image_preview.setText("이미지 미리보기")
            self.image_preview.setPixmap(QPixmap())
            self.files.clear()
            self.download_button.setEnabled(False)
            self.open_button.setEnabled(False)

    def _show_selected(self, row: int) -> None:
        if row < 0 or row >= len(self.posts):
            return
        post = self.posts[row]
        self.current_post = post
        self.title.setText(post.title)
        self.meta.setText(f"{post.service} · 크리에이터 {post.creator_id} · 게시물 {post.post_id}\n게시일: {post.published or '정보 없음'}")
        self.content.setMarkdown(post.content or "(본문 없음)")
        self._load_thumbnail(post)
        self.files.clear()
        for file in post.files:
            self.files.addItem(file.name or file.path, file)
        self.download_button.setEnabled(bool(post.files))
        self.open_button.setEnabled(True)

    def _load_thumbnail(self, post: Post) -> None:
        self._current_thumbnail_url = ""
        self.image_preview.setPixmap(QPixmap())
        self.image_preview.setText("이미지 미리보기 확인 중…")
        candidates: list[tuple[str, str | None]] = []

        media_objects = [post.raw.get("file")]
        raw_attachments = post.raw.get("attachments")
        if isinstance(raw_attachments, list):
            media_objects.extend(raw_attachments)
        for media in media_objects:
            if isinstance(media, dict):
                media_name = media.get("name") or media.get("filename")
                for key in ("thumbnail", "thumbnail_url", "preview", "preview_url", "path", "url"):
                    value = media.get(key)
                    if not isinstance(value, str) or not value.strip():
                        continue
                    if key in {"path", "url"}:
                        media_suffix = Path(str(media_name or value.split("?", 1)[0])).suffix.lower()
                        if media_suffix not in {
                            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"
                        }:
                            continue
                    candidates.append((value.strip(), str(media_name) if media_name else None))

        for key in ("thumbnail", "thumbnail_url", "preview", "preview_url"):
            value = post.raw.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append((value.strip(), None))

        # Images embedded directly in HTML/Markdown post content.
        for match in re.findall(r"""<img[^>]+src=["']([^"']+)""", post.content or "", flags=re.I):
            candidates.append((match, Path(match.split("?", 1)[0]).name or None))
        for match in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", post.content or ""):
            source = match.strip().strip("<>")
            candidates.append((source, Path(source.split("?", 1)[0]).name or None))

        for attachment in post.files:
            if Path(attachment.name).suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"
            }:
                candidates.append((attachment.path, attachment.name))

        for candidate, filename in candidates:
            try:
                thumb_url = resolve_thumbnail_url(candidate, filename=filename)
            except DownloadError:
                continue
            self._current_thumbnail_url = thumb_url
            worker = TaskThread(lambda url=thumb_url, cookie=self.session_cookie or None: fetch_thumbnail_bytes(url, session_cookie=cookie))
            self._thumbnail_workers.add(worker)
            worker.succeeded.connect(
                lambda payload, expected=thumb_url: self._thumbnail_loaded(payload, expected)
            )
            worker.failed.connect(
                lambda message, expected=thumb_url: self._thumbnail_failed(message, expected)
            )
            worker.finished.connect(
                lambda item=worker: self._thumbnail_workers.discard(item)
            )
            worker.start()
            return

        self.image_preview.setText("미리보기 가능한 이미지 첨부가 없습니다.")

    def _thumbnail_failed(self, message: str, expected_url: str) -> None:
        if expected_url != self._current_thumbnail_url:
            return
        self.image_preview.setText("썸네일을 불러오지 못했습니다. 원본 게시물에서 확인해 주세요.")
        self.image_preview.setToolTip(message)

    def _thumbnail_loaded(self, payload: bytes, expected_url: str) -> None:
        if expected_url != self._current_thumbnail_url:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(payload):
            self.image_preview.setText("Pawchive 응답을 이미지로 해석할 수 없습니다.")
            return
        self.image_preview.setToolTip("")
        self.image_preview.setPixmap(pixmap.scaled(
            max(180, self.image_preview.width() - 16),
            max(100, self.image_preview.height() - 12),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        ))
        selected = self.items.currentItem()
        if selected:
            selected.setIcon(QIcon(pixmap.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)))

    def _download(self) -> None:
        if self.current_post and self.files.currentData():
            self.download_requested.emit(self.current_post, self.files.currentData())

    def _open_post(self) -> None:
        if self.current_post:
            QDesktopServices.openUrl(QUrl(self.current_post.post_url))


class SettingsPage(QWidget):
    changed = Signal()

    def __init__(self, settings: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.common_path = QLineEdit(settings.get("download_dir", ""))
        common_row = QHBoxLayout()
        common_row.addWidget(self.common_path, 1)
        browse_common = QPushButton("폴더 선택")
        browse_common.clicked.connect(lambda: self._browse(self.common_path))
        common_row.addWidget(browse_common)
        common_wrap = QWidget(); common_wrap.setLayout(common_row)
        form.addRow("공통 다운로드 폴더", common_wrap)

        self.sync_interval = QSpinBox()
        self.sync_interval.setRange(15, 1440)
        self.sync_interval.setSuffix("분")
        self.sync_interval.setValue(int(settings.get("sync_minutes", 60)))
        form.addRow("자동 동기화 간격", self.sync_interval)
        self.session_key = QLineEdit(settings.get("session_key", ""))
        self.session_key.setEchoMode(QLineEdit.Password)
        self.session_key.setPlaceholderText("선택 사항 · 403 응답을 받는 파일에만 사용")
        form.addRow("파일 서버 세션 키", self.session_key)
        layout.addLayout(form)

        layout.addWidget(QLabel("게임별 저장 폴더"))
        layout.addWidget(QLabel("게임 이름과 폴더를 등록해 두면 다운로드할 때 저장 위치로 선택할 수 있습니다. 앱은 게시물을 임의로 특정 게임에 분류하지 않습니다."))
        game_row = QHBoxLayout()
        self.game_name = QLineEdit(); self.game_name.setPlaceholderText("게임 이름")
        self.game_path = QLineEdit(); self.game_path.setPlaceholderText("게임별 저장 폴더")
        browse_game = QPushButton("폴더 선택")
        browse_game.clicked.connect(lambda: self._browse(self.game_path))
        add_game = QPushButton("경로 등록")
        add_game.clicked.connect(self._add_game_path)
        game_row.addWidget(self.game_name)
        game_row.addWidget(self.game_path, 1)
        game_row.addWidget(browse_game)
        game_row.addWidget(add_game)
        layout.addLayout(game_row)
        self.game_list = QListWidget()
        layout.addWidget(self.game_list, 1)
        remove = QPushButton("선택한 게임 경로 삭제")
        remove.clicked.connect(self._remove_game_path)
        layout.addWidget(remove)
        save = QPushButton("설정 저장")
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        layout.addWidget(save)
        self._refresh_game_list()

    def _browse(self, field: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "폴더 선택", field.text() or str(Path.home()))
        if folder:
            field.setText(folder)

    def _add_game_path(self) -> None:
        name, path = self.game_name.text().strip(), self.game_path.text().strip()
        if not name or not path:
            QMessageBox.warning(self, "입력 필요", "게임 이름과 폴더를 모두 입력해 주세요.")
            return
        self.settings.setdefault("game_paths", {})[name] = path
        self.game_name.clear(); self.game_path.clear()
        self._refresh_game_list()

    def _remove_game_path(self) -> None:
        item = self.game_list.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        self.settings.setdefault("game_paths", {}).pop(name, None)
        self._refresh_game_list()

    def _refresh_game_list(self) -> None:
        self.game_list.clear()
        for name, path in sorted(self.settings.get("game_paths", {}).items()):
            item = QListWidgetItem(f"{name}  —  {path}")
            item.setData(Qt.UserRole, name)
            self.game_list.addItem(item)

    def _save(self) -> None:
        self.settings["download_dir"] = self.common_path.text().strip()
        self.settings["sync_minutes"] = self.sync_interval.value()
        self.settings["session_key"] = self.session_key.text().strip()
        save_settings(self.settings)
        self.changed.emit()
        QMessageBox.information(self, "저장 완료", "설정을 저장했습니다.")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Pawchive Mod Finder")
        self.resize(1320, 860)
        self.settings = load_settings()
        self.database = LocalRepository(data_dir() / "pawchive.sqlite3")
        self.workers: set[TaskThread] = set()
        self.preview_creator: Creator | None = None
        self._build_ui()
        for browser in (self.following_browser, self.creator_browser, self.explore_browser):
            browser.set_session_cookie(self.settings.get("session_key") or None)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.refresh_following)
        self._reset_timer()
        self._load_creators()
        self._load_following_local()
        if self.database.followed_creators():
            QTimer.singleShot(1800, self.refresh_following)

    def _build_ui(self) -> None:
        central = QWidget(); self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 18, 20, 16)
        header = QFrame(); header.setObjectName("Header")
        header_row = QHBoxLayout(header)
        titles = QVBoxLayout()
        title = QLabel("PAWCHIVE MOD FINDER"); title.setObjectName("Hero")
        subtitle = QLabel("좋아하는 크리에이터를 팔로우하고 새 게시물과 파일을 한곳에서 확인하세요.")
        subtitle.setObjectName("Subtle")
        titles.addWidget(title); titles.addWidget(subtitle)
        header_row.addLayout(titles); header_row.addStretch()
        self.sync_label = QLabel("자동 동기화 60분")
        self.sync_label.setObjectName("Subtle")
        header_row.addWidget(self.sync_label)
        self.status = QLabel("준비됨")
        self.status.setObjectName("Subtle")
        header_row.addWidget(self.status)
        root.addWidget(header)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # Following feed
        following_page = QWidget(); following_layout = QVBoxLayout(following_page)
        top = QHBoxLayout()
        top.addWidget(QLabel("팔로우한 크리에이터의 최신 게시물"))
        top.addStretch()
        refresh = QPushButton("새로고침")
        refresh.setObjectName("Primary")
        refresh.clicked.connect(self.refresh_following)
        top.addWidget(refresh)
        following_layout.addLayout(top)
        self.following_browser = PostBrowser()
        self.following_browser.download_requested.connect(self.download_attachment)
        following_layout.addWidget(self.following_browser, 1)
        self.tabs.addTab(following_page, "팔로잉")

        # Creators
        creators_page = QWidget(); creators_layout = QVBoxLayout(creators_page)
        creator_top = QHBoxLayout()
        creator_top.addWidget(QLabel("내가 팔로우한 크리에이터"))
        creator_top.addStretch()
        self.creator_refresh = QPushButton("선택 크리에이터 게시물 새로고침")
        self.creator_refresh.clicked.connect(self.refresh_selected_creator)
        self.creator_unfollow = QPushButton("언팔로우")
        self.creator_unfollow.clicked.connect(self.unfollow_selected_creator)
        creator_top.addWidget(self.creator_refresh); creator_top.addWidget(self.creator_unfollow)
        creators_layout.addLayout(creator_top)
        splitter = QSplitter(Qt.Horizontal)
        self.creator_list = QListWidget()
        self.creator_list.currentRowChanged.connect(self._creator_selected)
        splitter.addWidget(self.creator_list)
        self.creator_browser = PostBrowser()
        self.creator_browser.download_requested.connect(self.download_attachment)
        splitter.addWidget(self.creator_browser)
        splitter.setSizes([300, 920])
        creators_layout.addWidget(splitter, 1)
        self.tabs.addTab(creators_page, "크리에이터")

        # Discover
        discover_page = QWidget(); discover_layout = QVBoxLayout(discover_page)
        discover_panel = QFrame(); discover_panel.setObjectName("Panel")
        discover_form = QVBoxLayout(discover_panel)
        discover_form.addWidget(QLabel("Pawchive 크리에이터 추가"))
        discover_form.addWidget(QLabel("프로필 주소를 붙여넣거나 서비스와 크리에이터 ID를 직접 입력하세요."))
        url_row = QHBoxLayout()
        self.profile_url = QLineEdit(); self.profile_url.setPlaceholderText("https://pawchive.pw/fanbox/user/6570768")
        self.lookup_url = QPushButton("주소 확인")
        self.lookup_url.clicked.connect(self._lookup_url)
        url_row.addWidget(self.profile_url, 1); url_row.addWidget(self.lookup_url)
        discover_form.addLayout(url_row)
        id_row = QHBoxLayout()
        self.service_input = QLineEdit(); self.service_input.setPlaceholderText("서비스 (예: fanbox)")
        self.id_input = QLineEdit(); self.id_input.setPlaceholderText("크리에이터 ID")
        self.lookup_id = QPushButton("ID로 찾기")
        self.lookup_id.clicked.connect(self._lookup_id)
        id_row.addWidget(self.service_input); id_row.addWidget(self.id_input, 1); id_row.addWidget(self.lookup_id)
        discover_form.addLayout(id_row)
        self.creator_preview = QLabel("확인된 크리에이터 정보가 여기에 표시됩니다.")
        self.creator_preview.setWordWrap(True)
        self.creator_preview.setObjectName("Subtle")
        discover_form.addWidget(self.creator_preview)
        self.follow_button = QPushButton("이 크리에이터 팔로우")
        self.follow_button.setObjectName("Primary")
        self.follow_button.setEnabled(False)
        self.follow_button.clicked.connect(self.follow_preview_creator)
        discover_form.addWidget(self.follow_button)
        discover_layout.addWidget(discover_panel)
        discover_layout.addStretch()
        self.tabs.addTab(discover_page, "크리에이터 찾기")

        # Explore
        explore_page = QWidget(); explore_layout = QVBoxLayout(explore_page)
        explore_top = QHBoxLayout()
        self.explore_query = QLineEdit(); self.explore_query.setPlaceholderText("Pawchive 전체 게시물 검색 (비워두면 최신순)")
        self.explore_query.returnPressed.connect(self.explore_posts)
        explore_button = QPushButton("게시물 검색")
        explore_button.setObjectName("Primary")
        explore_button.clicked.connect(self.explore_posts)
        explore_top.addWidget(self.explore_query, 1); explore_top.addWidget(explore_button)
        explore_layout.addLayout(explore_top)
        self.explore_browser = PostBrowser()
        self.explore_browser.download_requested.connect(self.download_attachment)
        explore_layout.addWidget(self.explore_browser, 1)
        self.tabs.addTab(explore_page, "전체 탐색")

        # Downloads
        downloads_page = QWidget(); downloads_layout = QVBoxLayout(downloads_page)
        downloads_head = QHBoxLayout()
        downloads_head.addWidget(QLabel("다운로드 기록"))
        downloads_head.addStretch()
        open_folder = QPushButton("공통 다운로드 폴더 열기")
        open_folder.clicked.connect(self.open_download_folder)
        downloads_head.addWidget(open_folder)
        downloads_layout.addLayout(downloads_head)
        self.download_history = QListWidget()
        downloads_layout.addWidget(self.download_history, 1)
        self.tabs.addTab(downloads_page, "다운로드")

        # Settings
        self.settings_page = SettingsPage(self.settings)
        self.settings_page.changed.connect(self._settings_changed)
        self.tabs.addTab(self.settings_page, "설정")
        self._refresh_download_history()

    def _reset_timer(self) -> None:
        minutes = max(15, int(self.settings.get("sync_minutes", 60)))
        self.sync_timer.start(minutes * 60 * 1000)
        self.sync_label.setText(f"자동 동기화 {minutes}분")

    def _run_task(self, label: str, task: Callable[[], object],
                  on_success: Callable[[object], None]) -> None:
        if any(worker.isRunning() for worker in self.workers):
            QMessageBox.information(self, "작업 진행 중", "현재 작업이 끝난 뒤 다시 시도해 주세요.")
            return
        self.status.setText(label)
        worker = TaskThread(task)
        self.workers.add(worker)
        worker.succeeded.connect(on_success)
        worker.failed.connect(self._task_failed)
        worker.finished.connect(lambda w=worker: self.workers.discard(w))
        worker.start()

    def _task_failed(self, message: str) -> None:
        self.status.setText("작업 실패")
        QMessageBox.warning(self, "작업 실패", message)

    def _client(self) -> PawchiveClient:
        return PawchiveClient()

    def _load_creators(self) -> None:
        selected = self.creator_list.currentItem()
        selected_key = selected.data(Qt.UserRole) if selected else None
        self.creator_list.clear()
        for row in self.database.followed_creators():
            name = row.get("name") or row["creator_id"]
            item = QListWidgetItem(f"{name}\n{row['service']} · {row['creator_id']}")
            item.setData(Qt.UserRole, (row["service"], row["creator_id"]))
            self.creator_list.addItem(item)
            if selected_key == item.data(Qt.UserRole):
                self.creator_list.setCurrentItem(item)
        if self.creator_list.count() and self.creator_list.currentRow() < 0:
            self.creator_list.setCurrentRow(0)

    def _load_following_local(self) -> None:
        rows = self.database.following_feed(limit=150)
        posts = [self._post_from_row(row) for row in rows]
        self.following_browser.set_posts(posts)

    @staticmethod
    def _post_from_row(row: dict) -> Post:
        try:
            raw = json.loads(row.get("raw_json") or "{}")
        except ValueError:
            raw = {}
        raw.setdefault("id", row.get("post_id"))
        raw.setdefault("service", row.get("service"))
        raw.setdefault("user", row.get("creator_id"))
        raw.setdefault("title", row.get("title"))
        raw.setdefault("content", row.get("content"))
        raw.setdefault("published", row.get("published"))
        raw.setdefault("added", row.get("added"))
        return Post.from_api(raw, Creator(service=row["service"], creator_id=row["creator_id"]))

    def refresh_following(self) -> None:
        creators = self.database.followed_creators()
        if not creators:
            self.status.setText("팔로우한 크리에이터가 없습니다.")
            self._load_following_local()
            return

        def task():
            total, failed = 0, []
            with self._client() as client:
                for creator in creators:
                    service, creator_id = creator["service"], creator["creator_id"]
                    try:
                        posts = client.all_creator_posts(service, creator_id)
                        total += self.database.save_posts(posts)
                        self.database.update_sync_state(service, creator_id, 50, datetime.now().isoformat(), "")
                    except Exception as exc:
                        failed.append(f"{creator.get('name') or creator_id}: {exc}")
                        self.database.update_sync_state(service, creator_id, 0, "", str(exc))
            return total, failed

        def done(result):
            total, failed = result
            self._load_following_local()
            self._load_creators()
            self.status.setText(f"동기화 완료 · 게시물 {total}개 확인")
            if failed:
                QMessageBox.warning(self, "일부 동기화 실패", "\n".join(failed[:8]))

        self._run_task("팔로잉 게시물 동기화 중…", task, done)

    def _creator_selected(self, row: int) -> None:
        item = self.creator_list.item(row)
        if not item:
            self.creator_browser.set_posts([])
            return
        service, creator_id = item.data(Qt.UserRole)
        rows = self.database.posts_for_creator(service, creator_id, limit=150)
        self.creator_browser.set_posts([self._post_from_row(record) for record in rows])

    def refresh_selected_creator(self) -> None:
        item = self.creator_list.currentItem()
        if not item:
            QMessageBox.information(self, "크리에이터 선택", "먼저 크리에이터를 선택하세요.")
            return
        service, creator_id = item.data(Qt.UserRole)

        def task():
            with self._client() as client:
                posts = client.creator_posts(service, creator_id, offset=0)
            self.database.save_posts(posts)
            self.database.update_sync_state(service, creator_id, 50, datetime.now().isoformat(), "")
            return posts

        def done(posts):
            rows = self.database.posts_for_creator(service, creator_id, limit=150)
            self.creator_browser.set_posts([self._post_from_row(row) for row in rows])
            self._load_following_local()
            self.status.setText(f"{len(posts)}개 게시물을 가져왔습니다.")

        self._run_task("크리에이터 게시물 가져오는 중…", task, done)

    def unfollow_selected_creator(self) -> None:
        item = self.creator_list.currentItem()
        if not item:
            return
        service, creator_id = item.data(Qt.UserRole)
        self.database.unfollow(service, creator_id)
        self._load_creators()
        self._load_following_local()
        self.status.setText("언팔로우했습니다.")

    def _lookup_url(self) -> None:
        try:
            service, creator_id = parse_creator_url(self.profile_url.text())
        except ValueError as exc:
            QMessageBox.warning(self, "주소 확인", str(exc))
            return
        self.service_input.setText(service)
        self.id_input.setText(creator_id)
        self._lookup_creator(service, creator_id)

    def _lookup_id(self) -> None:
        self._lookup_creator(self.service_input.text(), self.id_input.text())

    def _lookup_creator(self, service: str, creator_id: str) -> None:
        def task():
            with self._client() as client:
                return client.resolve_creator(service, creator_id)

        def done(creator):
            self.preview_creator = creator
            self.creator_preview.setText(
                f"이름: {creator.name or '(이름 없음)'}\n서비스: {creator.service}\nID: {creator.creator_id}\n"
                f"프로필: {creator.profile_url}"
            )
            self.follow_button.setEnabled(True)
            self.status.setText("크리에이터 확인 완료")

        self.follow_button.setEnabled(False)
        self._run_task("크리에이터 확인 중…", task, done)

    def follow_preview_creator(self) -> None:
        creator = self.preview_creator
        if not creator:
            return
        self.database.save_creator(creator)
        self.database.follow(creator.service, creator.creator_id)
        self._load_creators()
        self._load_following_local()
        self.follow_button.setEnabled(False)
        self.status.setText(f"{creator.name or creator.creator_id} 팔로우 완료")

    def explore_posts(self) -> None:
        query = self.explore_query.text().strip()
        if query and len(query) < 3:
            QMessageBox.information(self, "검색어 길이", "검색어는 3글자 이상 입력하세요.")
            return

        def task():
            with self._client() as client:
                posts = client.all_recent_posts(query or None)
            self.database.save_posts(posts)
            return posts

        def done(posts):
            self.explore_browser.set_posts(posts)
            self.status.setText(f"전체 탐색 결과 {len(posts)}개")

        self._run_task("Pawchive 전체 게시물 검색 중…", task, done)

    def download_attachment(self, post: Post, attachment: Attachment) -> None:
        paths = {"공통 저장 폴더": self.settings.get("download_dir", "")}
        for name, folder in self.settings.get("game_paths", {}).items():
            paths[f"{name} 저장 폴더"] = folder
        options = [label for label, folder in paths.items() if folder]
        if not options:
            QMessageBox.warning(self, "저장 폴더 없음", "설정에서 공통 저장 폴더 또는 게임별 저장 폴더를 지정하세요.")
            self.tabs.setCurrentWidget(self.settings_page)
            return
        choice, accepted = QInputDialog.getItem(
            self, "저장 위치 선택", "이 파일을 저장할 폴더를 선택하세요. 게임은 자동 분류하지 않습니다.",
            options, 0, False,
        )
        if not accepted:
            return
        folder = Path(paths[choice]).expanduser()
        raw_name = Path(attachment.name or attachment.path.rsplit("/", 1)[-1]).name
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw_name).strip(" .") or "download.bin"
        target = folder / filename
        folder.mkdir(parents=True, exist_ok=True)

        def task():
            saved = download_file(
                attachment.path, target,
                session_cookie=self.settings.get("session_key") or None,
                filename=attachment.name,
            )
            extracted = []
            try:
                extracted = safe_extract_archive(saved, saved.parent / (saved.stem + "_extracted"))
            except (ValueError, DownloadError, OSError) as exc:
                # Non-archive files are expected. ZIP safety failures are surfaced to the user.
                if saved.suffix.lower() in {".zip", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2", ".txz"}:
                    raise
            self.database.record_download(
                post.service, post.creator_id, post.post_id, str(saved),
                str(folder), "complete", "",
            )
            return saved, extracted

        def done(result):
            saved, extracted = result
            self._refresh_download_history()
            message = f"다운로드 완료: {saved}"
            if extracted:
                message += f"\n압축 해제: {len(extracted)}개 파일"
            self.status.setText("다운로드 완료")
            QMessageBox.information(self, "다운로드 완료", message)

        self._run_task(f"{filename} 다운로드 중…", task, done)

    def _refresh_download_history(self) -> None:
        self.download_history.clear()
        try:
            with sqlite3.connect(self.database.path) as db:
                rows = db.execute(
                    "SELECT file_path, status, error, created_at FROM downloads ORDER BY id DESC LIMIT 200"
                ).fetchall()
        except sqlite3.Error:
            rows = []
        for file_path, status, error, created_at in rows:
            item = QListWidgetItem(f"[{status}] {Path(file_path).name} · {created_at}\n{file_path}\n{error}")
            self.download_history.addItem(item)

    def open_download_folder(self) -> None:
        folder = Path(self.settings.get("download_dir", "")).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _settings_changed(self) -> None:
        self.settings = self.settings_page.settings
        for browser in (self.following_browser, self.creator_browser, self.explore_browser):
            browser.set_session_cookie(self.settings.get("session_key") or None)
        self._reset_timer()
        self.status.setText("설정을 저장했습니다.")


def run_app() -> int:
    app = QApplication([])
    app.setApplicationName("Pawchive Mod Finder")
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    return app.exec()
