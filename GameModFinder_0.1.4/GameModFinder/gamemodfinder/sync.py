from __future__ import annotations

from typing import Any, Callable

from .analyzer import analyze_post
from .api import PawchiveClient
from .database import FinderDatabase
from .models import Post


def sync_creator(
    database: FinderDatabase,
    api: PawchiveClient,
    aliases: dict[str, Any],
    service: str,
    user_id: str,
    *,
    max_pages: int = 5,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Incrementally sync one creator, stopping at the previously seen post.

    Pawchive's public creator timeline uses GET /{service}/user/{id}?o=<offset>.
    API offsets advance in batches of 50. The initial sync is bounded by
    max_pages so a newly-added creator cannot trigger an unbounded crawl.
    """
    creator = database.get_creator(service, user_id)
    if creator is None:
        database.add_creator(service, user_id, followed=True)
        creator = database.get_creator(service, user_id) or {}

    # Pull the public profile once when the local label is still just its ID.
    name = str(creator.get("name") or "")
    if not name or name == user_id:
        try:
            profile = api.get_creator_profile(service, user_id)
            profile_name = str(
                profile.get("name") or profile.get("username") or profile.get("display_name") or ""
            ).strip()
            if profile_name:
                database.add_creator(service, user_id, profile_name)
        except Exception:
            # The post timeline remains useful even if optional profile metadata fails.
            pass

    last_seen = str(creator.get("last_seen_post_id") or "")
    latest_id = ""
    collected: dict[str, Post] = {}
    boundary_found = False
    page_limit = max(1, min(int(max_pages), 100))

    pages_checked = 0
    for page in range(page_limit):
        offset = page * 50
        pages_checked = page + 1
        if progress:
            progress(f"{service}/{user_id} 게시물 동기화 · {offset + 1}번부터 확인")
        raw_posts = api.get_creator_posts(service, user_id, offset=offset)
        if not raw_posts:
            break

        if page == 0:
            first_valid = next((item for item in raw_posts if item.get("id")), None)
            if first_valid is not None:
                latest_id = str(first_valid["id"])

        for raw in raw_posts:
            post = Post.from_dict(raw)
            if not post.id:
                continue
            if last_seen and post.id == last_seen:
                boundary_found = True
                break
            collected.setdefault(post.key, post)

        if boundary_found or len(raw_posts) < 50:
            break

    analyzed: list[dict[str, Any]] = []
    for post in collected.values():
        result = analyze_post(post, aliases)
        result["post"] = post
        analyzed.append(result)

    analyzed.sort(key=lambda item: (
        item["post"].published or item["post"].added, item["post"].id
    ), reverse=True)
    if not latest_id and analyzed:
        latest_id = analyzed[0]["post"].id

    database.upsert_results(analyzed)
    database.record_creator_sync(service, user_id, latest_id or last_seen, len(analyzed))
    return {
        "service": service,
        "user_id": user_id,
        "new_posts": len(analyzed),
        "latest_post_id": latest_id or last_seen,
        "pages_checked": pages_checked,
        "reached_previous_post": boundary_found,
        "results": analyzed,
    }


def sync_followed_creators(
    database: FinderDatabase,
    api: PawchiveClient,
    aliases: dict[str, Any],
    *,
    max_pages: int = 5,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Sync followed creators sequentially to preserve API pacing."""
    summaries: list[dict[str, Any]] = []
    creators = database.get_creators(followed_only=True)
    for index, creator in enumerate(creators, start=1):
        if progress:
            progress(f"제작자 {index}/{len(creators)} 동기화 중: {creator['name']}")
        try:
            summary = sync_creator(
                database, api, aliases,
                creator["service"], creator["user_id"],
                max_pages=max_pages, progress=progress,
            )
            summaries.append(summary)
        except Exception as exc:
            summaries.append({
                "service": creator["service"],
                "user_id": creator["user_id"],
                "new_posts": 0,
                "error": str(exc),
            })
    return summaries
