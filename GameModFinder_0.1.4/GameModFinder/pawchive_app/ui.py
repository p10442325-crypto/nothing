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
    QVBoxLayout, QWidget, QInputDialog, QStackedWidget, QListView, QGridLayout, QScrollArea,
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
QListWidget#PostList::item { background:#182230; border:1px solid #2b3b50; border-radius:12px; padding:8px; margin:3px 0px; }
QFrame#AttachmentCard { background:#182230; border:1px solid #304158; border-radius:9px; }
QTextBrowser#PostContent { background:#101822; border:1px solid #334258; border-radius:10px; padding:14px; font-size:11pt; }
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

    ARCHIVE_SUFFIXES = {
        ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz",
        ".tgz", ".tbz2", ".txz", ".tar.gz", ".tar.bz2", ".tar.xz",
    }
    IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
    MAX_THUMBNAIL_WORKERS = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)

        self.list_page = QWidget()
        list_layout = QVBoxLayout(self.list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_header = QHBoxLayout()
        heading = QLabel("게시물")
        heading.setObjectName("Section")
        self.list_count = QLabel("0개")
        self.list_count.setObjectName("Subtle")
        list_header.addWidget(heading)
        list_header.addStretch()
        list_header.addWidget(self.list_count)
        list_layout.addLayout(list_header)

        self.search_input = QLineEdit(self.list_page)
        self.search_input.setPlaceholderText("게시물 제목 · 본문 · 크리에이터 · 첨부파일 검색")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.filter_posts)
        list_layout.addWidget(self.search_input)

        # Rectangular image cards with wrapped titles.
        self.items = QListWidget()
        self.items.setObjectName("PostList")
        self.items.setViewMode(QListView.IconMode)
        self.items.setFlow(QListView.LeftToRight)
        self.items.setWrapping(True)
        self.items.setResizeMode(QListView.Adjust)
        self.items.setMovement(QListView.Static)
        self.items.setLayoutMode(QListView.Batched)
        self.items.setBatchSize(48)
        self.items.setWordWrap(True)
        self.items.setUniformItemSizes(True)
        self.items.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.items.setIconSize(QSize(176, 126))
        self.items.setGridSize(QSize(216, 204))
        self.items.setSpacing(0)
        self.items.verticalScrollBar().setSingleStep(24)
        self.items.verticalScrollBar().setPageStep(160)
        self.items.itemClicked.connect(self._open_item)
        self.items.verticalScrollBar().valueChanged.connect(self._schedule_visible_thumbnails)
        self.list_empty = QLabel("압축파일이 첨부된 게시물이 없습니다.")
        self.list_empty.setObjectName("Subtle")
        self.list_empty.setAlignment(Qt.AlignCenter)
        self.list_content = QStackedWidget(self.list_page)
        self.list_content.addWidget(self.items)
        self.list_content.addWidget(self.list_empty)
        list_layout.addWidget(self.list_content, 1)
        self.stack.addWidget(self.list_page)

        self.detail_page = QWidget()
        detail_layout = QVBoxLayout(self.detail_page)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)

        navigation = QHBoxLayout()
        self.back_button = QPushButton("← 게시물 목록으로")
        self.back_button.clicked.connect(self._back_to_list)
        navigation.addWidget(self.back_button)
        navigation.addStretch()
        self.open_button = QPushButton("Pawchive 원본 열기")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_post)
        navigation.addWidget(self.open_button)
        detail_layout.addLayout(navigation)

        self.title = QLabel("게시물을 선택하세요")
        self.title.setObjectName("Hero")
        self.title.setWordWrap(True)
        self.title.hide()
        self.meta = QLabel("")
        self.meta.setObjectName("Subtle")
        self.meta.setWordWrap(True)
        self.meta.hide()

        # Each attachment has its own download action; no dropdown selection.
        self.attachments_heading = QLabel("첨부 파일")
        self.attachments_heading.setObjectName("Section")
        detail_layout.addWidget(self.attachments_heading)
        self.attachments_area = QScrollArea()
        self.attachments_area.setWidgetResizable(True)
        self.attachments_area.setMaximumHeight(104)
        self.attachments_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.attachments_host = QWidget()
        self.attachments_grid = QGridLayout(self.attachments_host)
        self.attachments_grid.setContentsMargins(2, 2, 2, 2)
        self.attachments_grid.setSpacing(8)
        self.attachments_area.setWidget(self.attachments_host)
        detail_layout.addWidget(self.attachments_area)

        self.content = QTextBrowser()
        self.content.setObjectName("PostContent")
        self.content.setOpenExternalLinks(False)
        self.content.setPlaceholderText("게시물 본문이 여기에 표시됩니다.")
        detail_layout.addWidget(self.content, 1)

        # Compatibility placeholder: attached images render inline in the body.
        self.image_preview = QLabel("이미지 미리보기", self.detail_page)
        self.image_preview.setObjectName("PostPreview")
        self.image_preview.hide()

        self.session_cookie = ""
        self.posts: list[Post] = []
        self.current_post: Post | None = None
        self.attachment_buttons: list[QPushButton] = []

        self._thumbnail_generation = 0
        self._thumbnail_items: dict[str, list[QListWidgetItem]] = {}
        self._thumbnail_queue: list[tuple[str, int]] = []
        self._thumbnail_pending: set[str] = set()
        self._thumbnail_loaded: set[str] = set()
        self._thumbnail_failed_urls: set[str] = set()
        self._thumbnail_workers: set[TaskThread] = set()
        self._thumbnail_worker_urls: dict[TaskThread, str] = {}
        self._thumbnail_debounce = QTimer(self)
        self._thumbnail_debounce.setSingleShot(True)
        self._thumbnail_debounce.setInterval(100)
        self._thumbnail_debounce.timeout.connect(self._queue_visible_thumbnails)
        self.stack.addWidget(self.detail_page)
        self._card_icon_width = 176
        self._card_icon_height = 126
        self.stack.setCurrentWidget(self.list_page)

    @classmethod
    def _is_archive(cls, filename: str) -> bool:
        lowered = (filename or "").split("?", 1)[0].lower()
        return any(lowered.endswith(suffix) for suffix in cls.ARCHIVE_SUFFIXES)

    @classmethod
    def _is_image(cls, filename: str) -> bool:
        return Path(filename or "").suffix.lower() in cls.IMAGE_SUFFIXES

    @classmethod
    def _thumbnail_url_for_post(cls, post: Post) -> str:
        candidates: list[tuple[str, str | None]] = []
        media_objects = [post.raw.get("file")]
        raw_attachments = post.raw.get("attachments")
        if isinstance(raw_attachments, list):
            media_objects.extend(raw_attachments)

        for media in media_objects:
            if not isinstance(media, dict):
                continue
            name = media.get("name") or media.get("filename")
            for key in ("thumbnail", "thumbnail_url", "preview", "preview_url", "path", "url"):
                value = media.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                if key in {"path", "url"} and not cls._is_image(str(name or value.split("?", 1)[0])):
                    continue
                candidates.append((value.strip(), str(name) if name else None))

        for key in ("thumbnail", "thumbnail_url", "preview", "preview_url"):
            value = post.raw.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append((value.strip(), None))

        for match in re.findall(r"""<img[^>]+src=["']([^"']+)""", post.content or "", flags=re.I):
            candidates.append((match, Path(match.split("?", 1)[0]).name or None))
        for match in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", post.content or ""):
            source = match.strip().strip("<>")
            candidates.append((source, Path(source.split("?", 1)[0]).name or None))

        for attachment in post.files:
            if cls._is_image(attachment.name):
                candidates.append((attachment.path, attachment.name))

        for candidate, filename in candidates:
            try:
                return resolve_thumbnail_url(candidate, filename=filename)
            except DownloadError:
                continue
        return ""

    def set_session_cookie(self, value: str | None) -> None:
        self.session_cookie = value or ""
        setter = getattr(self.content, "set_session_cookie", None)
        if callable(setter):
            setter(self.session_cookie)

    def set_posts(self, posts: list[Post]) -> None:
        self._thumbnail_generation += 1
        generation = self._thumbnail_generation
        self._thumbnail_queue.clear()
        self._thumbnail_pending.clear()
        self._thumbnail_loaded.clear()
        self._thumbnail_failed_urls.clear()
        self._thumbnail_items.clear()

        posts = [
            post for post in posts
            if any(
                self._is_archive(attachment.name) or self._is_archive(attachment.path)
                for attachment in post.files
            )
        ]
        self.posts = posts
        self.items.clear()
        self.current_post = None
        self.search_input.clear()
        self.stack.setCurrentWidget(self.list_page)

        for post in posts:
            item = QListWidgetItem(post.title or "(제목 없음)")
            item.setData(Qt.UserRole, post)
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            item.setToolTip(
                f"{post.service} · 크리에이터 {post.creator_id} · "
                f"{post.published or '게시일 정보 없음'}"
            )
            thumb_url = self._thumbnail_url_for_post(post)
            item.setData(Qt.UserRole + 1, thumb_url)
            if thumb_url:
                self._thumbnail_items.setdefault(thumb_url, []).append(item)
            self.items.addItem(item)

        self.filter_posts("")
        self.title.setText("게시물을 선택하세요")
        self.meta.clear()
        self.content.clear()
        self._clear_attachment_cards()
        self.open_button.setEnabled(False)
        QTimer.singleShot(0, lambda expected=generation: self._refresh_card_layout(expected))

    def filter_posts(self, query: str = "") -> None:
        """Filter the loaded archive-post list without discarding cached posts."""
        needle = " ".join((query or "").casefold().split())
        visible_count = 0
        for index, post in enumerate(self.posts):
            item = self.items.item(index)
            if item is None:
                continue
            attachment_text = " ".join(
                f"{attachment.name} {attachment.path}" for attachment in post.files
            )
            searchable = " ".join((
                post.title, post.content, post.service, post.creator_id,
                post.post_id, attachment_text,
            )).casefold()
            visible = not needle or needle in searchable
            item.setHidden(not visible)
            if visible:
                visible_count += 1

        self.list_count.setText(
            f"{visible_count:,} / {len(self.posts):,}개" if needle
            else f"{len(self.posts):,}개"
        )
        if not self.posts:
            self.list_empty.setText("압축파일이 첨부된 게시물이 없습니다.")
            self.list_content.setCurrentWidget(self.list_empty)
        elif visible_count == 0:
            self.list_empty.setText("검색 결과가 없습니다.")
            self.list_content.setCurrentWidget(self.list_empty)
        else:
            self.list_content.setCurrentWidget(self.items)
        self._thumbnail_debounce.start()

    def _refresh_card_layout(self, generation: int) -> None:
        if generation != self._thumbnail_generation:
            return
        self._update_card_grid()
        self._queue_visible_thumbnails(generation)

    def _schedule_visible_thumbnails(self, *_args) -> None:
        self._thumbnail_debounce.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_thumbnail_debounce"):
            self._update_card_grid()
            self._thumbnail_debounce.start()

    def _update_card_grid(self) -> None:
        if not hasattr(self, "items"):
            return
        available = max(1, self.items.viewport().width())
        target_cell_width = 270
        # Round up the column count so all cards distribute across the available row.
        columns = max(1, (available + target_cell_width - 1) // target_cell_width)
        cell_width = max(1, available // columns)
        icon_width = max(72, min(260, cell_width - 12))
        icon_height = max(60, int(icon_width * 0.70))
        cell_height = icon_height + 78
        self._card_icon_width = icon_width
        self._card_icon_height = icon_height
        self.items.setGridSize(QSize(cell_width, cell_height))
        self.items.setIconSize(QSize(icon_width, icon_height))
        for index in range(self.items.count()):
            self.items.item(index).setSizeHint(QSize(cell_width, cell_height))

    def _queue_visible_thumbnails(self, expected_generation: int | None = None) -> None:
        generation = self._thumbnail_generation if expected_generation is None else expected_generation
        if generation != self._thumbnail_generation or not self.posts:
            return
        viewport = self.items.viewport().rect()
        buffer = viewport.adjusted(0, -self.items.gridSize().height(), 0, self.items.gridSize().height() * 2)
        queued = False
        for index in range(self.items.count()):
            item = self.items.item(index)
            if item.isHidden():
                continue
            url = item.data(Qt.UserRole + 1)
            if not url or url in self._thumbnail_pending or url in self._thumbnail_loaded or url in self._thumbnail_failed_urls:
                continue
            rect = self.items.visualItemRect(item)
            if rect.isValid() and buffer.intersects(rect):
                self._thumbnail_pending.add(url)
                self._thumbnail_queue.append((url, generation))
                queued = True
        if not queued and not self._thumbnail_queue and not self._thumbnail_workers:
            # First layout can happen before the viewport has a useful geometry.
            for index in range(min(self.items.count(), 12)):
                item = self.items.item(index)
                if item.isHidden():
                    continue
                url = item.data(Qt.UserRole + 1)
                if url and url not in self._thumbnail_pending and url not in self._thumbnail_loaded and url not in self._thumbnail_failed_urls:
                    self._thumbnail_pending.add(url)
                    self._thumbnail_queue.append((url, generation))
        self._pump_thumbnail_queue()

    def _pump_thumbnail_queue(self) -> None:
        while self._thumbnail_queue and len(self._thumbnail_workers) < self.MAX_THUMBNAIL_WORKERS:
            url, generation = self._thumbnail_queue.pop(0)
            if generation != self._thumbnail_generation:
                self._thumbnail_pending.discard(url)
                continue
            worker = TaskThread(
                lambda image_url=url, cookie=self.session_cookie or None:
                    fetch_thumbnail_bytes(image_url, session_cookie=cookie)
            )
            self._thumbnail_workers.add(worker)
            self._thumbnail_worker_urls[worker] = url
            worker.succeeded.connect(
                lambda payload, image_url=url, gen=generation:
                    self._thumbnail_loaded_for_card(image_url, payload, gen)
            )
            worker.failed.connect(
                lambda message, image_url=url, gen=generation:
                    self._thumbnail_failed_for_card(image_url, message, gen)
            )
            worker.finished.connect(
                lambda item=worker, image_url=url, gen=generation:
                    self._thumbnail_worker_finished(item, image_url, gen)
            )
            worker.start()

    def _thumbnail_loaded_for_card(self, url: str, payload: bytes, generation: int) -> None:
        if generation != self._thumbnail_generation:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(payload):
            self._thumbnail_failed_urls.add(url)
            return
        width, height = self._card_icon_width, self._card_icon_height
        scaled = pixmap.scaled(
            QSize(width, height),
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        left = max(0, (scaled.width() - width) // 2)
        top = max(0, (scaled.height() - height) // 2)
        icon = QIcon(scaled.copy(left, top, width, height))
        for item in self._thumbnail_items.get(url, []):
            item.setIcon(icon)
        self._thumbnail_loaded.add(url)

    def _thumbnail_failed_for_card(self, url: str, message: str, generation: int) -> None:
        if generation != self._thumbnail_generation:
            return
        self._thumbnail_failed_urls.add(url)
        for item in self._thumbnail_items.get(url, []):
            item.setToolTip(item.toolTip() + "\n미리보기를 불러오지 못했습니다: " + message)

    def _thumbnail_worker_finished(self, worker: TaskThread, url: str, generation: int) -> None:
        self._thumbnail_workers.discard(worker)
        self._thumbnail_worker_urls.pop(worker, None)
        if generation == self._thumbnail_generation:
            self._thumbnail_pending.discard(url)
        worker.deleteLater()
        self._pump_thumbnail_queue()

    def _open_item(self, item: QListWidgetItem) -> None:
        self._show_selected(self.items.row(item))

    def _show_selected(self, row: int) -> None:
        if row < 0 or row >= len(self.posts):
            return
        post = self.posts[row]
        self.current_post = post
        self.title.setText(post.title)
        self.meta.setText(
            f"{post.service} · 크리에이터 {post.creator_id} · 게시물 {post.post_id}\n"
            f"게시일: {post.published or '정보 없음'}"
        )
        self.content.setMarkdown(post.content or "(본문 없음)")
        self._populate_attachment_cards(post)
        self.open_button.setEnabled(True)
        self.stack.setCurrentWidget(self.detail_page)

    def _clear_attachment_cards(self) -> None:
        self.attachment_buttons.clear()
        while self.attachments_grid.count():
            item = self.attachments_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.attachments_heading.setVisible(False)
        self.attachments_area.setVisible(False)

    def _populate_attachment_cards(self, post: Post) -> None:
        self._clear_attachment_cards()
        archive_files = [
            attachment for attachment in post.files
            if self._is_archive(attachment.name) or self._is_archive(attachment.path)
        ]
        if not archive_files:
            return
        self.attachments_heading.setVisible(True)
        self.attachments_area.setVisible(True)
        for index, attachment in enumerate(archive_files):
            card = QFrame()
            card.setObjectName("AttachmentCard")
            row = QHBoxLayout(card)
            row.setContentsMargins(9, 7, 9, 7)
            icon = QLabel("📦")
            icon.setMinimumWidth(28)
            filename = QLabel(attachment.name or attachment.path.rsplit("/", 1)[-1])
            filename.setWordWrap(True)
            filename.setToolTip(attachment.path)
            button = QPushButton("다운로드")
            button.setObjectName("Primary")
            button.clicked.connect(
                lambda checked=False, selected=attachment, source=post:
                    self.download_requested.emit(source, selected)
            )
            row.addWidget(icon)
            row.addWidget(filename, 1)
            row.addWidget(button)
            grid_row, grid_col = divmod(index, 2)
            self.attachments_grid.addWidget(card, grid_row, grid_col)
            self.attachment_buttons.append(button)

    def _back_to_list(self) -> None:
        self.stack.setCurrentWidget(self.list_page)
        self.items.setFocus()

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
        self.download_workers: set[TaskThread] = set()
        self.preview_creator: Creator | None = None
        self._creator_tag_owner: tuple[str, str] | None = None
        self._creator_tag_request: tuple[str, str] | None = None
        self._creator_tag_workers: set[TaskThread] = set()
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
        root.setContentsMargins(10, 8, 10, 8)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # Keep operational status compact without the large product banner.
        self.sync_label = QLabel("자동 동기화 60분")
        self.sync_label.setObjectName("Subtle")
        self.status = QLabel("준비됨")
        self.status.setObjectName("Subtle")
        status_corner = QWidget()
        status_row = QHBoxLayout(status_corner)
        status_row.setContentsMargins(0, 0, 8, 0)
        status_row.setSpacing(12)
        status_row.addWidget(self.sync_label)
        status_row.addWidget(self.status)
        self.tabs.setCornerWidget(status_corner, Qt.TopRightCorner)

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
        creator_tag_row = QHBoxLayout()
        creator_tag_row.addWidget(QLabel("이 크리에이터의 태그"))
        self.creator_tag_combo = QComboBox()
        self.creator_tag_combo.addItem("전체 게시물", "")
        self.creator_tag_combo.setEnabled(False)
        self.creator_tag_combo.currentIndexChanged.connect(self._creator_tag_changed)
        creator_tag_row.addWidget(self.creator_tag_combo, 1)
        creators_layout.addLayout(creator_tag_row)
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

    def _run_download_task(self, label: str, task: Callable[[], object],
                           on_success: Callable[[object], None]) -> None:
        active = [worker for worker in self.download_workers if worker.isRunning()]
        if len(active) >= 3:
            QMessageBox.information(
                self, "다운로드 대기",
                "동시에 진행할 수 있는 파일 다운로드는 최대 3개입니다. "
                "하나가 끝나면 다시 시도해 주세요.",
            )
            return
        self.status.setText(f"{label} · 동시 다운로드 {len(active) + 1}/3")
        worker = TaskThread(task)
        self.download_workers.add(worker)
        worker.succeeded.connect(on_success)
        worker.failed.connect(self._task_failed)
        worker.finished.connect(lambda current=worker: self.download_workers.discard(current))
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
        rows = self.database.following_feed()
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
                        self.database.update_sync_state(service, creator_id, len(posts), datetime.now().isoformat(), "")
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
        self.creator_tag_combo.blockSignals(True)
        self.creator_tag_combo.clear()
        self.creator_tag_combo.addItem("전체 게시물", "")
        self.creator_tag_combo.setEnabled(False)
        self.creator_tag_combo.blockSignals(False)
        self._creator_tag_owner = None
        if not item:
            self.creator_browser.set_posts([])
            return
        service, creator_id = item.data(Qt.UserRole)
        self._creator_tag_owner = (service, creator_id)
        rows = self.database.posts_for_creator(service, creator_id)
        self.creator_browser.set_posts([self._post_from_row(record) for record in rows])
        self._load_creator_tags(service, creator_id)

    def _load_creator_tags(self, service: str, creator_id: str) -> None:
        owner = (service, creator_id)
        self._creator_tag_request = owner

        def task():
            with self._client() as client:
                return client.creator_tags(service, creator_id)

        worker = TaskThread(task)
        self._creator_tag_workers.add(worker)

        def done(tags):
            if self._creator_tag_owner != owner or self._creator_tag_request != owner:
                return
            self.creator_tag_combo.blockSignals(True)
            self.creator_tag_combo.clear()
            self.creator_tag_combo.addItem("전체 게시물", "")
            for tag in tags:
                self.creator_tag_combo.addItem(tag, tag)
            self.creator_tag_combo.setEnabled(bool(tags))
            self.creator_tag_combo.blockSignals(False)
            self.status.setText(
                f"{owner[1]} 태그 {len(tags)}개 확인" if tags else "크리에이터 태그가 없습니다."
            )

        def failed(message):
            if self._creator_tag_owner == owner:
                self.creator_tag_combo.setEnabled(False)
                self.status.setText("크리에이터 태그를 불러오지 못했습니다: " + message)

        worker.succeeded.connect(done)
        worker.failed.connect(failed)
        worker.finished.connect(lambda current=worker: self._creator_tag_workers.discard(current))
        worker.start()

    def _creator_tag_changed(self, _index: int) -> None:
        item = self.creator_list.currentItem()
        tag = self.creator_tag_combo.currentData()
        if not item or not tag:
            if item:
                service, creator_id = item.data(Qt.UserRole)
                rows = self.database.posts_for_creator(service, creator_id)
                self.creator_browser.set_posts([self._post_from_row(row) for row in rows])
            return

        service, creator_id = item.data(Qt.UserRole)
        owner = (service, creator_id)

        def task():
            with self._client() as client:
                posts = client.all_creator_posts(service, creator_id, tag=tag)
            self.database.save_posts(posts)
            return posts

        def done(posts):
            if self._creator_tag_owner != owner or self.creator_tag_combo.currentData() != tag:
                return
            self.creator_browser.set_posts(posts)
            self.status.setText(f"태그 '{tag}' 게시물 {len(posts)}개")

        self._run_task(f"'{tag}' 태그 게시물 불러오는 중…", task, done)

    def refresh_selected_creator(self) -> None:
        item = self.creator_list.currentItem()
        if not item:
            QMessageBox.information(self, "크리에이터 선택", "먼저 크리에이터를 선택하세요.")
            return
        service, creator_id = item.data(Qt.UserRole)

        def task():
            with self._client() as client:
                posts = client.all_creator_posts(service, creator_id)
            self.database.save_posts(posts)
            self.database.update_sync_state(service, creator_id, len(posts), datetime.now().isoformat(), "")
            return posts

        def done(posts):
            rows = self.database.posts_for_creator(service, creator_id)
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
