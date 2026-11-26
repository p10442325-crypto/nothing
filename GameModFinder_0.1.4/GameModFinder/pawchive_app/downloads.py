from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import stat
import tarfile
import tempfile
import zipfile
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from .api import APP_USER_AGENT

FILES_HOST = "file.pawchive.pw"
_DOWNLOAD_LOCK = threading.Lock()
_LAST_DOWNLOAD_AT = 0.0
DOWNLOAD_INTERVAL_SECONDS = 1.0
_THUMB_CACHE_LOCK = threading.Lock()
_THUMB_CACHE: OrderedDict[str, bytes] = OrderedDict()
_THUMB_CACHE_SIZE = 0
_THUMB_CACHE_LIMIT = 48 * 1024 * 1024


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


def _curl_transfer(
    url: str,
    destination: str | Path,
    timeout: int,
    session_cookie: str | None = None,
    *,
    resume: bool = True,
    attempts: int = 5,
) -> Path:
    """Download from Pawchive with its documented identifiable UA and HTTP/1.1."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "pawchive.pw" or host.endswith(".pawchive.pw")
    ):
        raise DownloadError("다운로드 주소가 Pawchive HTTPS 서버가 아닙니다.")

    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise DownloadError("Windows 기본 curl.exe를 찾지 못했습니다.")

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
                "--connect-timeout", "30",
                "--speed-limit", "1024",
                "--speed-time", "120",
                "-H", f"User-Agent: {APP_USER_AGENT}",
                "-H", "Accept: */*",
                "-D", str(header_path),
            ]
            if session_cookie:
                if any(ch in session_cookie for ch in "\r\n;"):
                    raise DownloadError("세션 쿠키 값이 올바르지 않습니다.")
                command.extend(["-b", f"session={session_cookie}"])
            if offset:
                command.extend(["-C", str(offset)])
            command.extend(["-o", str(target), url])

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
                    time.sleep(3)
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
                # If the range request was ignored, restart cleanly instead of appending a full body.
                if offset and status == 200:
                    target.unlink(missing_ok=True)

            if attempt + 1 < max(1, attempts):
                time.sleep(3)

        raise DownloadError(
            f"Pawchive 다운로드 연결에 실패했습니다({last_error or '응답 없음'}). "
            "브라우저 흉내 UA가 아닌 앱 식별 UA와 HTTP/1.1로 재시도했지만 연결이 끊겼습니다."
        )
    finally:
        header_path.unlink(missing_ok=True)


def fetch_thumbnail_bytes(
    url: str, timeout: int = 30, session_cookie: str | None = None,
) -> bytes:
    """Fetch a Pawchive thumbnail with the dedicated downloader transport."""
    global _THUMB_CACHE_SIZE
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "img.pawchive.pw"
        or not parsed.path.startswith("/thumbnail/data/")
    ):
        raise DownloadError("허용되지 않은 Pawchive 썸네일 주소입니다.")

    with _THUMB_CACHE_LOCK:
        cached = _THUMB_CACHE.get(url)
        if cached is not None:
            _THUMB_CACHE.move_to_end(url)
            return cached

        with tempfile.TemporaryDirectory(prefix="pawchive-thumb-") as directory:
            temporary = Path(directory) / "thumbnail.bin"
            _curl_transfer(
                url, temporary, timeout, session_cookie=session_cookie,
                resume=False, attempts=3,
            )
            payload = temporary.read_bytes()

        if not payload or len(payload) > 16 * 1024 * 1024:
            raise DownloadError("이미지 응답이 비어 있거나 너무 큽니다.")

        _THUMB_CACHE[url] = payload
        _THUMB_CACHE_SIZE += len(payload)
        while _THUMB_CACHE_SIZE > _THUMB_CACHE_LIMIT and _THUMB_CACHE:
            _, removed = _THUMB_CACHE.popitem(last=False)
            _THUMB_CACHE_SIZE -= len(removed)
        return payload


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
        _curl_transfer(
            url, temporary, timeout, session_cookie=session_cookie,
            resume=True, attempts=5,
        )
        os.replace(temporary, target)
        return target
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

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


def safe_extract_archive(archive_path: str | Path, destination: str | Path) -> list[Path]:
    """Safely extract ZIP or TAR-family archives; unsupported formats are left intact."""
    archive = Path(archive_path)
    if zipfile.is_zipfile(archive):
        return safe_extract_zip(archive, destination)
    if tarfile.is_tarfile(archive):
        return safe_extract_tar(archive, destination)
    return []
