from __future__ import annotations

from dataclasses import dataclass, field
import html
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
import re


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""


def is_mega_file_url(value: str) -> bool:
    """Return True only for HTTPS MEGA public-file share URLs."""
    parsed = urlparse(_string(value).strip())
    host = (parsed.hostname or "").lower()
    allowed_hosts = {"mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz"}
    modern_file = re.fullmatch(r"/file/[A-Za-z0-9_-]{8}/?", parsed.path or "") is not None
    legacy_file = host.endswith("mega.co.nz") and parsed.path in {"", "/"} and parsed.fragment.startswith("!")
    return (
        parsed.scheme.lower() == "https"
        and host in allowed_hosts
        and parsed.username is None
        and parsed.password is None
        and bool(parsed.fragment)
        and (modern_file or legacy_file)
    )

def find_mega_file_url(value: Any) -> str:
    """Find a MEGA file URL anywhere in an attachment payload.

    Pawchive may include a normal file path and an outbound MEGA URL in
    different properties of the same attachment object. Prefer the MEGA URL
    so it is never handed to the Pawchive file-server resolver.
    """
    if isinstance(value, dict):
        for child in value.values():
            found = find_mega_file_url(child)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = find_mega_file_url(child)
            if found:
                return found
    elif isinstance(value, str):
        candidate = html.unescape(value).strip().rstrip(".,);]")
        if is_mega_file_url(candidate):
            return candidate
        # Some APIs put the URL inside descriptive text or HTML rather than
        # returning a dedicated URL property.
        for match in re.findall(
            r"https://(?:www\\.)?mega(?:\\.nz|\\.co\\.nz)/[^\\s\\\"'<>]+",
            html.unescape(value),
            flags=re.I,
        ):
            candidate = match.rstrip(".,);]")
            if is_mega_file_url(candidate):
                return candidate
    return ""


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    path: str
    kind: str = "attachment"
    deferred: bool = False

    @classmethod
    def from_api(cls, value: Any, kind: str = "attachment") -> "Attachment | None":
        # Pawchive may return a deferred file reference with a name but no path.
        # The actual download URL is supplied later by the post-detail endpoint.
        if isinstance(value, str):
            path = value.strip()
            name = ""
            deferred = False
        elif isinstance(value, dict):
            name = _string(
                value.get("name") or value.get("filename")
                or value.get("original_name") or value.get("file_name")
                or value.get("original_filename") or value.get("filename_orig")
            )
            path = _string(
                value.get("path") or value.get("temp_download_url")
                or value.get("temp_url") or value.get("url")
                or value.get("download_url") or value.get("temporary_url")
                or value.get("file_url") or value.get("original_url")
                or value.get("download_path") or value.get("temp_path")
                or value.get("href")
            )
            deferred_value = value.get("deferred")
            deferred = (
                deferred_value is True
                or str(deferred_value).strip().casefold() in {"1", "true", "yes"}
            )
            if not path and not name:
                nested = value.get("file")
                if isinstance(nested, (str, dict)):
                    return cls.from_api(nested, kind)
                return None
        else:
            return None

        parsed = urlparse(path)
        if not name and path:
            query_names = parse_qs(parsed.query)
            name = next(
                (query_names[key][0] for key in ("f", "filename", "name", "file")
                 if query_names.get(key)),
                "",
            )
        if not name and path:
            name = unquote(parsed.path.rsplit("/", 1)[-1])
        if not name and not path:
            return None
        return cls(name=name or path, path=path, kind=kind, deferred=deferred)


@dataclass(frozen=True, slots=True)
class Creator:
    service: str
    creator_id: str
    name: str = ""
    indexed: str = ""
    updated: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> str:
        return f"{self.service}:{self.creator_id}"

    @property
    def profile_url(self) -> str:
        return f"https://pawchive.pw/{self.service}/user/{self.creator_id}"

    @classmethod
    def from_api(cls, value: Any) -> "Creator":
        if not isinstance(value, dict):
            raise ValueError("Creator response must be an object")
        creator_id = _string(value.get("id") or value.get("user_id"))
        service = _string(value.get("service"))
        if not creator_id or not service:
            raise ValueError("Creator response is missing id or service")
        return cls(
            service=service,
            creator_id=creator_id,
            name=_string(value.get("name") or value.get("username")),
            indexed=_string(value.get("indexed")),
            updated=_string(value.get("updated")),
            raw=dict(value),
        )


