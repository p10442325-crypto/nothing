from __future__ import annotations

import os
import shutil
import stat
import tarfile
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


def download_file(path_or_url: str, destination: str | Path, timeout: int = 60,
                  session_cookie: str | None = None) -> Path:
    """Download one file; an optional session cookie is sent to the file host only."""
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
        temporary.unlink(missing_ok=True)
        if isinstance(exc, HTTPError) and exc.code in (401, 403):
            raise DownloadError("파일 접근이 거부됐습니다. 해당 파일에 세션 권한이 필요할 수 있습니다.") from exc
        raise DownloadError(f"파일 다운로드 실패: {exc}") from exc


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
