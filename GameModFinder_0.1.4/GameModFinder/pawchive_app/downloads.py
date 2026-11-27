from __future__ import annotations

import bz2
import gzip
import lzma
import os
import re
import hashlib
import shutil
import subprocess
import threading
import time
import stat
import sys
import tarfile
import tempfile
import zipfile
from concurrent.futures import Future
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from .api import APP_USER_AGENT

FILES_HOST = "file.pawchive.pw"
_DOWNLOAD_LOCK = threading.Lock()
_LAST_DOWNLOAD_AT = 0.0
DOWNLOAD_INTERVAL_SECONDS = 1.0
# Share a five-request in-flight budget across file and thumbnail hosts.
_TRANSFER_SEMAPHORE = threading.BoundedSemaphore(5)
_THUMB_CACHE_LOCK = threading.Lock()
_THUMB_CACHE: OrderedDict[str, bytes] = OrderedDict()
_THUMB_CACHE_SIZE = 0
_THUMB_CACHE_LIMIT = 48 * 1024 * 1024
_THUMB_DISK_CACHE_LIMIT = 256 * 1024 * 1024
_THUMB_INFLIGHT: dict[str, Future[bytes]] = {}


def _wait_download_slot() -> None:
    """Respect Pawchive's published limit of at most one new file request per second."""
    global _LAST_DOWNLOAD_AT
    with _DOWNLOAD_LOCK:
        wait = DOWNLOAD_INTERVAL_SECONDS - (time.monotonic() - _LAST_DOWNLOAD_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_DOWNLOAD_AT = time.monotonic()



class DownloadError(RuntimeError):
    pass


def resolve_file_url(path_or_url: str, filename: str | None = None) -> str:
    """Resolve an API attachment path to the dedicated Pawchive file host.

    Pawchive API paths are relative to the file server's /data prefix. Some
    API responses include that prefix already; older/current responses may
    provide only the hash-directory path. Both forms are accepted.
    """
    value = (path_or_url or "").strip()
    if not value:
        raise DownloadError("파일 경로가 비어 있습니다.")

    parsed = urlsplit(value)
    if parsed.scheme:
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() != FILES_HOST
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise DownloadError("Pawchive 파일 서버 주소가 아닙니다.")
        path = parsed.path
        query = list(parse_qsl(parsed.query, keep_blank_values=True))
    else:
        if parsed.netloc:
            raise DownloadError("파일 경로 형식이 올바르지 않습니다.")
        path = parsed.path
        query = list(parse_qsl(parsed.query, keep_blank_values=True))

    decoded = unquote(path).replace("\\", "/")
    if "\x00" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        raise DownloadError("안전하지 않은 Pawchive 파일 경로입니다.")
    if not decoded.strip("/"):
        raise DownloadError("Pawchive 파일 경로가 비어 있습니다.")

    normalized = "/" + decoded.lstrip("/")
    if normalized == "/data" or normalized.startswith("/data/"):
        pass
    elif normalized.startswith("/thumbnail/"):
        raise DownloadError("썸네일 주소는 원본 파일 다운로드 주소로 사용할 수 없습니다.")
    elif normalized.startswith("/data"):
        raise DownloadError("Pawchive 파일 경로 형식이 올바르지 않습니다.")
    else:
        # Pawchive's API returns a path relative to /data. Reject obvious
        # non-file routes rather than treating arbitrary website URLs as files.
        parts = [part for part in normalized.split("/") if part]
        reserved = {"profile", "user", "creator", "creators", "post", "posts", "api", "thumbnail"}
        if len(parts) < 2 or parts[0].casefold() in reserved:
            raise DownloadError("허용되지 않은 Pawchive 파일 경로입니다.")
        normalized = "/data" + normalized

    if filename and not any(key == "f" for key, _ in query):
        query.append(("f", filename))
    encoded_path = quote(normalized, safe="/:@!$&'()*+,;=-._~")
    query_string = urlencode(query)
    return f"https://{FILES_HOST}{encoded_path}" + (f"?{query_string}" if query_string else "")



def resolve_thumbnail_url(path_or_url: str, filename: str | None = None) -> str:
    """Resolve an attachment path to Pawchive's image thumbnail host.

    The optional filename is sent as the documented `f` query parameter.
    """
    value = (path_or_url or "").strip()
    if not value:
        raise DownloadError("이미지 경로가 비어 있습니다.")

    parsed = urlsplit(value)
    query = list(parse_qsl(parsed.query, keep_blank_values=True))
    filename = filename or Path(unquote(parsed.path)).name or None
    if parsed.scheme:
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
            raise DownloadError("지원하지 않는 Pawchive 이미지 주소입니다.")
        path = unquote(parsed.path).replace(chr(92), "/")
        if host == "img.pawchive.pw":
            if not path.startswith("/thumbnail/data/"):
                raise DownloadError("지원하지 않는 Pawchive 썸네일 경로입니다.")
            thumb_path = path
        elif host == FILES_HOST or host in {"pawchive.pw", "www.pawchive.pw"}:
            if not path.startswith("/data/"):
                raise DownloadError("Pawchive 이미지 경로 형식이 올바르지 않습니다.")
            thumb_path = "/thumbnail" + path
        else:
            raise DownloadError("허용되지 않은 Pawchive 이미지 서버입니다.")
    else:
        if parsed.netloc:
            raise DownloadError("이미지 경로 형식이 올바르지 않습니다.")
        path = unquote(parsed.path).replace(chr(92), "/")
        if chr(0) in path or any(part in {".", ".."} for part in path.split("/")):
            raise DownloadError("안전하지 않은 Pawchive 이미지 경로입니다.")
        normalized = "/" + path.lstrip("/")
        if normalized.startswith("/thumbnail/data/"):
            thumb_path = normalized
        elif normalized.startswith("/data/"):
            thumb_path = "/thumbnail" + normalized
        else:
            if normalized.startswith("/f/") or normalized == "/f":
                raise DownloadError("이 파일 형식은 썸네일 미리보기를 지원하지 않습니다.")
            parts = [part for part in normalized.split("/") if part]
            if len(parts) < 2:
                raise DownloadError("Pawchive 이미지 경로 형식이 올바르지 않습니다.")
            thumb_path = "/thumbnail/data" + normalized

    if chr(0) in thumb_path or any(part in {".", ".."} for part in thumb_path.split("/")):
        raise DownloadError("안전하지 않은 Pawchive 이미지 경로입니다.")
    if filename and not any(key == "f" for key, _ in query):
        query.append(("f", filename))
    encoded_path = quote(thumb_path, safe="/:@!$&'()*+,;=-._~")
    query_string = urlencode(query)
    return "https://img.pawchive.pw" + encoded_path + (f"?{query_string}" if query_string else "")


def _curl_headers(header_path: Path) -> tuple[int | None, int | None]:
    """Read final HTTP status and full response size from curl -D output."""
    try:
        raw = header_path.read_text(encoding="iso-8859-1", errors="replace")
    except OSError:
        return None, None

    statuses = re.findall(r"^HTTP/\S+\s+(\d{3})", raw, flags=re.I | re.M)
    status = int(statuses[-1]) if statuses else None

    ranges = re.findall(r"^Content-Range:\s*bytes\s+\d+-\d+/(\d+)", raw, flags=re.I | re.M)
    if ranges:
        return status, int(ranges[-1])

    lengths = re.findall(r"^Content-Length:\s*(\d+)", raw, flags=re.I | re.M)
    return status, int(lengths[-1]) if lengths else None


def _find_curl() -> str | None:
    """Prefer Windows' inbox curl over third-party PATH copies."""
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        system_curl = system_root / "System32" / "curl.exe"
        if system_curl.is_file():
            return str(system_curl)
    return shutil.which("curl.exe") or shutil.which("curl")


def _browser_transfer(
    url: str,
    destination: str | Path,
    timeout: int,
    session_cookie: str | None = None,
) -> Path:
    """Last-resort Chromium transport, retaining the app's identifiable User-Agent.

    This is not browser impersonation: the request identifies this application
    and its project contact, while Chromium is used only as an alternate TLS
    and network stack. HTTP/2 and QUIC are disabled to keep transport parity
    with Pawchive's documented HTTP/1.1 download path.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "pawchive.pw" or host.endswith(".pawchive.pw")
    ):
        raise DownloadError("브라우저 재시도 주소가 Pawchive HTTPS 서버가 아닙니다.")

    try:
        from playwright.sync_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
            sync_playwright,
        )
    except ImportError as exc:
        raise DownloadError("Playwright가 없습니다. 앱 폴더에서 run.bat을 다시 실행해 주세요.") from exc

    if session_cookie and any(ch in session_cookie for ch in "\r\n;"):
        raise DownloadError("세션 쿠키 값이 올바르지 않습니다.")

    errors: list[str] = []
    target = Path(destination)
    with sync_playwright() as playwright:
        for channel in ("chrome", "msedge"):
            browser = None
            context = None
            try:
                browser = playwright.chromium.launch(
                    channel=channel,
                    headless=True,
                    args=[
                        "--disable-http2",
                        "--disable-quic",
                        "--disable-features=UseDnsHttpsSvcbAlpn",
                    ],
                )
                context = browser.new_context(
                    accept_downloads=True,
                    locale="ko-KR",
                    user_agent=APP_USER_AGENT,
                    extra_http_headers={"Accept": "*/*"},
                    ignore_https_errors=False,
                )
                if session_cookie:
                    context.add_cookies([{
                        "name": "session",
                        "value": session_cookie,
                        "domain": host,
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }])

                def allow_pawchive(route):
                    request_host = (urlsplit(route.request.url).hostname or "").lower()
                    if request_host == "pawchive.pw" or request_host.endswith(".pawchive.pw"):
                        route.continue_()
                    else:
                        route.abort()

                context.route("**/*", allow_pawchive)
                page = context.new_page()
                downloads = []
                page.on("download", lambda item: downloads.append(item))
                response = None
                navigation_error = ""
                try:
                    response = page.goto(
                        url,
                        wait_until="commit",
                        timeout=max(10, int(timeout)) * 1000,
                    )
                except PlaywrightError as exc:
                    # Attachment responses can abort navigation while raising a download event.
                    navigation_error = str(exc)

                if not downloads:
                    try:
                        page.wait_for_timeout(700)
                    except PlaywrightError:
                        pass

                if downloads:
                    downloads[0].save_as(str(target))
                    if target.exists() and target.stat().st_size > 0:
                        size = target.stat().st_size
                        if size == 376:
                            target.unlink(missing_ok=True)
                            raise DownloadError(
                                "Pawchive가 자동 요청을 차단했습니다(376바이트 반응)."
                            )
                        return target

                if response is not None:
                    if response.status == 404:
                        raise DownloadError("Pawchive 파일을 찾지 못했습니다(404).")
                    if response.status == 403:
                        raise DownloadError("Pawchive 파일 서버가 요청을 거부했습니다(403).")
                    if response.status == 429:
                        raise DownloadError("Pawchive 요청 제한(429)에 도달했습니다.")
                    if response.status >= 400:
                        raise DownloadError(f"Chrome/Edge 파일 서버 오류: HTTP {response.status}")

                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > 512 * 1024 * 1024:
                        raise DownloadError(
                            "브라우저가 다운로드 이벤트를 제공하지 않았고 파일이 512 MiB보다 큽니다."
                        )
                    body = response.body()
                    if not body:
                        raise DownloadError("Chrome/Edge에서 빈 파일 응답을 받았습니다.")
                    if len(body) == 376:
                        raise DownloadError(
                            "Pawchive가 자동 요청을 차단했습니다(376바이트 반응)."
                        )
                    if content_length and len(body) != int(content_length):
                        raise DownloadError(
                            f"브라우저 응답 크기가 맞지 않습니다: {len(body)}B / {content_length}B"
                        )
                    target.write_bytes(body)
                    return target

                raise DownloadError(
                    navigation_error or "Chrome/Edge에서 파일 응답을 받지 못했습니다."
                )
            except DownloadError:
                raise
            except PlaywrightTimeoutError as exc:
                errors.append(f"{channel}: 시간 초과: {exc}")
            except Exception as exc:
                errors.append(f"{channel}: {exc}")
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

    raise DownloadError(
        "Chrome/Edge 네트워크 전송도 실패했습니다. "
        + (" | ".join(errors[-2:]) if errors else "사용 가능한 브라우저가 없습니다.")
    )


def _curl_transfer(
    url: str,
    destination: str | Path,
    timeout: int,
    session_cookie: str | None = None,
    *,
    resume: bool = True,
    attempts: int = 5,
) -> Path:
    """Transfer using app-identifiable curl/HTTP1.1, then an app-UA Chromium fallback."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "pawchive.pw" or host.endswith(".pawchive.pw")
    ):
        raise DownloadError("다운로드 주소가 Pawchive HTTPS 서버가 아닙니다.")

    curl = _find_curl()
    if not curl:
        raise DownloadError("Windows curl.exe를 찾지 못했습니다.")

    if session_cookie and any(ch in session_cookie for ch in "\r\n;"):
        raise DownloadError("세션 쿠키 값이 올바르지 않습니다.")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    header_path = target.with_name(target.name + ".headers")
    last_error = ""
    try:
        for attempt in range(max(1, attempts)):
            offset = target.stat().st_size if resume and target.exists() else 0
            header_path.unlink(missing_ok=True)
            command = [
                curl,
                "-sS",
                "-f",
                "-L",
                "--http1.1",
                "--connect-timeout", "15",
                "--speed-limit", "1024",
                "--speed-time", "120",
                "-H", f"User-Agent: {APP_USER_AGENT}",
                "-H", "Accept: */*",
                "-D", str(header_path),
            ]
            # Broken IPv6 routes are common on Windows networks. Try IPv4 first,
            # then let curl choose normally for networks that are IPv6-only.
            if attempt < max(1, attempts // 2):
                command.append("-4")
            if session_cookie:
                command.extend(["-b", f"session={session_cookie}"])
            if offset:
                command.extend(["-C", str(offset)])
            command.extend(["-o", str(target), url])

            # Pawchive's one-request-per-second limit applies to the file
            # host. Thumbnail requests use the separate img host and are
            # already bounded by the UI's small worker pool.
            if host == FILES_HOST:
                _wait_download_slot()
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=None,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = str(exc)
                if attempt + 1 < max(1, attempts):
                    time.sleep(min(1.5, 0.5 * (attempt + 1)))
                    continue
                break

            status, expected_size = _curl_headers(header_path)
            actual_size = target.stat().st_size if target.exists() else 0
            detail = (result.stderr or result.stdout or "").strip()
            if result.returncode == 0 and status in (200, 206) and actual_size > 0:
                if actual_size == 376 or expected_size == 376:
                    target.unlink(missing_ok=True)
                    raise DownloadError(
                        "Pawchive가 자동 요청을 차단했습니다(376바이트 반응). "
                        "잠시 기다린 뒤 다시 시도해 주세요."
                    )
                if expected_size is not None and actual_size != expected_size:
                    last_error = (
                        f"파일 크기가 응답과 다릅니다: 받은 {actual_size}B / "
                        f"예상 {expected_size}B"
                    )
                else:
                    return target
            elif status == 404:
                target.unlink(missing_ok=True)
                raise DownloadError("Pawchive 파일을 찾지 못했습니다(404). 원본 파일이 없을 수 있습니다.")
            elif status == 429:
                raise DownloadError("Pawchive 요청 제한(429)에 도달했습니다. 잠시 후 다시 시도해 주세요.")
            elif status == 403:
                target.unlink(missing_ok=True)
                raise DownloadError(
                    "Pawchive 파일 서버가 요청을 거부했습니다(403). "
                    "파일 접근 권한 또는 서버의 자동 요청 제한을 확인해 주세요."
                )
            else:
                last_error = detail or f"curl 종료 코드 {result.returncode}, HTTP {status or 'unknown'}"
                if offset and status == 200:
                    target.unlink(missing_ok=True)

            if attempt + 1 < max(1, attempts):
                time.sleep(3)

        # Curl may be reset by the TLS/network path even when the public API is
        # reachable. Retry with Chrome/Edge's alternate TLS stack while keeping
        # the same application User-Agent and the one-request-per-second gate.
        try:
            if host == FILES_HOST:
                _wait_download_slot()
            _browser_transfer(url, target, timeout, session_cookie=session_cookie)
            return target
        except DownloadError as browser_error:
            raise DownloadError(
                "Pawchive 파일 서버가 앱 연결을 반복해서 끊었습니다. "
                f"curl: {last_error or '연결 실패'} / Chrome·Edge: {browser_error}"
            ) from browser_error
    finally:
        header_path.unlink(missing_ok=True)


def _thumbnail_cache_key(url: str, session_cookie: str | None) -> str:
    # Hash the optional session key into the cache key without ever persisting it.
    raw = url + chr(0) + (session_cookie or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _thumbnail_cache_path(cache_key: str) -> Path:
    root = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or (Path.home() / ".cache")
    )
    return root / "PawchiveModFinder" / "thumbnails" / cache_key[:2] / (cache_key + ".bin")


def _remember_thumbnail(cache_key: str, payload: bytes) -> None:
    global _THUMB_CACHE_SIZE
    with _THUMB_CACHE_LOCK:
        previous = _THUMB_CACHE.pop(cache_key, None)
        if previous is not None:
            _THUMB_CACHE_SIZE -= len(previous)
        _THUMB_CACHE[cache_key] = payload
        _THUMB_CACHE_SIZE += len(payload)
        while _THUMB_CACHE_SIZE > _THUMB_CACHE_LIMIT and _THUMB_CACHE:
            _, removed = _THUMB_CACHE.popitem(last=False)
            _THUMB_CACHE_SIZE -= len(removed)


def _trim_thumbnail_disk_cache(directory: Path) -> None:
    try:
        files = list(directory.glob("*/*.bin"))
        total = sum(item.stat().st_size for item in files if item.is_file())
        if total <= _THUMB_DISK_CACHE_LIMIT:
            return
        for item in sorted(files, key=lambda value: value.stat().st_mtime):
            try:
                size = item.stat().st_size
                item.unlink(missing_ok=True)
                total -= size
                if total <= _THUMB_DISK_CACHE_LIMIT:
                    break
            except OSError:
                continue
    except OSError:
        pass


def fetch_thumbnail_bytes(
    url: str, timeout: int = 30, session_cookie: str | None = None,
) -> bytes:
    """Fetch a Pawchive thumbnail using memory/disk caches and request de-duplication."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "img.pawchive.pw"
        or not parsed.path.startswith("/thumbnail/data/")
    ):
        raise DownloadError("허용되지 않은 Pawchive 썸네일 주소입니다.")

    cache_key = _thumbnail_cache_key(url, session_cookie)
    with _THUMB_CACHE_LOCK:
        cached = _THUMB_CACHE.get(cache_key)
        if cached is not None:
            _THUMB_CACHE.move_to_end(cache_key)
            return cached
        future = _THUMB_INFLIGHT.get(cache_key)
        is_owner = future is None
        if future is None:
            future = Future()
            _THUMB_INFLIGHT[cache_key] = future

    if not is_owner:
        # Another visible card or the rich-text viewer is already fetching it.
        return future.result(timeout=max(120, int(timeout) + 90))

    try:
        disk_path = _thumbnail_cache_path(cache_key)
        payload = b""
        try:
            if disk_path.is_file():
                size = disk_path.stat().st_size
                if 0 < size <= 16 * 1024 * 1024:
                    payload = disk_path.read_bytes()
                else:
                    disk_path.unlink(missing_ok=True)
        except OSError:
            payload = b""

        if not payload:
            with tempfile.TemporaryDirectory(prefix="pawchive-thumb-") as directory:
                temporary = Path(directory) / "thumbnail.bin"
                with _TRANSFER_SEMAPHORE:
                    _curl_transfer(
                        url, temporary, timeout, session_cookie=session_cookie,
                        resume=False, attempts=2,
                    )
                payload = temporary.read_bytes()

            if not payload or len(payload) > 16 * 1024 * 1024:
                raise DownloadError("이미지 응답이 비어 있거나 너무 큽니다.")

            # The cache is an optimization only; disk write failures must not
            # turn a successful network response into a failed preview.
            try:
                disk_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_cache = disk_path.with_name(disk_path.name + f".{os.getpid()}.tmp")
                temporary_cache.write_bytes(payload)
                os.replace(temporary_cache, disk_path)
                _trim_thumbnail_disk_cache(disk_path.parent.parent)
            except OSError:
                try:
                    temporary_cache.unlink(missing_ok=True)
                except (OSError, UnboundLocalError):
                    pass

        _remember_thumbnail(cache_key, payload)
        future.set_result(payload)
        return payload
    except Exception as exc:
        future.set_exception(exc)
        raise
    finally:
        with _THUMB_CACHE_LOCK:
            if _THUMB_INFLIGHT.get(cache_key) is future:
                _THUMB_INFLIGHT.pop(cache_key, None)


def download_file(
    path_or_url: str,
    destination: str | Path,
    timeout: int = 60,
    session_cookie: str | None = None,
    filename: str | None = None,
) -> Path:
    """Download one original attachment via Pawchive's identified HTTP/1.1 curl flow."""
    url = resolve_file_url(path_or_url, filename=filename)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"파일이 이미 존재합니다: {target}")
    temporary = target.with_name(target.name + ".part")
    try:
        with _TRANSFER_SEMAPHORE:
            _curl_transfer(
                url, temporary, timeout, session_cookie=session_cookie,
                resume=True, attempts=5,
            )
        os.replace(temporary, target)
        return target
    except Exception:
        temporary.unlink(missing_ok=True)
        raise