@dataclass(frozen=True, slots=True)
class Post:
    service: str
    creator_id: str
    post_id: str
    title: str = ""
    content: str = ""
    published: str = ""
    added: str = ""
    files: tuple[Attachment, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> str:
        return f"{self.service}:{self.creator_id}:{self.post_id}"

    @property
    def post_url(self) -> str:
        return (
            f"https://pawchive.pw/{self.service}/user/{self.creator_id}"
            f"/post/{self.post_id}"
        )

    @classmethod
    def from_api(cls, value: Any, creator: Creator | None = None) -> "Post":
        if not isinstance(value, dict):
            raise ValueError("Post response must be an object")
        service = _string(value.get("service") or (creator.service if creator else ""))
        creator_id = _string(value.get("user") or value.get("creator_id") or (creator.creator_id if creator else ""))
        post_id = _string(value.get("id") or value.get("post_id"))
        if not (service and creator_id and post_id):
            raise ValueError("Post response is missing service, user, or id")

        found: list[Attachment] = []

        def add_attachment(item: Any, kind: str = "attachment") -> None:
            mega_url = find_mega_file_url(item)
            attachment = Attachment.from_api(item, kind)
            if mega_url:
                attachment = Attachment(
                    name=(attachment.name if attachment else "") or "MEGA 외부 파일 링크",
                    path=mega_url,
                    kind="external_link",
                    deferred=False,
                )
            if not attachment:
                return
            if attachment.path:
                duplicate = any(
                    entry.path and entry.path == attachment.path
                    for entry in found
                )
            else:
                # Several deferred refs can all lack a path in list responses.
                # An empty path is not a unique identity and must not collapse
                # a RAR attachment into an earlier pathless preview image.
                identity = (attachment.name.casefold(), attachment.kind)
                duplicate = any(
                    not entry.path
                    and (entry.name.casefold(), entry.kind) == identity
                    for entry in found
                )
            if not duplicate:
                found.append(attachment)

        add_attachment(value.get("file"), "file")

        # Pawchive exposes three distinct attachment sources. In particular,
        # inline files can be separate from the main file and regular attachments.
        for field_name in ("attachments", "files", "inline"):
            raw_items = value.get(field_name)
            if isinstance(raw_items, list):
                for item in raw_items:
                    add_attachment(item, field_name)
            elif isinstance(raw_items, dict):
                if any(key in raw_items for key in (
                    "name", "filename", "path", "url", "download_url", "href", "file"
                )):
                    add_attachment(raw_items, field_name)
                else:
                    for filename, item in raw_items.items():
                        if isinstance(item, str):
                            add_attachment({"name": filename, "path": item}, field_name)
                        elif isinstance(item, dict):
                            normalized = dict(item)
                            normalized.setdefault("name", filename)
                            add_attachment(normalized, field_name)

        add_attachment(value.get("shared_file"), "shared_file")

        # Some inline attachments are represented as download links inside the
        # post body rather than as objects in the API's "inline" array.
        archive_suffixes = (
            ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz",
            ".tgz", ".tbz2", ".txz",
        )
        anchor_pattern = re.compile(
            r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""",
            flags=re.I | re.S,
        )
        def add_mega_link(raw_url: str, label_text: str = "") -> None:
            clean_url = html.unescape(raw_url).strip().rstrip(".,);]")
            parsed = urlparse(clean_url)
            host = (parsed.hostname or "").lower()
            mega_host = host in {
                "mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz",
            }
            modern_file = parsed.path.startswith("/file/") and bool(parsed.fragment)
            legacy_file = (
                host.endswith("mega.co.nz")
                and parsed.path in {"", "/"}
                and parsed.fragment.startswith("!")
            )
            if parsed.scheme != "https" or not mega_host or not (modern_file or legacy_file):
                return
            label = " ".join(label_text.split())
            if not label or label.casefold() in {
                "download", "download here", "click here", "link",
                "mega", "다운로드", "여기", "링크",
            }:
                label = "MEGA 외부 파일 링크"
            attachment = Attachment.from_api(
                {"name": label, "path": clean_url}, "external_link"
            )
            if attachment and not any(
                entry.kind == "external_link" and entry.path == attachment.path
                for entry in found
            ):
                found.append(attachment)

        for href, label in anchor_pattern.findall(_string(value.get("content"))):
            clean_href = html.unescape(href).strip()
            label_text = re.sub(r"<[^>]*>", " ", label)
            label_text = " ".join(html.unescape(label_text).split())
            parsed_href = urlparse(clean_href)
            host = (parsed_href.hostname or "").lower()

            # MEGA links are external downloads, not Pawchive file paths.
            # Keep them as link cards so users can open them in a browser;
            # never feed the encrypted URL to the direct-file downloader.
            if host in {
                "mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz",
            }:
                add_mega_link(clean_href, label_text)
                continue

            safe_host = host in {"pawchive.pw", "www.pawchive.pw", "file.pawchive.pw"}
            safe_path = (
                not parsed_href.scheme
                and not parsed_href.netloc
                and (
                    parsed_href.path.startswith("/data/")
                    or re.match(r"^/[0-9a-f]{2,}/[0-9a-f]{2,}/", parsed_href.path, re.I)
                )
            )
            if not (safe_host or safe_path):
                continue
            candidate_name = label_text or unquote(parsed_href.path.rsplit("/", 1)[-1])
            if not candidate_name.casefold().endswith(archive_suffixes):
                continue
            attachment = Attachment.from_api(
                {"name": candidate_name, "path": clean_href}, "inline"
            )
            if attachment and attachment.path not in {entry.path for entry in found}:
                found.append(attachment)

        # Pawchive content may expose external download URLs as plain text
        # instead of <a href="..."> anchors. Recognize the MEGA file URL form
        # and route it through the same external-link card.
        mega_url_pattern = re.compile(
            r"https://(?:www\.)?mega(?:\.nz|\.co\.nz)/(?:file/[^\s\"'<>]+|#![^\s\"'<>]+)",
            flags=re.I,
        )
        for match in mega_url_pattern.findall(_string(value.get("content"))):
            add_mega_link(match)

        return cls(
            service=service,
            creator_id=creator_id,
            post_id=post_id,
            title=_string(value.get("title")) or "(제목 없음)",
            content=_string(value.get("content")),
            published=_string(value.get("published")),
            added=_string(value.get("added")),
            files=tuple(found),
            raw=dict(value),
        )


_CREATOR_PATH = re.compile(r"^/([^/]+)/user/([^/]+)(?:/post/([^/]+))?/?$")


def parse_creator_url(value: str) -> tuple[str, str]:
    """Parse a Pawchive creator or post URL into (service, creator_id)."""
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {"pawchive.pw", "www.pawchive.pw"}:
        raise ValueError("Pawchive 주소만 입력할 수 있습니다.")
    match = _CREATOR_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("크리에이터 주소 형식이 올바르지 않습니다.")
    service, creator_id, _post_id = match.groups()
    return service, creator_id


def post_matches_search_fields(
    post: Post,
    query: str,
    *,
    include_title: bool = True,
    include_content: bool = True,
) -> bool:
    """Match every query term against only the selected post fields."""
    terms = [term.casefold() for term in (query or "").split() if term]
    if not terms:
        return True
    if not include_title and not include_content:
        return False

    title = " ".join((post.title or "").casefold().split())
    body = post.content or ""
    body = re.sub(r"<[^>]*>", " ", body)
    body = re.sub(r"\s+", " ", html.unescape(body)).casefold()

    searchable = " ".join(
        value for enabled, value in (
            (include_title, title),
            (include_content, body),
        ) if enabled
    )
    return all(term in searchable for term in terms)
