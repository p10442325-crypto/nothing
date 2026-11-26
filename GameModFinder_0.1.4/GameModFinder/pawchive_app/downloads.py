from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .api import APP_USER_AGENT

FILES_HOST = "file.pawchive.pw"
_DOWNLOAD_LOCK = threading.Lock()
_LAST_DOWNLOAD_AT = 0.0
DOWNLOAD_INTERVAL_SECONDS = 1.0


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


def _browser_download(
    url: str,
    destination: str | Path,
    timeout: int,
    session_cookie: str | None = None,
) -> Path:
    """Use an installed Chrome/Edge browser when Python and curl transports reset."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "pawchive.pw" or host.endswith(".pawchive.pw")):
        raise DownloadError("브라우저 재시도 주소가 Pawchive 호스트가 아닙니다.")

    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as exc:
        raise DownloadError("Playwright가 없습니다. 앱 폴더에서 run.bat을 다시 실행해 주세요.") from exc

    errors: list[str] = []
    target = Path(destination)
    with sync_playwright() as playwright:
        for channel in ("chrome", "msedge"):
            browser = None
            context = None
            try:
                browser = playwright.chromium.launch(channel=channel, headless=True)
                context = browser.new_context(
                    accept_downloads=True,
                    locale="ko-KR",
                    ignore_https_errors=False,
                )
                if session_cookie:
                    if any(ch in session_cookie for ch in "\\r\\n;"):
                        raise DownloadError("세션 쿠키 값이 올바르지 않습니다.")
                    context.add_cookies([{
                        "name": "session",
                        "value": session_cookie,
                        "domain": host,
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }])

                # Do not allow a file-host redirect to send cookies or data elsewhere.
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
                        timeout=max(5, int(timeout)) * 1000,
                    )
                except PlaywrightError as exc:
                    navigation_error = str(exc)

                if downloads:
                    downloads[0].save_as(str(target))
                    if target.exists() and target.stat().st_size > 0:
                        return target

                if response is not None:
                    if response.status == 404:
                        raise DownloadError("Pawchive 파일을 찾지 못했습니다(404).")
                    if response.status == 429:
                        raise DownloadError("Pawchive 요청 제한(429)에 도달했습니다. 잠시 후 다시 시도해 주세요.")
                    if response.status >= 400:
                        raise DownloadError(f"브라우저 파일 서버 오류: HTTP {response.status}")
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > 512 * 1024 * 1024:
                        raise DownloadError("브라우저 재시도 파일이 너무 큽니다(512 MiB 초과).")
                    body = response.body()
                    if not body:
                        raise DownloadError("브라우저에서 빈 파일 응답을 받았습니다.")
                    if len(body) > 512 * 1024 * 1024:
                        raise DownloadError("브라우저 재시도 파일이 너무 큽니다(512 MiB 초과).")
                    target.write_bytes(body)
                    return target

                raise DownloadError(navigation_error or "Chrome/Edge에서 파일 응답을 받지 못했습니다.")
            except DownloadError:
                raise
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
        "Chrome/Edge 브라우저 전송도 실패했습니다. "
        + (" | ".join(errors[-2:]) if errors else "사용 가능한 브라우저가 없습니다.")
    )


def fetch_thumbnail_bytes(url: str, timeout: int = 30) -> bytes:
    """Fetch a Pawchive thumbnail through the browser transport in a worker thread."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "img.pawchive.pw"
        or not parsed.path.startswith("/thumbnail/data/")
    ):
        raise DownloadError("허용되지 않은 Pawchive 썸네일 주소입니다.")
    with tempfile.TemporaryDirectory(prefix="pawchive-thumb-") as directory:
        temporary = Path(directory) / "thumbnail.bin"
        _wait_download_slot()
        _browser_download(url, temporary, timeout)
        payload = temporary.read_bytes()
    if not payload or len(payload) > 16 * 1024 * 1024:
        raise DownloadError("이미지 응답이 비어 있거나 너무 큽니다.")
    return payload


def download_file(
    path_or_url: str,
    destination: str | Path,
    timeout: int = 60,
    session_cookie: str | None = None,
    filename: str | None = None,
) -> Path:
    """Download one file with Python, curl, then isolated Chrome/Edge fallback."""
    url = resolve_file_url(path_or_url, filename=filename)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"파일이 이미 존재합니다: {target}")
    temporary = target.with_name(target.name + ".part")
    headers = {"User-Agent": APP_USER_AGENT, "Accept": "*/*"}
    if session_cookie:
        if any(ch in session_cookie for ch in "\\r\\n;"):
            raise DownloadError("세션 쿠키 값이 올바르지 않습니다.")
        headers["Cookie"] = f"session={session_cookie}"
    request = Request(url, headers=headers, method="GET")
    python_error: Exception | None = None
    try:
        _wait_download_slot()
        with urlopen(request, timeout=timeout) as response, temporary.open("wb") as output:
            if response.status >= 400:
                raise DownloadError(f"파일 서버 오류: HTTP {response.status}")
            shutil.copyfileobj(response, output, length=1024 * 1024)
        os.replace(temporary, target)
        return target
    except HTTPError as exc:
        temporary.unlink(missing_ok=True)
        if exc.code == 404:
            raise DownloadError(
                "Pawchive 파일을 찾지 못했습니다(404). 파일 경로가 바뀌었거나 원본 파일이 없을 수 있습니다."
            ) from exc
        if exc.code == 429:
            raise DownloadError("Pawchive 요청 제한(429)에 도달했습니다. 잠시 후 다시 시도해 주세요.") from exc
        python_error = exc
    except (URLError, OSError, TimeoutError) as exc:
        temporary.unlink(missing_ok=True)
        python_error = exc

    curl_error = ""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        try:
            _wait_download_slot()
            command = [
                curl, "--fail", "--silent", "--show-error",
                "--max-time", str(int(timeout)), "--user-agent", APP_USER_AGENT,
                "--header", "Accept: */*",
            ]
            if session_cookie:
                command.extend(["--header", f"Cookie: session={session_cookie}"])
            command.extend(["--output", str(temporary), url])
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 5)
            if result.returncode == 0 and temporary.exists() and temporary.stat().st_size > 0:
                os.replace(temporary, target)
                return target
            temporary.unlink(missing_ok=True)
            curl_error = (result.stderr or result.stdout or "").strip() or str(result.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            temporary.unlink(missing_ok=True)
            curl_error = str(exc)

    try:
        _wait_download_slot()
        _browser_download(url, temporary, timeout, session_cookie=session_cookie)
        os.replace(temporary, target)
        return target
    except Exception as browser_error:
        temporary.unlink(missing_ok=True)
        details = [f"Python: {python_error}"]
        if curl_error:
            details.append(f"curl: {curl_error}")
        details.append(f"Chrome/Edge: {browser_error}")
        raise DownloadError("파일 다운로드에 실패했습니다. " + " / ".join(details)) from browser_error


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
