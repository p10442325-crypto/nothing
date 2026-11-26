from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import QUrl, QThread, Signal
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QTextBrowser

from .downloads import fetch_thumbnail_bytes
from .models import Attachment, Post


IMAGE_EXTENSIONS = {
    ".apng", ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png",
    ".tif", ".tiff", ".webp",
}
IMAGE_HOSTS = {"pawchive.pw", "www.pawchive.pw", "img.pawchive.pw", "file.pawchive.pw"}
VOID_TAGS = {"br", "hr", "img"}
ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "caption", "code", "del", "div",
    "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
    "li", "ol", "p", "pre", "s", "span", "strong", "table", "tbody",
    "td", "th", "thead", "tr", "u", "ul",
}
BLOCKED_TAGS = {"iframe", "object", "embed", "script", "style", "svg", "math", "form"}


def _safe_segments(path: str) -> str:
    decoded = unquote(path).replace("\\", "/")
    if "\x00" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        return ""
    return decoded


def pawchive_image_url(path_or_url: str, filename: str = "") -> str:
    """Build Pawchive's thumbnail URL for an API attachment path."""
    value = (path_or_url or "").strip()
    if not value:
        return ""

    parsed = urlsplit(value)
    query = list(parse_qsl(parsed.query, keep_blank_values=True))
    filename = filename or Path(unquote(parsed.path)).name
    path = parsed.path
    if parsed.scheme:
        if parsed.scheme.lower() != "https":
            return ""
        host = (parsed.hostname or "").lower()
        if host not in IMAGE_HOSTS:
            return ""
        path = _safe_segments(path)
        if not path:
            return ""
        if host == "img.pawchive.pw" and path.startswith("/thumbnail/data/"):
            thumbnail_path = path
        elif path.startswith("/data/"):
            thumbnail_path = "/thumbnail" + path
        elif host in {"pawchive.pw", "www.pawchive.pw"}:
            return value
        else:
            return ""
    else:
        if parsed.netloc:
            return ""
        path = _safe_segments(path)
        if not path:
            return ""
        path = "/" + path.lstrip("/")
        if path.startswith("/thumbnail/data/"):
            thumbnail_path = path
        elif path.startswith("/data/"):
            thumbnail_path = "/thumbnail" + path
        else:
            if path.startswith("/f/") or path == "/f":
                return ""
            thumbnail_path = "/thumbnail/data/" + path.lstrip("/")

    if filename and not any(key == "f" for key, _ in query):
        query.append(("f", filename))
    return urlunsplit((
        "https",
        "img.pawchive.pw",
        quote(thumbnail_path, safe="/:@!$&'()*+,;=-._~"),
        urlencode(query),
        "",
    ))

