from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
from tempfile import TemporaryDirectory
from typing import Callable, Iterable

from .archive_analysis import analyze_archive_for_ini
from .downloads import download_file
from .models import Post, post_matches_search_fields


ARCHIVE_SUFFIXES = (
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz",
    ".tgz", ".tbz2", ".txz", ".tar.gz", ".tar.bz2", ".tar.xz",
)
MAX_SEARCH_PAGES = 20
MAX_SEARCH_QUERIES = 100


@dataclass(frozen=True, slots=True)
class CreatorDiscoveryResult:
    queries: tuple[str, ...]
    pages_fetched: int
    archive_posts_found: int
    archives_scanned: int
    ini_archives_found: int
    creators_found: int
    errors: tuple[str, ...]


def load_game_catalog(path: str | Path | None = None) -> dict[str, dict]:
    """Load the bundled game -> character alias catalogue."""
    source = Path(path) if path else Path(__file__).resolve().parent.parent / "data" / "game_aliases.json"
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict) and isinstance(value.get("characters"), dict)}


def game_search_terms(game: dict) -> list[str]:
    """Build broad Pawchive search terms from a game's names and character aliases."""
    values = [str(game.get("name") or "")]
    aliases = game.get("aliases", [])
    if isinstance(aliases, list):
        values.extend(str(value) for value in aliases)
    characters = game.get("characters", {})
    if isinstance(characters, dict):
        for character, names in characters.items():
            values.append(str(character))
            if isinstance(names, list):
                values.extend(str(value) for value in names)
    return parse_search_queries("\n".join(values))


def _post_matches_discovery_term(post: Post, query: str, *, include_title: bool, include_content: bool) -> bool:
    if post_matches_search_fields(post, query, include_title=include_title, include_content=include_content):
        return True
    terms = [term.casefold() for term in query.split() if term]
    if not terms:
        return True
    parts = [f"{item.name} {item.path}" for item in post.files]
    for key in ("tags", "tag", "labels", "categories"):
        value = post.raw.get(key)
        if isinstance(value, (str, int)):
            parts.append(str(value))
        elif isinstance(value, list):
            parts.extend(str(item) for item in value if isinstance(item, (str, int)))
        elif isinstance(value, dict):
            parts.extend(str(item) for item in value.values() if isinstance(item, (str, int)))
    searchable = " ".join(parts).casefold()
    return bool(searchable) and all(term in searchable for term in terms)

def parse_search_queries(value: str) -> list[str]:
    """Split user-entered character names on commas, semicolons or newlines."""
    queries: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,;\r\n]+", value or ""):
        query = " ".join(raw.split())
        key = query.casefold()
        if query and key not in seen:
            seen.add(key)
            queries.append(query)
    return queries


def is_archive_attachment(name: str, path: str = "") -> bool:
    for value in (name or "", path or ""):
        lowered = value.split("?", 1)[0].casefold()
        if any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES):
            return True
    return False


def scan_mod_creator_candidates(
    client,
    repository,
    queries: Iterable[str],
    *,
    page_limit: int = 5,
    include_title: bool = True,
    include_content: bool = True,
    session_cookie: str | None = None,
    temporary_root: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> CreatorDiscoveryResult:
    """Search Pawchive pages, inspect archive listings, and persist creators backed by INI files.

    The archive is downloaded to a temporary folder. Only member names are read;
    files are neither extracted nor executed. Individual archive failures are
    recorded and do not stop the rest of the discovery run.
    """
    normalized_queries: list[str] = []
    seen_queries: set[str] = set()
    for query in queries:
        normalized = " ".join(str(query).split())
        key = normalized.casefold()
        if normalized and key not in seen_queries:
            seen_queries.add(key)
            normalized_queries.append(normalized)
    if not normalized_queries:
        raise ValueError("검색어를 하나 이상 입력해 주세요.")
    if len(normalized_queries) > MAX_SEARCH_QUERIES:
        raise ValueError(f"검색어는 한 번에 최대 {MAX_SEARCH_QUERIES}개까지 입력할 수 있습니다.")
    if not include_title and not include_content:
        raise ValueError("제목 또는 내용을 검색 범위로 선택해 주세요.")

    page_limit = max(1, min(MAX_SEARCH_PAGES, int(page_limit)))
    pages_fetched = 0
    archive_posts: set[str] = set()
    scanned_archives: set[tuple[str, str]] = set()
    positive_archives: set[tuple[str, str]] = set()
    positive_creators: set[tuple[str, str]] = set()
    errors: list[str] = []
    temp_root = Path(temporary_root) if temporary_root else None
    if temp_root:
        temp_root.mkdir(parents=True, exist_ok=True)

    def report(message: str) -> None:
        if progress:
            progress(message)

    for query_index, query in enumerate(normalized_queries, start=1):
        for page_index in range(page_limit):
            offset = page_index * 50
            report(
                f"'{query}' 검색 중 · 검색어 {query_index}/{len(normalized_queries)} "
                f"· 페이지 {page_index + 1}/{page_limit}"
            )
            posts = client.recent_posts(query, offset=offset)
            pages_fetched += 1
            for post in posts:
                if not _post_matches_discovery_term(
                    post, query, include_title=include_title, include_content=include_content
                ):
                    continue
                archives = [
                    attachment for attachment in post.files
                    if is_archive_attachment(attachment.name, attachment.path)
                ]
                if not archives:
                    continue
                archive_posts.add(post.key)

                for attachment in archives:
                    archive_key = (post.key, attachment.path or attachment.name)
                    if archive_key in scanned_archives:
                        continue
                    scanned_archives.add(archive_key)
                    report(
                        f"압축파일 확인 중 · {post.title[:50]} · "
                        f"{attachment.name or '첨부 파일'}"
                    )
                    raw_name = Path(
                        (attachment.name or attachment.path.rsplit("/", 1)[-1]).split("?", 1)[0]
                    ).name
                    safe_name = re.sub(
                        r'[<>:"/\\|?*\x00-\x1f]', "_", raw_name
                    ).strip(" .") or "archive.bin"
                    try:
                        with TemporaryDirectory(
                            prefix="pawchive_discovery_",
                            dir=str(temp_root) if temp_root else None,
                        ) as temporary:
                            target = Path(temporary) / safe_name
                            saved = download_file(
                                attachment.path,
                                target,
                                session_cookie=session_cookie,
                                filename=attachment.name or None,
                            )
                            inspection = analyze_archive_for_ini(saved)
                        if inspection.ini_count:
                            repository.save_mod_creator_evidence(
                                post,
                                attachment.name or safe_name,
                                inspection.ini_files,
                                inspection.ini_count,
                            )
                            positive_archives.add(archive_key)
                            positive_creators.add((post.service, post.creator_id))
                    except Exception as exc:
                        if len(errors) < 30:
                            errors.append(
                                f"{post.service}:{post.creator_id} · "
                                f"{attachment.name or safe_name}: {exc}"
                            )

            if len(posts) < 50:
                break

    report(
        f"자동 탐색 완료 · 압축파일 {len(scanned_archives):,}개 검사 · "
        f"INI 발견 {len(positive_archives):,}개 · 제작자 {len(positive_creators):,}명"
    )
    return CreatorDiscoveryResult(
        queries=tuple(normalized_queries),
        pages_fetched=pages_fetched,
        archive_posts_found=len(archive_posts),
        archives_scanned=len(scanned_archives),
        ini_archives_found=len(positive_archives),
        creators_found=len(positive_creators),
        errors=tuple(errors),
    )
