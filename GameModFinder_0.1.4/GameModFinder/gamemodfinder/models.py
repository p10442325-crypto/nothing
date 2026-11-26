from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Attachment:
    name: str = ""
    path: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "Attachment":
        if not isinstance(data, dict):
            return cls(name=str(data or ""))
        return cls(name=str(data.get("name") or ""), path=str(data.get("path") or ""))

    @property
    def extension(self) -> str:
        name = self.name or self.path
        return name.rsplit(".", 1)[-1].lower() if "." in name else ""


@dataclass
class Post:
    id: str
    user: str
    service: str
    title: str = ""
    content: str = ""
    published: str = ""
    added: str = ""
    file: Attachment = field(default_factory=Attachment)
    attachments: list[Attachment] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    embed: dict[str, Any] = field(default_factory=dict)
    shared_file: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Post":
        file_data = data.get("file") or {}
        attachment_data = data.get("attachments") or []
        tags_data = data.get("tags") or []
        if isinstance(tags_data, str):
            tags = [tag.strip() for tag in tags_data.split(",") if tag.strip()]
        elif isinstance(tags_data, list):
            tags = [str(item.get("name", item)) if isinstance(item, dict) else str(item) for item in tags_data]
        else:
            tags = []
        return cls(
            id=str(data.get("id") or ""),
            user=str(data.get("user") or data.get("user_id") or ""),
            service=str(data.get("service") or ""),
            title=str(data.get("title") or ""),
            content=str(data.get("content") or ""),
            published=str(data.get("published") or ""),
            added=str(data.get("added") or ""),
            file=Attachment.from_dict(file_data),
            attachments=[Attachment.from_dict(item) for item in attachment_data if item],
            tags=tags,
            embed=data.get("embed") if isinstance(data.get("embed"), dict) else {},
            shared_file=bool(data.get("shared_file", False)),
            raw=data,
        )

    @property
    def key(self) -> str:
        return f"{self.service}:{self.user}:{self.id}"

    @property
    def post_url(self) -> str:
        if not all((self.service, self.user, self.id)):
            return "https://pawchive.pw"
        return f"https://pawchive.pw/{self.service}/user/{self.user}/post/{self.id}"

    @property
    def all_attachments(self) -> list[Attachment]:
        items = []
        if self.file.name or self.file.path:
            items.append(self.file)
        items.extend(self.attachments)
        return items