def _safe_href(value: str) -> str:
    raw = (value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() in {"http", "https", "mailto"}:
        return raw if parsed.scheme.lower() == "mailto" or parsed.hostname else ""
    if not parsed.scheme and not parsed.netloc:
        return "https://pawchive.pw/" + raw.lstrip("/")
    return ""


class _HTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self.blocked_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.blocked_stack:
            if tag in BLOCKED_TAGS:
                self.blocked_stack.append(tag)
            return
        if tag in BLOCKED_TAGS:
            self.blocked_stack.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return

        safe_attrs: list[str] = []
        values = dict(attrs)
        if tag == "a":
            href = _safe_href(values.get("href") or "")
            if href:
                safe_attrs.append(f'href="{html.escape(href, quote=True)}"')
            title = values.get("title")
            if title:
                safe_attrs.append(f'title="{html.escape(title, quote=True)}"')
        elif tag == "img":
            source = pawchive_image_url(values.get("src") or "")
            if not source:
                return
            safe_attrs.append(f'src="{html.escape(source, quote=True)}"')
            for attr in ("alt", "title"):
                if values.get(attr):
                    safe_attrs.append(f'{attr}="{html.escape(values[attr] or "", quote=True)}"')
            safe_attrs.append('style="max-width:100%;height:auto;"')
        elif tag in {"td", "th"}:
            for attr in ("colspan", "rowspan"):
                value = values.get(attr)
                if value and value.isdigit() and 0 < int(value) <= 30:
                    safe_attrs.append(f'{attr}="{value}"')
        suffix = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        self.parts.append(f"<{tag}{suffix}>")
        if tag not in VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.blocked_stack:
            if tag == self.blocked_stack[-1]:
                self.blocked_stack.pop()
            return
        if tag in self.open_tags:
            while self.open_tags:
                opened = self.open_tags.pop()
                self.parts.append(f"</{opened}>")
                if opened == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self.blocked_stack:
            self.parts.append(html.escape(data))


def _markdown_inline(value: str) -> str:
    text = html.escape(value)

    def image(match: re.Match[str]) -> str:
        alt, source = match.group(1), match.group(2).strip()
        url = pawchive_image_url(source)
        if not url:
            return html.escape(alt or source)
        return f'<img src="{html.escape(url, quote=True)}" alt="{html.escape(alt, quote=True)}" style="max-width:100%;height:auto;">'

    def link(match: re.Match[str]) -> str:
        label, destination = match.group(1), html.unescape(match.group(2).strip())
        href = _safe_href(destination)
        if not href:
            return label
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'

    text = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', image, text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link, text)
    tick = chr(96)
    text = re.sub(tick + r'([^' + tick + r']+)' + tick, r'<code>\1</code>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(r'~~(.+?)~~', r'<del>\1</del>', text)
    return text


def _markdown_to_html(content: str) -> str:
    output: list[str] = []
    paragraph: list[str] = []
    list_tag = ""
    quote_lines: list[str] = []
    in_code = False
    code_marker = chr(96) * 3

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            output.append("<p>" + "<br>".join(_markdown_inline(line) for line in paragraph) + "</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_tag
        if list_tag:
            output.append(f"</{list_tag}>")
            list_tag = ""

    def flush_quote() -> None:
        nonlocal quote_lines
        if quote_lines:
            output.append("<blockquote>" + "<br>".join(_markdown_inline(line) for line in quote_lines) + "</blockquote>")
            quote_lines = []

    for line in (content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip().startswith(code_marker):
            flush_paragraph(); flush_list(); flush_quote()
            if in_code:
                output.append("</code></pre>")
            else:
                output.append("<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            output.append(html.escape(line) + "\n")
            continue
        if not line.strip():
            flush_paragraph(); flush_list(); flush_quote()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if heading:
            flush_paragraph(); flush_list(); flush_quote()
            level = len(heading.group(1))
            output.append(f"<h{level}>{_markdown_inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.+)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if bullet or numbered:
            flush_paragraph(); flush_quote()
            desired = "ul" if bullet else "ol"
            if list_tag != desired:
                flush_list()
                list_tag = desired
                output.append(f"<{list_tag}>")
            output.append(f"<li>{_markdown_inline((bullet or numbered).group(1))}</li>")
            continue
        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            flush_paragraph(); flush_list()
            quote_lines.append(quote.group(1))
            continue
        flush_list(); flush_quote()
        paragraph.append(line)
    flush_paragraph(); flush_list(); flush_quote()
    if in_code:
        output.append("</code></pre>")
    return "".join(output)


def render_post_html(content: str, attachments: tuple[Attachment, ...] | list[Attachment] = ()) -> str:
    raw = content or ""
    tag_pattern = "|".join(sorted(ALLOWED_TAGS | BLOCKED_TAGS))
    if re.search(r"<\s*/?\s*(?:" + tag_pattern + r")\b", raw, re.I):
        parser = _HTMLSanitizer()
        try:
            parser.feed(raw)
            parser.close()
            body = "".join(parser.parts)
        except Exception:
            body = f"<p>{html.escape(raw)}</p>"
    else:
        body = _markdown_to_html(raw)

    images: list[str] = []
    seen: set[str] = set()
    for attachment in attachments:
        suffix = Path(attachment.name or urlsplit(attachment.path).path).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            continue
        source = pawchive_image_url(attachment.path, attachment.name)
        if not source or source in seen:
            continue
        seen.add(source)
        images.append(
            '<figure style="margin:12px 0;">'
            f'<img src="{html.escape(source, quote=True)}" alt="{html.escape(attachment.name, quote=True)}" style="max-width:100%;height:auto;">'
            f'<figcaption>{html.escape(attachment.name)}</figcaption></figure>'
        )
    if images:
        body += '<hr><h3>첨부 이미지</h3>' + "".join(images)
    if not body.strip():
        body = "<p>(본문 없음)</p>"
    return (
        "<html><head><meta charset='utf-8'><style>"
        "body{font-family:'Segoe UI','Malgun Gothic',sans-serif;color:#e8edf5;background:#151d28;}"
        "p{line-height:1.55;margin:7px 0;}h1,h2,h3,h4{color:#f3f6fb;margin:14px 0 7px;}"
        "a{color:#83b7f2;}blockquote{border-left:3px solid #547da9;padding:4px 12px;color:#c2d1e1;}"
        "pre,code{background:#0e141d;border-radius:4px;padding:2px 4px;}pre{padding:10px;white-space:pre-wrap;}"
        "table{border-collapse:collapse;}td,th{border:1px solid #53657b;padding:5px;}"
        "figcaption{font-size:9pt;color:#aab8c9;margin-top:4px;}"
        "</style></head><body>" + body + "</body></html>"
    )


class _ThumbnailFetchThread(QThread):
    loaded = Signal(str, bytes)
    failed = Signal(str, str)

    def __init__(self, url: str, session_cookie: str | None = None) -> None:
        super().__init__()
        self.url = url
        self.session_cookie = session_cookie

    def run(self) -> None:
        try:
            self.loaded.emit(self.url, fetch_thumbnail_bytes(self.url, session_cookie=self.session_cookie))
        except Exception as exc:
            self.failed.emit(self.url, str(exc))


class PostTextBrowser(QTextBrowser):
    """Read-only rich post viewer with browser-backed Pawchive image loading."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setPlaceholderText("게시물 본문이 여기에 표시됩니다.")
        self._image_cache: dict[str, QImage] = {}
        self._pending: dict[str, _ThumbnailFetchThread] = {}
        self.session_cookie = ""
        self._image_failures: set[str] = set()

    def set_session_cookie(self, value: str | None) -> None:
        self.session_cookie = value or ""

    def setHtml(self, text: str) -> None:
        self._image_failures.clear()
        super().setHtml(text)

    def loadResource(self, resource_type, name):
        if resource_type == QTextDocument.ResourceType.ImageResource:
            url = name if isinstance(name, QUrl) else QUrl(str(name))
            host = (url.host() or "").lower()
            key = url.toString()
            if (
                url.scheme().lower() == "https"
                and host == "img.pawchive.pw"
                and url.path().startswith("/thumbnail/data/")
            ):
                cached = self._image_cache.get(key)
                if cached is not None:
                    return cached
                if key not in self._pending and key not in self._image_failures:
                    worker = _ThumbnailFetchThread(key, self.session_cookie or None)
                    self._pending[key] = worker
                    worker.loaded.connect(self._image_loaded)
                    worker.failed.connect(self._image_failed)
                    worker.finished.connect(
                        lambda image_url=key, item=worker: self._worker_finished(image_url, item)
                    )
                    worker.finished.connect(worker.deleteLater)
                    worker.start()
                return QImage()
        return super().loadResource(resource_type, name)

    def _worker_finished(self, key: str, worker: _ThumbnailFetchThread) -> None:
        if self._pending.get(key) is worker:
            self._pending.pop(key, None)

    def _image_loaded(self, key: str, payload: bytes) -> None:
        image = QImage.fromData(payload)
        if image.isNull():
            self._image_failures.add(key)
            return
        self._image_cache[key] = image
        url = QUrl(key)
        self.document().addResource(QTextDocument.ResourceType.ImageResource, url, image)
        self.document().markContentsDirty(0, self.document().characterCount())
        self.viewport().update()

    def _image_failed(self, key: str, message: str) -> None:
        self._image_failures.add(key)
        self.setToolTip(message)


def _enhanced_download_attachment(self, post: Post, attachment: Attachment) -> None:
    paths = {"공통 저장 폴더": self.settings.get("download_dir", "")}
    for name, folder in self.settings.get("game_paths", {}).items():
        paths[f"{name} 저장 폴더"] = folder
    options = [label for label, folder in paths.items() if folder]
    if not options:
        QMessageBox.warning(self, "저장 폴더 없음", "설정에서 공통 저장 폴더 또는 게임별 저장 폴더를 지정하세요.")
        self.tabs.setCurrentWidget(self.settings_page)
        return
    choice, accepted = QInputDialog.getItem(
        self, "저장 위치 선택", "저장할 기준 폴더를 선택하세요. 게임은 자동 분류하지 않습니다.",
        options, 0, False,
    )
    if not accepted:
        return

    base_folder = Path(paths[choice]).expanduser()
    try:
        base_folder.mkdir(parents=True, exist_ok=True)
        base_folder = base_folder.resolve()
        if not base_folder.is_dir():
            raise OSError("선택한 저장 경로가 폴더가 아닙니다.")
    except OSError as exc:
        QMessageBox.warning(self, "저장 폴더 오류", f"저장 폴더를 열 수 없습니다.\n{exc}")
        return

    action, accepted = QInputDialog.getItem(
        self, "세부 저장 위치", f"기준 폴더:\n{base_folder}",
        ["선택한 폴더에 저장", "하위 폴더 선택"], 0, False,
    )
    if not accepted:
        return
    folder = base_folder
    if action == "하위 폴더 선택":
        selected = QFileDialog.getExistingDirectory(
            self, "저장할 하위 폴더 선택", str(base_folder),
        )
        if not selected:
            return
        try:
            folder = Path(selected).expanduser().resolve()
            folder.relative_to(base_folder)
        except (OSError, ValueError):
            QMessageBox.warning(self, "폴더 범위 확인", "기준 저장 폴더 안에 있는 하위 폴더만 선택할 수 있습니다.")
            return

    raw_name = Path(attachment.name or attachment.path.rsplit("/", 1)[-1]).name
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw_name).strip(" .") or "download.bin"
    target = folder / filename

    def task():
        from .downloads import DownloadError, download_file, safe_extract_archive
        saved = download_file(
            attachment.path, target,
            session_cookie=self.settings.get("session_key") or None,
            filename=attachment.name or None,
        )
        extracted = []
        try:
            extracted = safe_extract_archive(saved, saved.parent / (saved.stem + "_extracted"))
        except (ValueError, DownloadError, OSError):
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


def install_ui_enhancements(ui_module) -> None:
    """Patch the existing app widgets without altering its core navigation."""
    if getattr(ui_module, "_pawchive_enhancements_installed", False):
        return
    base_browser = ui_module.PostBrowser

    class RichPostBrowser(base_browser):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            old = self.content
            layout = old.parentWidget().layout()
            index = layout.indexOf(old)
            layout.removeWidget(old)
            old.deleteLater()
            self.content = PostTextBrowser()
            layout.insertWidget(index, self.content, 1)

        def _show_selected(self, row: int) -> None:
            super()._show_selected(row)
            if 0 <= row < len(self.posts):
                post = self.posts[row]
                self.content.setHtml(render_post_html(post.content, post.files))

    ui_module.PostBrowser = RichPostBrowser
    ui_module.MainWindow.download_attachment = _enhanced_download_attachment
    ui_module._pawchive_enhancements_installed = True
