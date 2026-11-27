from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import zipfile


MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MAX_MEMBER_NAME_LENGTH = 1_024
MAX_REPORTED_INI_FILES = 30


class ArchiveInspectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    archive_format: str
    member_count: int
    ini_count: int
    ini_files: tuple[str, ...]


def _validated_name(name: str) -> str:
    value = (name or "").replace("\\", "/")
    if not value or len(value) > MAX_MEMBER_NAME_LENGTH or "\x00" in value:
        raise ArchiveInspectionError("압축파일 안에 비정상적인 파일명이 있습니다.")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ArchiveInspectionError(f"절대 경로가 포함된 압축파일입니다: {value[:120]}")
    parts = PurePosixPath(value).parts
    if any(part in {"..", "."} for part in parts):
        raise ArchiveInspectionError(f"상위 폴더를 참조하는 경로가 포함되어 있습니다: {value[:120]}")
    return value


def _finish(archive_format: str, entries: list[tuple[str, bool]]) -> ArchiveInspection:
    if len(entries) > MAX_ARCHIVE_MEMBERS:
        raise ArchiveInspectionError(
            f"압축파일 내부 항목이 너무 많습니다(최대 {MAX_ARCHIVE_MEMBERS:,}개)."
        )
    ini_files: list[str] = []
    ini_count = 0
    for raw_name, is_directory in entries:
        name = _validated_name(raw_name)
        if is_directory or name.endswith("/"):
            continue
        if PurePosixPath(name).suffix.casefold() == ".ini":
            ini_count += 1
            if len(ini_files) < MAX_REPORTED_INI_FILES:
                ini_files.append(name)
    return ArchiveInspection(
        archive_format=archive_format,
        member_count=len(entries),
        ini_count=ini_count,
        ini_files=tuple(ini_files),
    )


def analyze_archive_for_ini(archive_path: str | Path) -> ArchiveInspection:
    """Inspect an archive's member names for .ini files without extracting it."""
    path = Path(archive_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ArchiveInspectionError(f"압축파일을 읽을 수 없습니다: {exc}") from exc
    if size <= 0:
        raise ArchiveInspectionError("압축파일이 비어 있습니다.")
    if size > MAX_ARCHIVE_BYTES:
        raise ArchiveInspectionError(
            f"압축파일이 너무 큽니다(검사 한도 {MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB)."
        )

    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                entries = [(info.filename, info.is_dir() or stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)) for info in infos]
            return _finish("ZIP", entries)

        if tarfile.is_tarfile(path):
            with tarfile.open(path, mode="r:*") as archive:
                infos = archive.getmembers()
                entries = [(info.name, not info.isfile()) for info in infos]
            return _finish("TAR", entries)

        suffix = path.name.casefold()
        if suffix.endswith(".7z"):
            try:
                import py7zr
            except ImportError as exc:
                raise ArchiveInspectionError(
                    "7Z 검사를 위한 py7zr 패키지가 없습니다. run.bat으로 앱을 다시 실행해 주세요."
                ) from exc
            with py7zr.SevenZipFile(path, mode="r") as archive:
                names = archive.getnames()
            return _finish("7Z", [(name, name.endswith(("/", "\\"))) for name in names])

        if suffix.endswith(".rar"):
            try:
                import rarfile
            except ImportError as exc:
                raise ArchiveInspectionError(
                    "RAR 검사를 위한 rarfile 패키지가 없습니다. run.bat으로 앱을 다시 실행해 주세요."
                ) from exc
            with rarfile.RarFile(path) as archive:
                infos = archive.infolist()
                entries = [(info.filename, info.isdir()) for info in infos]
            return _finish("RAR", entries)
    except ArchiveInspectionError:
        raise
    except Exception as exc:
        raise ArchiveInspectionError(f"압축파일 내부 목록을 읽지 못했습니다: {exc}") from exc

    raise ArchiveInspectionError(
        "지원하는 압축 형식을 확인하지 못했습니다. ZIP, 7Z, RAR, TAR 계열만 검사할 수 있습니다."
    )
