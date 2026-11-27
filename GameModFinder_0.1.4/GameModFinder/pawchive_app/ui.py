from __future__ import annotations

import json
import os
import re
import sqlite3
from urllib.parse import quote
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QUrl, QSize
from PySide6.QtGui import QDesktopServices, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QPlainTextEdit, QTextBrowser, QProgressBar, QSpinBox, QSplitter, QTabWidget,
    QVBoxLayout, QWidget, QInputDialog, QStackedWidget, QListView, QGridLayout, QScrollArea,
)

from .api import PawchiveClient, PawchiveError
from .downloads import (DownloadError, download_file, fetch_thumbnail_bytes, resolve_thumbnail_url, safe_extract_archive)
from .models import Attachment, Creator, Post, parse_creator_url, post_matches_search_fields
from .creator_discovery import (
    game_search_terms, load_game_catalog, parse_search_queries, scan_mod_creator_candidates,
)
from .repository import LocalRepository
from .updater import UpdateCheck, UpdateError, UpdateManager, UpdatePackage


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
    progress = Signal(str)

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
    load_more_requested = Signal()

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
        self.load_more_button = QPushButton("다음 페이지 불러오기", self.list_page)
        self.load_more_button.setObjectName("Primary")
        self.load_more_button.clicked.connect(self.load_more_requested.emit)
        self.load_more_button.hide()
        list_layout.addWidget(self.load_more_button)
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
        self.attachments_area.setMaximumHeight(132)
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
        self._pending_layout_generation = 0
        self._layout_refresh_timer = QTimer(self)
        self._layout_refresh_timer.setSingleShot(True)
        self._layout_refresh_timer.timeout.connect(self._refresh_pending_card_layout)
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
        """Replace the current feed and reset any in-progress thumbnail work."""
        self._thumbnail_generation += 1
        self._thumbnail_queue.clear()
        self._thumbnail_pending.clear()
        self._thumbnail_loaded.clear()
        self._thumbnail_failed_urls.clear()
        self._thumbnail_items.clear()

        self.posts = []
        self.items.clear()
        self.current_post = None
        self.search_input.clear()
        self.stack.setCurrentWidget(self.list_page)
        self.set_pagination(False)

        self.append_posts(posts)
        self.title.setText("게시물을 선택하세요")
        self.meta.clear()
        self.content.clear()
        self._clear_attachment_cards()
        self.open_button.setEnabled(False)

    def append_posts(self, posts: list[Post]) -> None:
        """Append one API page without clearing the current search or loaded cards."""
        existing = {
            (post.service, post.creator_id, post.post_id)
            for post in self.posts
        }
        for post in posts:
            if not any(
                self._is_archive(attachment.name) or self._is_archive(attachment.path)
                for attachment in post.files
            ):
                continue
            key = (post.service, post.creator_id, post.post_id)
            if key in existing:
                continue
            existing.add(key)
            self.posts.append(post)

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

        self.filter_posts(self.search_input.text())
        self._pending_layout_generation = self._thumbnail_generation
        self._layout_refresh_timer.start(0)

    def set_pagination(self, has_more: bool, loading: bool = False) -> None:
        """Show a single explicit next-page action for paginated feeds."""
        self.load_more_button.setVisible(bool(has_more or loading))
        self.load_more_button.setEnabled(bool(has_more) and not loading)
        self.load_more_button.setText(
            "다음 페이지 불러오는 중…" if loading else "다음 페이지 불러오기"
        )

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

    def _refresh_pending_card_layout(self) -> None:
        self._refresh_card_layout(self._pending_layout_generation)

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
    check_updates_requested = Signal()
    apply_update_requested = Signal()

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

        update_panel = QFrame()
        update_panel.setObjectName("Panel")
        update_layout = QVBoxLayout(update_panel)
        update_title = QLabel("프로그램 업데이트")
        update_title.setObjectName("Section")
        update_layout.addWidget(update_title)
        self.update_status = QLabel("업데이트 확인 전")
        self.update_status.setObjectName("Subtle")
        self.update_status.setWordWrap(True)
        update_layout.addWidget(self.update_status)
        update_buttons = QHBoxLayout()
        self.check_updates_button = QPushButton("업데이트 확인")
        self.check_updates_button.clicked.connect(
            lambda: self.check_updates_requested.emit()
        )
        self.apply_update_button = QPushButton("업데이트 적용 후 재시작")
        self.apply_update_button.setObjectName("Primary")
        self.apply_update_button.setEnabled(False)
        self.apply_update_button.hide()
        self.apply_update_button.clicked.connect(
            lambda: self.apply_update_requested.emit()
        )
        update_buttons.addWidget(self.check_updates_button)
        update_buttons.addWidget(self.apply_update_button)
        update_layout.addLayout(update_buttons)
        layout.addWidget(update_panel)
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
        self._available_update: UpdateCheck | None = None
        self._prepared_update: UpdatePackage | None = None
        self.update_manager = UpdateManager(
            data_dir(),
            Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
        )
        self._explore_query_active = ""
        self._explore_search_title_active = True
        self._explore_search_content_active = True
        self._explore_next_offset = 0
        self._explore_has_more = False
        self._explore_loading = False
        self._mod_candidate_rows: list[dict] = []
        self._selected_mod_candidate: dict | None = None
        self._build_ui()
        for browser in (self.following_browser, self.creator_browser, self.explore_browser):
            browser.set_session_cookie(self.settings.get("session_key") or None)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.refresh_following)
        self._reset_timer()
        self._load_creators()
        self._load_following_local()
        self._refresh_mod_candidates()
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
        self.discover_page = discover_page
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
        self.explore_query = QLineEdit()
        self.explore_query.setPlaceholderText("캐릭터 이름/영문 별칭 등 Pawchive 게시물 검색")
        self.explore_query.returnPressed.connect(self.explore_posts)
        explore_button = QPushButton("게시물 검색")
        explore_button.setObjectName("Primary")
        explore_button.clicked.connect(self.explore_posts)
        explore_top.addWidget(self.explore_query, 1)
        explore_top.addWidget(explore_button)
        explore_layout.addLayout(explore_top)

        search_scope = QHBoxLayout()
        search_scope.addWidget(QLabel("검색 범위"))
        self.explore_search_title = QCheckBox("제목")
        self.explore_search_title.setChecked(True)
        self.explore_search_content = QCheckBox("내용")
        self.explore_search_content.setChecked(True)
        search_scope.addWidget(self.explore_search_title)
        search_scope.addWidget(self.explore_search_content)
        search_scope.addStretch()
        search_scope.addWidget(QLabel("검색 결과는 압축파일 첨부 게시물만 표시합니다."))
        explore_layout.addLayout(search_scope)

        self.explore_browser = PostBrowser()
        self.explore_browser.download_requested.connect(self.download_attachment)
        self.explore_browser.load_more_requested.connect(self.load_more_explore_posts)
        explore_layout.addWidget(self.explore_browser, 1)
        self.tabs.addTab(explore_page, "전체 탐색")

        # Game-first creator discovery driven by bundled character aliases.
        candidate_page = QWidget()
        candidate_layout = QVBoxLayout(candidate_page)
        candidate_intro = QLabel(
            "게임을 선택하면 내장된 캐릭터 이름과 한국어·영어·일본어·중국어 별칭으로 Pawchive를 검색합니다. "
            "게시물 제목·내용, 태그, 첨부파일 이름을 확인하고 압축파일 내부의 .ini 파일을 검사해 "
            "해당 게임 모드 제작자 후보를 모읍니다. 게시물에 게임 이름이 없어도 캐릭터 이름으로 찾습니다."
        )
        candidate_intro.setWordWrap(True)
        candidate_intro.setObjectName("Subtle")
        candidate_layout.addWidget(candidate_intro)

        scan_panel = QFrame()
        scan_panel.setObjectName("Panel")
        scan_panel_layout = QVBoxLayout(scan_panel)
        game_row = QHBoxLayout()
        game_row.addWidget(QLabel("대상 게임"))
        self.mod_scan_game_combo = QComboBox()
        self.mod_scan_game_combo.addItem("게임 선택", "")
        self._mod_scan_game_catalog = load_game_catalog()
        for game_key, game_data in self._mod_scan_game_catalog.items():
            self.mod_scan_game_combo.addItem(str(game_data.get("name") or game_key), game_key)
        self.mod_scan_game_combo.addItem("직접 입력 / 목록에 없는 게임", "__custom__")
        self.mod_scan_game_combo.currentIndexChanged.connect(self._mod_scan_game_changed)
        game_row.addWidget(self.mod_scan_game_combo, 1)
        scan_panel_layout.addLayout(game_row)
        scan_panel_layout.addWidget(QLabel("자동 생성된 검색어 (필요하면 수정 가능)"))
        self.mod_scan_queries = QPlainTextEdit()
        self.mod_scan_queries.setPlaceholderText(
            "게임을 선택하면 캐릭터 이름과 다국어 별칭이 자동으로 채워집니다.\n"
            "목록에 없는 캐릭터나 별칭은 직접 추가할 수 있습니다."
        )
        self.mod_scan_queries.setMaximumHeight(96)
        scan_panel_layout.addWidget(self.mod_scan_queries)

        scan_options = QHBoxLayout()
        scan_options.addWidget(QLabel("검색 범위"))
        self.mod_scan_title = QCheckBox("제목")
        self.mod_scan_title.setChecked(True)
        self.mod_scan_content = QCheckBox("내용")
        self.mod_scan_content.setChecked(True)
        scan_options.addWidget(self.mod_scan_title)
        scan_options.addWidget(self.mod_scan_content)
        scan_options.addSpacing(18)
        scan_options.addWidget(QLabel("검색어당 페이지"))
        self.mod_scan_pages = QSpinBox()
        self.mod_scan_pages.setRange(1, 20)
        self.mod_scan_pages.setValue(1)
        self.mod_scan_pages.setSuffix("페이지/검색어")
        scan_options.addWidget(self.mod_scan_pages)
        scan_options.addStretch()
        scan_panel_layout.addLayout(scan_options)

        scan_actions = QHBoxLayout()
        self.mod_scan_button = QPushButton("선택한 게임 제작자 자동 탐색")
        self.mod_scan_button.setObjectName("Primary")
        self.mod_scan_button.clicked.connect(self.scan_mod_creator_candidates)
        scan_actions.addWidget(self.mod_scan_button)
        self.mod_scan_status = QLabel("게임별 캐릭터 별칭으로 검색합니다. 검색어당 기본 1페이지이며 필요하면 늘릴 수 있습니다.")
        self.mod_scan_status.setWordWrap(True)
        self.mod_scan_status.setObjectName("Subtle")
        scan_actions.addWidget(self.mod_scan_status, 1)
        scan_panel_layout.addLayout(scan_actions)
        candidate_layout.addWidget(scan_panel)

        candidate_top = QHBoxLayout()
        candidate_top.addWidget(QLabel("선택한 게임 검색어 및 INI 파일 근거가 있는 제작자 후보"))
        candidate_top.addStretch()
        candidate_refresh = QPushButton("목록 새로고침")
        candidate_refresh.clicked.connect(self._refresh_mod_candidates)
        candidate_top.addWidget(candidate_refresh)
        candidate_layout.addLayout(candidate_top)
        self.mod_candidate_list = QListWidget()
        self.mod_candidate_list.currentRowChanged.connect(self._mod_candidate_selected)
        candidate_layout.addWidget(self.mod_candidate_list, 1)
        self.mod_candidate_detail = QLabel("아직 자동 탐색 결과가 없습니다.")
        self.mod_candidate_detail.setWordWrap(True)
        self.mod_candidate_detail.setObjectName("Subtle")
        candidate_layout.addWidget(self.mod_candidate_detail)
        candidate_actions = QHBoxLayout()
        self.open_candidate_post_button = QPushButton("근거 게시물 열기")
        self.open_candidate_post_button.clicked.connect(self._open_mod_candidate_post)
        self.open_candidate_creator_button = QPushButton("크리에이터 프로필 열기")
        self.open_candidate_creator_button.clicked.connect(self._open_mod_candidate_profile)
        self.use_candidate_id_button = QPushButton("크리에이터 찾기에 ID 입력")
        self.use_candidate_id_button.clicked.connect(self._load_candidate_into_discover)
        for button in (
            self.open_candidate_post_button,
            self.open_candidate_creator_button,
            self.use_candidate_id_button,
        ):
            button.setEnabled(False)
            candidate_actions.addWidget(button)
        candidate_layout.addLayout(candidate_actions)
        self.tabs.addTab(candidate_page, "제작자 후보")

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
        self.settings_page.check_updates_requested.connect(self.check_for_updates)
        self.settings_page.apply_update_requested.connect(self.prepare_and_apply_update)
        self.tabs.addTab(self.settings_page, "설정")
        self._refresh_download_history()

    def _reset_timer(self) -> None:
        minutes = max(15, int(self.settings.get("sync_minutes", 60)))
        self.sync_timer.start(minutes * 60 * 1000)
        self.sync_label.setText(f"자동 동기화 {minutes}분")

    def _run_task(self, label: str, task: Callable[[], object],
                  on_success: Callable[[object], None],
                  on_failure: Callable[[str], None] | None = None) -> None:
        if any(worker.isRunning() for worker in self.workers):
            QMessageBox.information(self, "작업 진행 중", "현재 작업이 끝난 뒤 다시 시도해 주세요.")
            return
        self.status.setText(label)
        worker = TaskThread(task)
        self.workers.add(worker)
        worker.succeeded.connect(on_success)
        worker.failed.connect(on_failure or self._task_failed)
        worker.finished.connect(lambda w=worker: self.workers.discard(w))
        worker.start()

    def _run_progress_task(
        self,
        label: str,
        task: Callable[[Callable[[str], None]], object],
        on_success: Callable[[object], None],
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        if any(worker.isRunning() for worker in self.workers):
            QMessageBox.information(self, "작업 진행 중", "현재 작업이 끝난 뒤 다시 시도해 주세요.")
            return
        self.status.setText(label)
        worker_ref: dict[str, TaskThread] = {}

        def execute():
            return task(lambda message: worker_ref["worker"].progress.emit(message))

        worker = TaskThread(execute)
        worker_ref["worker"] = worker
        self.workers.add(worker)
        worker.progress.connect(self.status.setText)
        worker.succeeded.connect(on_success)
        worker.failed.connect(on_failure or self._task_failed)
        worker.finished.connect(lambda current=worker: self.workers.discard(current))
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
                        posts = client.complete_post_metadata(posts)
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
        cached_posts = [self._post_from_row(record) for record in rows]
        self.creator_browser.set_posts(cached_posts)
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
                posts = client.complete_post_metadata(posts)
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
                posts = client.complete_post_metadata(posts)
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
        include_title = self.explore_search_title.isChecked()
        include_content = self.explore_search_content.isChecked()
        if query and len(query) < 3:
            QMessageBox.information(self, "검색어 길이", "검색어는 3글자 이상 입력하세요.")
            return
        if query and not (include_title or include_content):
            QMessageBox.information(self, "검색 범위", "제목 또는 내용을 하나 이상 선택하세요.")
            return
        if any(worker.isRunning() for worker in self.workers):
            QMessageBox.information(self, "작업 진행 중", "현재 작업이 끝난 뒤 다시 검색해 주세요.")
            return

        self._explore_query_active = query
        self._explore_search_title_active = include_title
        self._explore_search_content_active = include_content
        self._explore_next_offset = 0
        self._explore_has_more = False
        self._explore_loading = True
        self.explore_browser.set_posts([])
        self.explore_browser.list_empty.setText("첫 페이지를 불러오는 중입니다.")
        self.explore_browser.set_pagination(False, loading=True)

        def task():
            with self._client() as client:
                raw_posts = client.recent_posts(query or None, offset=0)
                raw_posts = client.complete_post_metadata(raw_posts)
            self.database.save_posts(raw_posts)
            visible_posts = [
                post for post in raw_posts
                if post_matches_search_fields(
                    post, query,
                    include_title=include_title,
                    include_content=include_content,
                )
            ]
            return raw_posts, visible_posts

        def failed(message: str) -> None:
            self._explore_loading = False
            self.explore_browser.set_pagination(False)
            self.explore_browser.list_empty.setText("게시물을 불러오지 못했습니다.")
            self.status.setText("전체 탐색 실패")
            QMessageBox.warning(self, "전체 탐색 실패", message)

        def done(result) -> None:
            raw_posts, visible_posts = result
            self._explore_loading = False
            self.explore_browser.set_posts(visible_posts)
            self._explore_next_offset = 50
            self._explore_has_more = len(raw_posts) >= 50
            self.explore_browser.set_pagination(self._explore_has_more)
            if query and not self.explore_browser.posts and self._explore_has_more:
                self.explore_browser.list_empty.setText(
                    "이 페이지에는 선택한 검색 범위에 맞는 압축파일 게시물이 없습니다. "
                    "다음 페이지를 불러올 수 있습니다."
                )
            self.status.setText(
                f"전체 탐색 1페이지 · 압축파일 게시물 "
                f"{len(self.explore_browser.posts):,}개"
            )

        self._run_task("Pawchive 전체 게시물 첫 페이지 검색 중…", task, done, failed)

    def load_more_explore_posts(self) -> None:
        if self._explore_loading or not self._explore_has_more:
            return
        if any(worker.isRunning() for worker in self.workers):
            QMessageBox.information(self, "작업 진행 중", "현재 작업이 끝난 뒤 다시 시도해 주세요.")
            return

        query = self._explore_query_active
        include_title = self._explore_search_title_active
        include_content = self._explore_search_content_active
        offset = self._explore_next_offset
        page_number = offset // 50 + 1
        self._explore_loading = True
        self.explore_browser.set_pagination(self._explore_has_more, loading=True)

        def task():
            with self._client() as client:
                raw_posts = client.recent_posts(query or None, offset=offset)
                raw_posts = client.complete_post_metadata(raw_posts)
            self.database.save_posts(raw_posts)
            visible_posts = [
                post for post in raw_posts
                if post_matches_search_fields(
                    post, query,
                    include_title=include_title,
                    include_content=include_content,
                )
            ]
            return raw_posts, visible_posts

        def failed(message: str) -> None:
            self._explore_loading = False
            self.explore_browser.set_pagination(self._explore_has_more)
            self.status.setText("전체 탐색 페이지 불러오기 실패")
            QMessageBox.warning(self, "전체 탐색 실패", message)

        def done(result) -> None:
            raw_posts, visible_posts = result
            self._explore_loading = False
            self.explore_browser.append_posts(visible_posts)
            self._explore_next_offset = offset + 50
            self._explore_has_more = len(raw_posts) >= 50
            self.explore_browser.set_pagination(self._explore_has_more)
            if not self.explore_browser.posts and query:
                self.explore_browser.list_empty.setText(
                    "아직 선택한 검색 범위에 맞는 압축파일 게시물이 없습니다. "
                    "다음 페이지에서 계속 확인할 수 있습니다."
                )
            self.status.setText(
                f"전체 탐색 {page_number}페이지 · 누적 압축파일 게시물 "
                f"{len(self.explore_browser.posts):,}개"
            )

        self._run_task(f"Pawchive 전체 탐색 {page_number}페이지 불러오는 중…", task, done, failed)

    def _mod_scan_game_changed(self, _index: int) -> None:
        game_key = self.mod_scan_game_combo.currentData()
        if not game_key or game_key == "__custom__":
            self.mod_scan_queries.clear()
            if game_key == "__custom__":
                self.mod_scan_status.setText(
                    "목록에 없는 게임입니다. 게임명과 해당 게임 캐릭터 이름·별칭을 검색어에 입력하세요."
                )
            return
        game = self._mod_scan_game_catalog.get(game_key, {})
        terms = game_search_terms(game)
        self.mod_scan_queries.setPlainText("\n".join(terms))
        self.mod_scan_status.setText(
            f"{game.get('name', game_key)} · 캐릭터/별칭 검색어 {len(terms)}개 준비 완료"
        )
    def scan_mod_creator_candidates(self) -> None:
        game_key = self.mod_scan_game_combo.currentData()
        if not game_key:
            QMessageBox.information(self, "게임 선택", "먼저 대상 게임을 선택해 주세요.")
            return
        queries = parse_search_queries(self.mod_scan_queries.toPlainText())
        if not queries:
            QMessageBox.information(self, "검색어 입력", "게임의 캐릭터 이름이나 별칭을 추가해 주세요.")
            return
        if any(len(query) < 2 for query in queries):
            QMessageBox.information(self, "검색어 길이", "검색어는 2글자 이상 입력해 주세요.")
            return
        if len(queries) > 20:
            QMessageBox.information(self, "검색어 개수", "한 번에 최대 20개까지 입력할 수 있습니다.")
            return
        include_title = self.mod_scan_title.isChecked()
        include_content = self.mod_scan_content.isChecked()
        if not (include_title or include_content):
            QMessageBox.information(self, "검색 범위", "제목 또는 내용을 하나 이상 선택하세요.")
            return
        if any(worker.isRunning() for worker in self.workers):
            QMessageBox.information(self, "작업 진행 중", "현재 작업이 끝난 뒤 자동 탐색을 시작해 주세요.")
            return

        page_limit = self.mod_scan_pages.value()
        self.mod_scan_button.setEnabled(False)
        self.mod_scan_status.setText("선택한 게임의 캐릭터·별칭으로 Pawchive를 검색하고 있습니다…")

        def task(report):
            with self._client() as client:
                return scan_mod_creator_candidates(
                    client,
                    self.database,
                    queries,
                    page_limit=page_limit,
                    include_title=include_title,
                    include_content=include_content,
                    session_cookie=self.settings.get("session_key") or None,
                    temporary_root=data_dir(),
                    progress=report,
                    game_key=str(game_key),
                    game_name=self.mod_scan_game_combo.currentText(),
                )

        def done(result) -> None:
            self.mod_scan_button.setEnabled(True)
            self._refresh_mod_candidates()
            summary = (
                f"완료 · 검색어 {len(result.queries)}개 · 페이지 {result.pages_fetched}개 · "
                f"압축파일 게시물 {result.archive_posts_found}개 · "
                f"압축파일 {result.archives_scanned}개 검사 · "
                f"INI 발견 {result.ini_archives_found}개 · "
                f"후보 제작자 {result.creators_found}명"
            )
            if result.errors:
                summary += f"\n일부 파일 검사 실패 {len(result.errors)}건: " + " | ".join(result.errors[:3])
                if len(result.errors) > 3:
                    summary += " | …"
            self.mod_scan_status.setText(summary)
            self.status.setText(
                f"제작자 후보 자동 탐색 완료 · 후보 {result.creators_found}명"
            )

        def failed(message: str) -> None:
            self.mod_scan_button.setEnabled(True)
            self.mod_scan_status.setText("자동 탐색 실패: " + message)
            self.status.setText("제작자 후보 자동 탐색 실패")
            QMessageBox.warning(self, "제작자 후보 자동 탐색 실패", message)

        self._run_progress_task(
            "게임별 모드 제작자 후보 자동 탐색 중…",
            task,
            done,
            failed,
        )

    def _refresh_mod_candidates(self) -> None:
        if not hasattr(self, "mod_candidate_list"):
            return
        selected = self.mod_candidate_list.currentRow()
        selected_game = self.mod_scan_game_combo.currentData() if hasattr(self, "mod_scan_game_combo") else None
        self._mod_candidate_rows = self.database.mod_creator_candidates(
            game_key=selected_game if selected_game and selected_game != "__custom__" else None
        )
        self.mod_candidate_list.blockSignals(True)
        self.mod_candidate_list.clear()
        for row in self._mod_candidate_rows:
            label = (
                f"{row['service']}:{row['creator_id']}  ·  "
                f"INI 확인 압축파일 {row['evidence_count']}개"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, row)
            self.mod_candidate_list.addItem(item)
        self.mod_candidate_list.blockSignals(False)
        if self._mod_candidate_rows:
            self.mod_candidate_list.setCurrentRow(min(max(0, selected), len(self._mod_candidate_rows) - 1))
            self._mod_candidate_selected(self.mod_candidate_list.currentRow())
        else:
            self._selected_mod_candidate = None
            self.mod_candidate_detail.setText("아직 자동 탐색으로 확인된 제작자 후보가 없습니다.")
            for button in (
                self.open_candidate_post_button,
                self.open_candidate_creator_button,
                self.use_candidate_id_button,
            ):
                button.setEnabled(False)

    def _mod_candidate_selected(self, index: int) -> None:
        if index < 0 or index >= len(self._mod_candidate_rows):
            self._selected_mod_candidate = None
            return
        row = self._mod_candidate_rows[index]
        self._selected_mod_candidate = row
        evidence = self.database.mod_creator_evidence_for_creator(
            row["service"], row["creator_id"]
        )
        latest = evidence[0] if evidence else {}
        try:
            ini_paths = json.loads(latest.get("ini_files_json", "[]"))
        except (TypeError, ValueError):
            ini_paths = []
        listed = "\n".join(f"• {path}" for path in ini_paths)
        self.mod_candidate_detail.setText(
            f"작성자: {row['service']}:{row['creator_id']}\n"
            f"INI 확인 압축파일: {row['evidence_count']}개\n"
            f"최근 게시물: {latest.get('post_title') or '(제목 없음)'}\n"
            f"압축파일: {latest.get('archive_name', '')}\n"
            f"INI 파일 수: {latest.get('ini_count', 0)}개\n{listed}\n\n"
            "이 목록은 INI 파일이 발견된 작성자 후보입니다. 최종 판단은 게시물과 파일 내용을 확인해 주세요."
        )
        for button in (
            self.open_candidate_post_button,
            self.open_candidate_creator_button,
            self.use_candidate_id_button,
        ):
            button.setEnabled(True)

    def _open_mod_candidate_post(self) -> None:
        row = self._selected_mod_candidate
        if not row or not row.get("latest_post_id"):
            return
        url = (
            f"https://pawchive.pw/{quote(row['service'], safe='')}/user/"
            f"{quote(row['creator_id'], safe='')}/post/{quote(row['latest_post_id'], safe='')}"
        )
        QDesktopServices.openUrl(QUrl(url))

    def _open_mod_candidate_profile(self) -> None:
        row = self._selected_mod_candidate
        if not row:
            return
        url = (
            f"https://pawchive.pw/{quote(row['service'], safe='')}/user/"
            f"{quote(row['creator_id'], safe='')}"
        )
        QDesktopServices.openUrl(QUrl(url))

    def _load_candidate_into_discover(self) -> None:
        row = self._selected_mod_candidate
        if not row:
            return
        self.service_input.setText(row["service"])
        self.id_input.setText(row["creator_id"])
        self.tabs.setCurrentWidget(self.discover_page)

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

    def _has_active_app_tasks(self) -> bool:
        active = list(self.workers) + list(self.download_workers) + list(self._creator_tag_workers)
        for browser in (
            getattr(self, "following_browser", None),
            getattr(self, "creator_browser", None),
            getattr(self, "explore_browser", None),
        ):
            if browser is not None:
                active.extend(getattr(browser, "_thumbnail_workers", set()))
                content = getattr(browser, "content", None)
                if content is not None:
                    active.extend(getattr(content, "_pending", {}).values())
        return any(worker.isRunning() for worker in active)

    def check_for_updates(self) -> None:
        if self._has_active_app_tasks():
            QMessageBox.information(
                self, "작업 진행 중",
                "다운로드나 게시물 불러오기가 끝난 뒤 업데이트를 확인해 주세요."
            )
            return
        page = self.settings_page
        page.check_updates_button.setEnabled(False)
        page.apply_update_button.hide()
        page.apply_update_button.setEnabled(False)
        page.update_status.setText("GitHub에서 최신 버전 확인 중…")
        self._available_update = None
        self._prepared_update = None

        def done(result: UpdateCheck) -> None:
            page.check_updates_button.setEnabled(True)
            self._available_update = result
            if result.available:
                page.update_status.setText(
                    "새 버전을 찾았습니다. 업데이트 파일을 내려받아 적용할 수 있습니다.\n"
                    f"현재: {result.installed_commit[:10]} · 최신: {result.latest_commit[:10]}"
                )
                page.apply_update_button.setText("업데이트 파일 받기")
                page.apply_update_button.setEnabled(True)
                page.apply_update_button.show()
                self.status.setText("새 업데이트 확인")
            else:
                page.update_status.setText(
                    f"최신 버전입니다. ({result.latest_commit[:10]})"
                )
                self.status.setText("최신 버전 사용 중")

        def failed(message: str) -> None:
            page.check_updates_button.setEnabled(True)
            page.update_status.setText("업데이트 확인 실패: " + message)
            self.status.setText("업데이트 확인 실패")

        self._run_task(
            "GitHub 업데이트 확인 중…",
            self.update_manager.check,
            done,
            failed,
        )

    def prepare_and_apply_update(self) -> None:
        if self._prepared_update is not None:
            self._apply_prepared_update()
            return
        if self._available_update is None or not self._available_update.available:
            self.check_for_updates()
            return
        if self._has_active_app_tasks():
            QMessageBox.information(
                self, "작업 진행 중",
                "다운로드나 게시물 불러오기가 끝난 뒤 업데이트를 적용해 주세요."
            )
            return

        page = self.settings_page
        page.apply_update_button.setEnabled(False)
        page.check_updates_button.setEnabled(False)
        page.update_status.setText("업데이트 파일 다운로드 및 안전성 확인 중…")
        commit = self._available_update.latest_commit

        def done(package: UpdatePackage) -> None:
            self._prepared_update = package
            page.check_updates_button.setEnabled(True)
            page.update_status.setText(
                "업데이트 파일을 준비했습니다. 적용하면 앱이 종료된 뒤 파일을 교체하고 다시 실행됩니다.\n"
                f"대상 버전: {package.commit[:10]}"
            )
            page.apply_update_button.setText("업데이트 적용 후 재시작")
            page.apply_update_button.setEnabled(True)
            page.apply_update_button.show()
            self.status.setText("업데이트 준비 완료")

        def failed(message: str) -> None:
            page.check_updates_button.setEnabled(True)
            page.apply_update_button.setText("업데이트 파일 받기")
            page.apply_update_button.setEnabled(True)
            page.update_status.setText("업데이트 다운로드 실패: " + message)
            self.status.setText("업데이트 다운로드 실패")

        self._run_task(
            "GitHub 업데이트 다운로드 중…",
            lambda: self.update_manager.prepare(commit),
            done,
            failed,
        )

    def _apply_prepared_update(self) -> None:
        package = self._prepared_update
        if package is None:
            return
        if self._has_active_app_tasks():
            QMessageBox.information(
                self, "작업 진행 중",
                "진행 중인 다운로드나 게시물 작업이 끝난 뒤 다시 적용해 주세요."
            )
            return

        answer = QMessageBox.question(
            self,
            "업데이트 적용",
            "앱을 종료하고 업데이트를 적용한 다음 자동으로 다시 실행합니다.\n"
            "설정과 다운로드 기록은 유지됩니다. 지금 재시작할까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            app_root = Path(__file__).resolve().parent.parent
            self.update_manager.launch_after_exit(package, app_root, os.getpid())
        except UpdateError as exc:
            QMessageBox.warning(self, "업데이트 시작 실패", str(exc))
            return

        self.settings_page.apply_update_button.setEnabled(False)
        self.settings_page.check_updates_button.setEnabled(False)
        self.settings_page.update_status.setText(
            "앱을 닫으면 업데이트를 적용하고 다시 실행합니다."
        )
        QApplication.quit()

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