MEGA_CDN_DOMAINS = ("mega.nz", "mega.co.nz", "mega.io")


def _is_mega_owned_host(host: str) -> bool:
    """Check a hostname against MEGA's domains without accepting lookalikes."""
    normalized = (host or "").lower().rstrip(".")
    return bool(normalized) and any(
        normalized == domain or normalized.endswith("." + domain)
        for domain in MEGA_CDN_DOMAINS
    )


def _is_trusted_mega_download_url(url: str) -> bool:
    """Accept only HTTPS download URLs served from MEGA-owned domains.

    MEGA may return HTTP storage URLs for its already-encrypted transfer
    chunks. Those are upgraded to HTTPS by _normalize_mega_download_url()
    before this validator is called.
    """
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "https"
        or not _is_mega_owned_host(host)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return False
    return True


def _mega_ctr_initial_value(iv_words: tuple[int, int]) -> int:
    """Build MEGA's 128-bit CTR initial counter from its two-word IV."""
    if len(iv_words) != 2:
        raise DownloadError("MEGA CTR 초기화 벡터 길이가 올바르지 않습니다.")
    return int.from_bytes(
        # MEGA stores the two IV words in the high 64 bits; low 64 bits are zero.
        # a32_to_str is imported locally by the download function, so use a
        # straightforward big-endian word encoding here.
        b"".join(int(word).to_bytes(4, "big") for word in (*iv_words, 0, 0)),
        "big",
    )


