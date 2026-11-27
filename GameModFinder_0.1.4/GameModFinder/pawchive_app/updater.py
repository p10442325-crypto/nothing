from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPOSITORY = "p10442325-crypto/nothing"
UPDATE_BRANCH = "feature/pawchive-mod-finder-v2"
BASELINE_COMMIT = "90fc99bd2dd924e224b7b4ee2d50b0fe8d718ab0"
APP_USER_AGENT = (
    "PawchiveModFinder-Updater/1.0 "
    "(+https://github.com/p10442325-crypto/nothing)"
)
MAX_ARCHIVE_BYTES = 180 * 1024 * 1024
MAX_EXTRACTED_BYTES = 350 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateCheck:
    installed_commit: str
    latest_commit: str

    @property
    def available(self) -> bool:
        return self.installed_commit != self.latest_commit


@dataclass(frozen=True)
class UpdatePackage:
    commit: str
    staged_app: Path


class UpdateManager:
    """Checks and stages safe, exact-commit updates from the project's GitHub branch."""

    def __init__(self, app_data_dir: str | Path, local_data_dir: str | Path) -> None:
        self.app_data_dir = Path(app_data_dir)
        self.local_data_dir = Path(local_data_dir)
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        self.local_data_dir.mkdir(parents=True, exist_ok=True)
        self.installed_commit_path = self.app_data_dir / "installed_commit.txt"

    def installed_commit(self) -> str:
        try:
            value = self.installed_commit_path.read_text(encoding="ascii").strip().lower()
        except OSError:
            value = BASELINE_COMMIT
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else BASELINE_COMMIT

    @staticmethod
    def _request_bytes(url: str, timeout: int, maximum: int) -> bytes:
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json, application/zip",
                "User-Agent": APP_USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum:
                        raise UpdateError("업데이트 파일이 허용된 크기(180MB)를 초과했습니다.")
                    chunks.append(chunk)
                return b"".join(chunks)
        except UpdateError:
            raise
        except HTTPError as exc:
            raise UpdateError(f"GitHub 업데이트 서버 오류: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise UpdateError(f"GitHub에 연결하지 못했습니다: {exc}") from exc

    def check(self) -> UpdateCheck:
        url = (
            f"https://api.github.com/repos/{REPOSITORY}/commits/"
            f"{UPDATE_BRANCH}"
        )
        body = self._request_bytes(url, timeout=20, maximum=2 * 1024 * 1024)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise UpdateError("GitHub 업데이트 정보가 올바른 JSON이 아닙니다.") from exc
        latest = str(payload.get("sha", "")).lower() if isinstance(payload, dict) else ""
        if not re.fullmatch(r"[0-9a-f]{40}", latest):
            raise UpdateError("GitHub 응답에서 올바른 업데이트 버전을 확인하지 못했습니다.")
        return UpdateCheck(self.installed_commit(), latest)

    @staticmethod
    def _find_app_prefix(archive: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise UpdateError("업데이트 ZIP에 파일이 너무 많습니다.")

        prefix: tuple[str, ...] | None = None
        for info in entries:
            parts = PurePosixPath(info.filename).parts
            for index in range(len(parts) - 1):
                if parts[index:index + 2] == ("GameModFinder_0.1.4", "GameModFinder"):
                    candidate = parts[:index + 2]
                    if prefix is not None and prefix != candidate:
                        raise UpdateError("업데이트 ZIP의 앱 폴더 구조가 모호합니다.")
                    prefix = candidate
                    break
        if prefix is None:
            raise UpdateError("업데이트 ZIP에서 GameModFinder 폴더를 찾지 못했습니다.")

        prefix_text = "/".join(prefix) + "/"
        selected = [
            info for info in entries
            if info.filename.startswith(prefix_text)
            and not info.filename.endswith("/")
        ]
        required = {
            "main_v2.py",
            "run.bat",
            "requirements.txt",
            "pawchive_app/ui.py",
            "pawchive_app/updater.py",
        }
        found: set[str] = set()
        total_size = 0
        for info in selected:
            relative = info.filename[len(prefix_text):]
            parts = PurePosixPath(relative).parts
            if (
                not parts
                or PurePosixPath(relative).is_absolute()
                or any(part in {"", ".", ".."} for part in parts)
                or (len(parts[0]) >= 2 and parts[0][1] == ":")
            ):
                raise UpdateError("업데이트 ZIP에 안전하지 않은 파일 경로가 있습니다.")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise UpdateError("업데이트 ZIP의 심볼릭 링크는 허용되지 않습니다.")
            total_size += info.file_size
            if total_size > MAX_EXTRACTED_BYTES:
                raise UpdateError("압축 해제 후 업데이트 크기가 허용 범위를 초과합니다.")
            found.add(relative.replace("\\", "/"))
        if not required.issubset(found):
            missing = ", ".join(sorted(required - found))
            raise UpdateError(f"업데이트 ZIP에 필수 파일이 없습니다: {missing}")
        return prefix_text, selected

    def prepare(self, commit: str) -> UpdatePackage:
        commit = str(commit).lower()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise UpdateError("업데이트 버전 식별자가 올바르지 않습니다.")

        update_root = self.local_data_dir / "PawchiveModFinder" / "updates" / commit
        staged_app = update_root / "app"
        archive_path = update_root / "source.zip"
        update_root.mkdir(parents=True, exist_ok=True)

        if not archive_path.is_file() or archive_path.stat().st_size == 0:
            url = f"https://codeload.github.com/{REPOSITORY}/zip/{commit}"
            payload = self._request_bytes(
                url, timeout=120, maximum=MAX_ARCHIVE_BYTES
            )
            temp_path = archive_path.with_suffix(".zip.tmp")
            temp_path.write_bytes(payload)
            temp_path.replace(archive_path)

        if staged_app.exists():
            shutil.rmtree(staged_app)
        staged_app.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(archive_path) as archive:
                prefix, selected = self._find_app_prefix(archive)
                for info in selected:
                    relative = info.filename[len(prefix):]
                    destination = staged_app.joinpath(*PurePosixPath(relative).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
        except UpdateError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
            shutil.rmtree(staged_app, ignore_errors=True)
            raise UpdateError(f"업데이트 ZIP을 안전하게 풀지 못했습니다: {exc}") from exc

        required_paths = (
            staged_app / "main_v2.py",
            staged_app / "run.bat",
            staged_app / "requirements.txt",
            staged_app / "pawchive_app" / "ui.py",
            staged_app / "pawchive_app" / "updater.py",
        )
        if not all(path.is_file() for path in required_paths):
            shutil.rmtree(staged_app, ignore_errors=True)
            raise UpdateError("압축 해제된 업데이트에 필수 앱 파일이 없습니다.")
        return UpdatePackage(commit=commit, staged_app=staged_app)

    def launch_after_exit(
        self, package: UpdatePackage, app_root: str | Path, process_id: int
    ) -> None:
        if os.name != "nt":
            raise UpdateError("앱 자동 적용 및 재시작은 Windows에서만 지원합니다.")
        app_root = Path(app_root).resolve()
        staged_app = package.staged_app.resolve()
        if not (staged_app / "main_v2.py").is_file():
            raise UpdateError("준비된 업데이트 폴더를 찾지 못했습니다.")

        helper_path = self.app_data_dir / "apply_update.py"
        helper_path.write_text(_APPLY_HELPER, encoding="utf-8")
        log_path = self.local_data_dir / "PawchiveModFinder" / "update.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(helper_path),
            str(int(process_id)),
            str(staged_app),
            str(app_root),
            str(self.app_data_dir),
            str(log_path),
            package.commit,
        ]
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        try:
            subprocess.Popen(
                command,
                cwd=str(self.app_data_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise UpdateError(f"업데이트 적용 프로세스를 시작하지 못했습니다: {exc}") from exc


_APPLY_HELPER = r'''from __future__ import annotations

import ctypes
import fnmatch
import os
import shutil
import sys
import traceback
from pathlib import Path


def log(message):
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(message.rstrip() + "\n")


def wait_for_parent(pid):
    if os.name != "nt":
        raise RuntimeError("Windows updater helper only")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, "Could not open the app process")
    try:
        result = kernel32.WaitForSingleObject(handle, 180000)
        if result == 0x00000102:
            raise TimeoutError("The app did not close within three minutes")
        if result != 0:
            raise OSError(ctypes.get_last_error(), "Waiting for the app failed")
    finally:
        kernel32.CloseHandle(handle)


def ignore_runtime(_directory, names):
    ignored = {".venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
    return [name for name in names if name in ignored]


def main():
    pid, staged, install, app_data, log_file, commit = sys.argv[1:7]
    global log_path
    log_path = Path(log_file)
    staged, install, app_data = Path(staged), Path(install), Path(app_data)
    log("Waiting for Pawchive Mod Finder to close.")
    wait_for_parent(int(pid))

    if not (staged / "main_v2.py").is_file() or not (staged / "run.bat").is_file():
        raise RuntimeError("The staged update is incomplete.")

    backup = app_data / "update_backups" / commit
    if backup.exists():
        shutil.rmtree(backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    log("Backing up current application files.")
    shutil.copytree(install, backup, ignore=ignore_runtime)

    try:
        log("Applying update files.")
        shutil.copytree(staged, install, dirs_exist_ok=True, ignore=ignore_runtime)
        marker = app_data / "installed_commit.txt"
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(commit + "\n", encoding="ascii")
        temporary.replace(marker)
    except Exception:
        log("Update failed; restoring the previous application.")
        shutil.copytree(backup, install, dirs_exist_ok=True, ignore=ignore_runtime)
        raise

    log("Update applied. Restarting the application.")
    os.startfile(str(install / "run.bat"))


if __name__ == "__main__":
    log_path = Path(sys.argv[5])
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "Pawchive Mod Finder 업데이트에 실패했습니다. update.log 파일을 확인해 주세요.",
                "Pawchive Mod Finder 업데이트",
                0x10,
            )
        except Exception:
            pass
''';
