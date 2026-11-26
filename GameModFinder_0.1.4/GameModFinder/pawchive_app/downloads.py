from __future__ import annotations

import os
import re
import shutil
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .api import APP_USER_AGENT


FILES_HOST = "file.pawchive.pw"


class DownloadError(RuntimeError):
    pass


def resolve_file_url(path_or_url: str) -> str:
    """Resolve API file paths only on Pawchive's dedicated file host."""
    value = path_or_url.strip()
    if not value:
        raise DownloadError("파일 경로가 비어 있습니다.")
    if value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != FILES_HOST:
            raise DownloadError("Pawchive 파일 서버 주소가 아닙니다.")
        return value
    normalized = "/" + value.lstrip("/")
    if not normalized.startswith("/data/"):
        raise DownloadError("허용되지 않은 Pawchive 파일 경로입니다.")
    return f"https://{FILES_HOST}{quote(normalized, safe='/%:@')}"


def download_file(
    path_or_url: str,
    destination: str | Path,
    timeout: int = 60,
    session_cookie: str | None = None,
) -> Path:
    """Download a single file with a transparent app User-Agent.

    The caller should serialize starts to at most one new file request per second
    and keep no more than five downloads active, matching Pawchive guidance.
    """
    url = resolve_file_url(path_or_url)
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
        with urlopen(request, timeout=timeout) as response, temporary.open("wb") as output:
            if response.status >= 400:
                raise DownloadError(f"파일 서버 오류: HTTP {response.status}")
            shutil.copyfileobj(response, output, length=1024 * 1024)
        os.replace(temporary, target)
        return target
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, HTTPError) and exc.code in (401, 403):
            raise DownloadError("파일 접근이 거부됐습니다. 이 파일에 접근 권한이 필요할 수 있습니다.") from exc
        raise DownloadError(f"파일 다운로드 실패: {exc}") from exc


def safe_extract_zip(archive_path: str | Path, destination: str | Path) -> list[Path]:
    """Extract ZIP members only when every target stays inside destination.

    Symlink entries are rejected. Existing files are never overwritten.
    """
    archive = Path(archive_path)
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        planned: list[tuple[zipfile.ZipInfo, Path]] = []
        for member in members:
            raw = member.filename.replace("\\", "/")
            relative = PurePosixPath(raw)
            if relative.is_absolute() or any(part in ("..", "") for part in relative.parts):
                raise DownloadError(f"안전하지 않은 ZIP 경로: {member.filename}")
            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise DownloadError(f"ZIP 심볼릭 링크는 추출하지 않습니다: {member.filename}")
            target = (root / Path(*relative.parts)).resolve()
            if target != root and root not in target.parents:
                raise DownloadError(f"압축 해제 경로가 대상 폴더를 벗어납니다: {member.filename}")
            if target.exists():
                raise FileExistsError(f"압축 해제 대상이 이미 존재합니다: {target}")
            planned.append((member, target))

        for member, target in planned:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            extracted.append(target)
    return extracted