def _single_mega_download_url(value: object) -> str:
    """Normalize MEGA's one-URL response, including the API's singleton-array form.

    MEGA can return the ``g`` field as either a URL string or an array of
    URLs. The existing streaming/decryption code supports a single endpoint;
    CloudRAID responses with multiple endpoints must not be mistaken for one
    URL or bypass the trusted-host check.
    """
    while isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise DownloadError(
                "MEGA가 여러 다운로드 서버 주소(CloudRAID)를 반환했습니다. "
                "현재 버전은 단일 서버 다운로드만 지원합니다."
            )
        value = value[0]

    if not isinstance(value, str) or not value.strip():
        raise DownloadError("MEGA에서 다운로드 서버 주소를 받지 못했습니다.")
    return value.strip()


def _normalize_mega_download_url(value: object) -> str:
    """Normalize MEGA's API URL and upgrade trusted HTTP storage hosts to HTTPS.

    MEGA's API can return URLs such as
    ``http://gfsNNN.userstorage.mega.co.nz/...``. The hostname is validated
    before changing the scheme, so an arbitrary HTTP URL can never be upgraded
    and accepted. Only default HTTP/HTTPS ports are allowed; the actual transfer
    always uses HTTPS with the HTTP client's normal certificate verification.
    """
    raw_url = _single_mega_download_url(value)
    try:
        parsed = urlsplit(raw_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise DownloadError("MEGA 다운로드 서버 주소 형식이 올바르지 않습니다.") from exc

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not _is_mega_owned_host(host)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 80, 443)
    ):
        identity = _mega_download_server_identity(raw_url)
        raise DownloadError(
            "MEGA가 허용되지 않은 다운로드 서버를 반환했습니다 "
            f"({identity}). MEGA HTTPS 도메인만 사용할 수 있습니다."
        )

    # Strip :80 when upgrading; preserve an explicit default HTTPS port.
    netloc = host if port in (None, 80) else f"{host}:443"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _mega_download_server_identity(url: str) -> str:
    """Return non-sensitive URL diagnostics without exposing path or query tokens."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower() or "(host 없음)"
        scheme = parsed.scheme.lower() or "(scheme 없음)"
        return f"scheme={scheme}, host={host}"
    except ValueError:
        return "URL 형식=잘못됨"


def download_mega_file(
    url: str,
    destination_dir: str | Path,
    timeout: int = 300,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Path:
    """Download and decrypt one public MEGA file link without opening a browser.

    The public link key is used locally. Only the file handle is sent to MEGA's
    API; the fragment key is never transmitted. Encrypted data is streamed,
    decrypted chunk-by-chunk, and checked against MEGA's file MAC before the
    completed file is made visible at its final path.
    """
    parsed = urlsplit((url or "").strip())
    host = (parsed.hostname or "").lower()
    allowed_hosts = {
        "mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz",
        "mega.io", "www.mega.io",
    }
    match = re.fullmatch(r"/(?:file|embed)/([A-Za-z0-9_-]{8,64})/?", parsed.path or "")
    file_key_text = unquote(parsed.fragment or "")
    legacy_match = (
        re.fullmatch(r"!([A-Za-z0-9_-]{8,64})!([A-Za-z0-9_-]{43,44})", file_key_text)
        if host in {"mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz"} and parsed.path in {"", "/"}
        else None
    )
    if (
        parsed.scheme.lower() != "https"
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DownloadError("올바른 MEGA 공개 파일 링크가 아닙니다.")
    if legacy_match:
        file_handle = legacy_match.group(1)
        file_key_text = legacy_match.group(2)
    elif match:
        file_handle = match.group(1)
    else:
        raise DownloadError("올바른 MEGA 공개 파일 링크가 아닙니다.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,44}", file_key_text):
        raise DownloadError("MEGA 링크의 복호화 키 길이가 올바르지 않습니다.")

    try:
        from mega import Mega
        from mega.crypto import (
            a32_to_str,
            base64_to_a32,
            base64_url_decode,
            decrypt_attr,
            get_chunks,
            str_to_a32,
        )
        from Crypto.Cipher import AES
        from Crypto.Util import Counter
    except ImportError as exc:
        raise DownloadError(
            "MEGA 다운로드 구성 요소가 없습니다. 앱 폴더에서 run.bat을 다시 실행해 "
            "필수 패키지를 설치해 주세요."
        ) from exc

    client = Mega()
    temporary: Path | None = None
    try:
        try:
            key_words = base64_to_a32(file_key_text)
        except Exception as exc:
            raise DownloadError("MEGA 링크의 복호화 키를 읽지 못했습니다.") from exc
        if len(key_words) != 8:
            raise DownloadError("MEGA 링크의 복호화 키 길이가 올바르지 않습니다.")

        # Public-link metadata call: the key fragment stays local.
        file_data = client._api_request({"a": "g", "g": 1, "p": file_handle})
        if not isinstance(file_data, dict) or not all(
            field in file_data for field in ("g", "s", "at")
        ):
            raise DownloadError("MEGA에서 공개 파일 정보를 받지 못했습니다.")

        file_url = _normalize_mega_download_url(file_data["g"])
        if not _is_trusted_mega_download_url(file_url):
            # Keep this diagnostic deliberately limited to scheme + hostname;
            # MEGA's temporary URL path/query must never be copied into logs or UI.
            identity = _mega_download_server_identity(file_url)
            raise DownloadError(
                "MEGA 다운로드 서버 보안 검증에 실패했습니다 "
                f"({identity}). 허용된 MEGA HTTPS 도메인이 아닙니다. "
                "이 주소는 보안상 차단했습니다."
            )

        try:
            file_size = int(file_data["s"])
        except (TypeError, ValueError) as exc:
            raise DownloadError("MEGA 파일 크기 정보가 올바르지 않습니다.") from exc
        if file_size <= 0:
            raise DownloadError("MEGA 파일이 비어 있거나 크기를 확인할 수 없습니다.")

        decrypt_key = (
            key_words[0] ^ key_words[4],
            key_words[1] ^ key_words[5],
            key_words[2] ^ key_words[6],
            key_words[3] ^ key_words[7],
        )
        iv_words = key_words[4:6]
        expected_meta_mac = key_words[6:8]
        try:
            attributes = decrypt_attr(base64_url_decode(file_data["at"]), decrypt_key)
            remote_name = str(attributes["n"])
        except Exception as exc:
            raise DownloadError("MEGA 파일 이름 정보를 복호화하지 못했습니다.") from exc

        # Use MEGA's encrypted metadata filename, never the generic link-card label.
        safe_name = remote_name.replace("\\", "/").rsplit("/", 1)[-1]
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", safe_name).strip(" .")
        if not safe_name or safe_name in {".", ".."}:
            safe_name = "mega-download.bin"

        folder = Path(destination_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / safe_name
        if target.exists():
            raise FileExistsError(f"파일이 이미 존재합니다: {target}")
        temporary = target.with_name(target.name + ".mega.part")
        if temporary.exists():
            raise FileExistsError(f"임시 파일이 이미 존재합니다: {temporary}")

        key_bytes = a32_to_str(decrypt_key)
        iv_bytes = a32_to_str((iv_words[0], iv_words[1], iv_words[0], iv_words[1]))
        # MEGA CTR uses the 64-bit IV in the HIGH 64 bits of the 128-bit
        # counter block, followed by 64 zero bits. Keep the same layout as
        # mega.py-v2: ((iv0 << 32) + iv1) << 64. The previous expression
        # placed the IV words in the wrong byte positions and produced files
        # that decrypted to garbage, which then failed the file-MAC check.
        counter_value = _mega_ctr_initial_value((iv_words[0], iv_words[1]))
        counter = Counter.new(128, initial_value=counter_value)
        stream_cipher = AES.new(key_bytes, AES.MODE_CTR, counter=counter)
        running_mac = b"\x00" * 16
        downloaded = 0

        with _TRANSFER_SEMAPHORE:
            with client._http.stream("GET", file_url, timeout=timeout) as response:
                response.raise_for_status()
                declared_length = response.headers.get("content-length")
                if declared_length:
                    try:
                        if int(declared_length) != file_size:
                            raise DownloadError(
                                f"MEGA 응답 크기가 메타데이터와 다릅니다: "
                                f"{declared_length}B / {file_size}B"
                            )
                    except ValueError as exc:
                        raise DownloadError("MEGA 응답의 Content-Length가 올바르지 않습니다.") from exc

                chunks = iter(response.iter_raw(chunk_size=1024 * 1024))
                pending = bytearray()

                def read_exact(amount: int) -> bytes:
                    while len(pending) < amount:
                        try:
                            pending.extend(next(chunks))
                        except StopIteration:
                            break
                    if len(pending) < amount:
                        raise DownloadError(
                            f"MEGA 다운로드가 중간에 끊겼습니다: {downloaded}B / {file_size}B"
                        )
                    result = bytes(pending[:amount])
                    del pending[:amount]
                    return result

                with temporary.open("xb") as output:
                    for _chunk_start, chunk_size in get_chunks(file_size):
                        encrypted_chunk = read_exact(chunk_size)
                        plain_chunk = stream_cipher.decrypt(encrypted_chunk)
                        output.write(plain_chunk)

                        # MEGA authenticates each decrypted transfer chunk with
                        # AES-CBC, then chains those chunk MACs into the file MAC.
                        chunk_mac_cipher = AES.new(
                            key_bytes, AES.MODE_CBC, iv=iv_bytes
                        )
                        last_block = b""
                        for offset in range(0, len(plain_chunk), 16):
                            block = plain_chunk[offset:offset + 16]
                            if len(block) < 16:
                                block = block.ljust(16, b"\x00")
                            last_block = chunk_mac_cipher.encrypt(block)
                        if not last_block:
                            raise DownloadError("MEGA 파일 청크가 비어 있습니다.")
                        running_mac = AES.new(
                            key_bytes, AES.MODE_CBC, iv=running_mac
                        ).encrypt(last_block)

                        downloaded += len(plain_chunk)
                        if progress_callback:
                            progress_callback(downloaded, file_size)

                    if pending or any(chunks):
                        raise DownloadError("MEGA 서버가 예상보다 큰 파일을 전송했습니다.")
                    output.flush()
                    os.fsync(output.fileno())

        actual_mac = str_to_a32(running_mac)
        calculated_meta_mac = (
            actual_mac[0] ^ actual_mac[1],
            actual_mac[2] ^ actual_mac[3],
        )
        if calculated_meta_mac != tuple(expected_meta_mac):
            raise DownloadError(
                "MEGA 파일 복호화 검증에 실패했습니다. 링크 키가 잘못되었거나 "
                "다운로드 데이터가 손상되었을 수 있습니다."
            )

        # Atomic hard-link creation fails if another file appeared at target.
        os.link(temporary, target)
        temporary.unlink()
        temporary = None
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        try:
            client._http.close()
        except Exception:
            pass


def _safe_target(root: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or any(part in ("..", "") for part in relative.parts):
        raise DownloadError(f"안전하지 않은 압축 경로: {member_name}")
    target = (root / Path(*relative.parts)).resolve()
    if target != root and root not in target.parents:
        raise DownloadError(f"압축 해제 경로가 대상 폴더를 벗어납니다: {member_name}")
    return target


def safe_extract_zip(archive_path: str | Path, destination: str | Path) -> list[Path]:
    archive, root = Path(archive_path), Path(destination).resolve()
    with zipfile.ZipFile(archive) as zipped:
        planned = []
        for member in zipped.infolist():
            target = _safe_target(root, member.filename)
            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise DownloadError(f"ZIP 심볼릭 링크는 추출하지 않습니다: {member.filename}")
            if target.exists():
                raise FileExistsError(f"압축 해제 대상이 이미 존재합니다: {target}")
            planned.append((member, target))
        root.mkdir(parents=True, exist_ok=True)
        extracted = []
        for member, target in planned:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                extracted.append(target)
        return extracted


def safe_extract_tar(archive_path: str | Path, destination: str | Path) -> list[Path]:
    archive, root = Path(archive_path), Path(destination).resolve()
    with tarfile.open(archive, mode="r:*") as tar:
        planned = []
        for member in tar.getmembers():
            target = _safe_target(root, member.name)
            if not (member.isfile() or member.isdir()):
                raise DownloadError(f"압축 링크 또는 특수 파일은 추출하지 않습니다: {member.name}")
            if target.exists():
                raise FileExistsError(f"압축 해제 대상이 이미 존재합니다: {target}")
            planned.append((member, target))
        root.mkdir(parents=True, exist_ok=True)
        extracted = []
        for member, target in planned:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise DownloadError(f"압축 파일을 읽지 못했습니다: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            extracted.append(target)
        return extracted


def safe_extract_7z(archive_path: str | Path, destination: str | Path) -> list[Path]:
    """Safely extract a 7Z archive after validating every member and rejecting links."""
    archive, root = Path(archive_path), Path(destination).resolve()
    try:
        import py7zr
    except ImportError as exc:
        raise DownloadError(
            "7Z 압축 해제를 위한 py7zr 패키지가 없습니다. run.bat을 다시 실행해 주세요."
        ) from exc

    root_existed = root.exists()
    if root_existed and any(root.iterdir()):
        raise FileExistsError(f"압축 해제 대상 폴더가 비어 있지 않습니다: {root}")

    try:
        with py7zr.SevenZipFile(archive, mode="r") as seven:
            planned: list[tuple[str, Path, bool]] = []
            for member in seven.list():
                name = str(member.filename)
                is_directory = bool(member.is_directory)
                is_file = bool(member.is_file)
                is_symlink = bool(member.is_symlink)
                if is_symlink or not (is_directory or is_file):
                    raise DownloadError(f"7Z 링크 또는 특수 파일은 추출하지 않습니다: {name}")
                target = _safe_target(root, name)
                if target.exists():
                    raise FileExistsError(f"압축 해제 대상이 이미 존재합니다: {target}")
                planned.append((name, target, is_file))

            root.mkdir(parents=True, exist_ok=True)
            seven.extractall(path=root)
            return [target for _name, target, is_file in planned if is_file]
    except DownloadError:
        if not root_existed:
            shutil.rmtree(root, ignore_errors=True)
        raise
    except Exception as exc:
        if not root_existed:
            shutil.rmtree(root, ignore_errors=True)
        raise DownloadError(f"7Z 압축 해제에 실패했습니다: {exc}") from exc


def safe_extract_rar(archive_path: str | Path, destination: str | Path) -> list[Path]:
    """Safely extract a RAR archive member-by-member, rejecting links and special files."""
    archive, root = Path(archive_path), Path(destination).resolve()
    try:
        import rarfile
    except ImportError as exc:
        raise DownloadError(
            "RAR 압축 해제를 위한 rarfile 패키지가 없습니다. run.bat을 다시 실행해 주세요."
        ) from exc

    root_existed = root.exists()
    if root_existed and any(root.iterdir()):
        raise FileExistsError(f"압축 해제 대상 폴더가 비어 있지 않습니다: {root}")

    try:
        with rarfile.RarFile(archive) as rar:
            planned: list[tuple[object, Path, bool]] = []
            for member in rar.infolist():
                name = str(member.filename)
                is_directory = bool(member.is_dir() if hasattr(member, "is_dir") else member.isdir())
                is_symlink_attr = getattr(member, "is_symlink", None)
                is_symlink = bool(is_symlink_attr() if callable(is_symlink_attr) else False)
                is_file_attr = getattr(member, "is_file", None)
                is_file = bool(is_file_attr() if callable(is_file_attr) else not is_directory)
                if is_symlink or not (is_directory or is_file):
                    raise DownloadError(f"RAR 링크 또는 특수 파일은 추출하지 않습니다: {name}")
                target = _safe_target(root, name)
                if target.exists():
                    raise FileExistsError(f"압축 해제 대상이 이미 존재합니다: {target}")
                planned.append((member, target, is_file))

            root.mkdir(parents=True, exist_ok=True)
            extracted: list[Path] = []
            for member, target, is_file in planned:
                if not is_file:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with rar.open(member.filename) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                extracted.append(target)
            return extracted
    except DownloadError:
        if not root_existed:
            shutil.rmtree(root, ignore_errors=True)
        raise
    except Exception as exc:
        if not root_existed:
            shutil.rmtree(root, ignore_errors=True)
        raise DownloadError(
            f"RAR 압축 해제에 실패했습니다. RAR5는 unrar 또는 bsdtar가 필요할 수 있습니다: {exc}"
        ) from exc


def _extract_compressed_stream(
    archive: Path,
    destination: str | Path,
    opener: Callable,
    suffix: str,
) -> list[Path]:
    """Decompress a single-file GZIP/BZIP2/XZ stream into its own folder."""
    root = Path(destination).resolve()
    name = archive.name
    output_name = (
        name[:-len(suffix)]
        if name.casefold().endswith(suffix)
        else name + ".decompressed"
    )
    output_name = output_name or "decompressed"
    target = _safe_target(root, output_name)
    root_existed = root.exists()
    if target.exists():
        raise FileExistsError(f"압축 해제 대상이 이미 존재합니다: {target}")

    try:
        root.mkdir(parents=True, exist_ok=True)
        with opener(archive, "rb") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
    except Exception as exc:
        target.unlink(missing_ok=True)
        if not root_existed:
            shutil.rmtree(root, ignore_errors=True)
        raise DownloadError(
            f"{archive.name} 단일 압축 파일을 해제하지 못했습니다: {exc}"
        ) from exc
    return [target]


def _archive_output_stem(archive: Path) -> str:
    """Strip compound TAR suffixes when naming the extracted folder."""
    lowered = archive.name.casefold()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"):
        if lowered.endswith(suffix):
            return archive.name[:-len(suffix)] or "archive"
    return archive.stem or "archive"


def safe_extract_archive(archive_path: str | Path, destination: str | Path) -> list[Path]:
    """Safely extract common archive formats, detecting them by content where possible."""
    archive = Path(archive_path)
    if zipfile.is_zipfile(archive):
        return safe_extract_zip(archive, destination)
    if tarfile.is_tarfile(archive):
        return safe_extract_tar(archive, destination)

    with archive.open("rb") as source:
        signature = source.read(8)
    suffix = archive.suffix.casefold()
    lowered_name = archive.name.casefold()

    # Detect 7Z and RAR from their signatures as well as extensions. This also
    # handles download endpoints that provide a generic or extensionless name.
    if signature.startswith(b"7z\\xbc\\xaf\\x27\\x1c") or suffix == ".7z":
        return safe_extract_7z(archive, destination)
    if signature.startswith(b"Rar!\\x1a\\x07") or suffix == ".rar":
        return safe_extract_rar(archive, destination)

    # TAR archives were handled above. These branches cover standalone compressed
    # files (for example a .gz containing one DLL rather than a .tar.gz archive).
    if signature.startswith(b"\\x1f\\x8b") or suffix == ".gz":
        return _extract_compressed_stream(archive, destination, gzip.open, ".gz")
    if signature.startswith(b"BZh") or suffix == ".bz2":
        return _extract_compressed_stream(archive, destination, bz2.open, ".bz2")
    if signature.startswith(b"\\xfd7zXZ\\x00") or suffix == ".xz":
        return _extract_compressed_stream(archive, destination, lzma.open, ".xz")

    known_archive_suffixes = (
        ".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz",
        ".tgz", ".tbz2", ".txz",
    )
    if any(lowered_name.endswith(item) for item in known_archive_suffixes):
        raise DownloadError(f"다운로드한 압축 파일 형식을 확인할 수 없습니다: {archive.name}")
    return []


def extract_archive_and_remove(archive_path: str | Path) -> tuple[Path | None, list[Path]]:
    """Extract a downloaded archive and remove its original only after success.

    Returns the extraction directory and extracted files. Non-archive inputs are
    left untouched. Archive detection uses file signatures, so an extensionless
    or generically named 7Z/RAR/GZIP/BZIP2/XZ download is still unpacked.
    """
    archive = Path(archive_path)
    destination = archive.with_name(_archive_output_stem(archive) + "_extracted")
    extracted = safe_extract_archive(archive, destination)
    if not extracted:
        return None, []

    try:
        archive.unlink()
    except OSError as exc:
        raise DownloadError(
            f"압축 해제는 완료했지만 원본 압축파일을 삭제하지 못했습니다: {archive.name}"
        ) from exc
    return destination, extracted
