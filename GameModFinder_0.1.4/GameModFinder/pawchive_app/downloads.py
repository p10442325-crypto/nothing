from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import stat
import tarfile
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


def download_file(
    path_or_url: str,
    destination: str | Path,
    timeout: int = 60,
    session_cookie: str | None = None,
    filename: str | None = None,
) -> Path:
    """Download one file; the optional session cookie is sent to the file host only."""
    url = resolve_file_url(path_or_url, filename=filename)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"파일이 이미 존재합니다: {target}")
    temporary = target.with_name(target.name + ".part")
    headers = {"User-Agent": APP_USER_AGENT, "Accept": "*/*"}
    if session_cookie:
        if any(ch in session_cookie for ch in "\r\n;"):
            raise DownloadError("세션 쿠키 값이 올바르지 않습니다.")
        headers["Cookie"] = f"session={session_cookie}"
    request = Request(url, headers=headers, method="GET")
    try:
        _wait_download_slot()
        with urlopen(request, timeout=timeout) as response, temporary.open("wb") as output:
            if response.status >= 400:
                raise DownloadError(f"파일 서버 오류: HTTP {response.status}")
            shutil.copyfileobj(response, output, length=1024 * 1024)
        os.replace(temporary, target)
        return target
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, HTTPError) and exc.code in (401, 403):
            raise DownloadError("파일 접근이 거부됐습니다. 해당 파일에 세션 권한이 필요할 수 있습니다.") from exc
        if isinstance(exc, HTTPError) and exc.code == 404:
            raise DownloadError(
                "Pawchive 파일을 찾지 못했습니다(404). 게시물 파일 경로가 변경됐거나 원본 파일이 없을 수 있습니다."
            ) from exc

        # Pawchive asks download clients to identify themselves and use HTTP/1.1.
        # Retry with system curl rather than impersonating a browser.
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if curl:
            try:
                _wait_download_slot()
                command = [
                    curl, "--http1.1", "--fail", "--silent", "--show-error",
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
                detail = (result.stderr or result.stdout or "").strip()
                raise DownloadError(f"curl 재시도 실패: {detail or result.returncode}")
            except (OSError, subprocess.SubprocessError) as curl_exc:
                temporary.unlink(missing_ok=True)
                raise DownloadError(
                    f"파일 다운로드 실패: {exc}. 시스템 curl 재시도도 실패했습니다: {curl_exc}"
                ) from curl_exc
        raise DownloadError(
            f"파일 다운로드 실패: {exc}. 시스템 curl을 찾지 못해 대체 전송을 시도할 수 없습니다."
        ) from exc


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
