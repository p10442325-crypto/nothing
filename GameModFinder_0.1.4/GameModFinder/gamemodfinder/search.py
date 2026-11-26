from __future__ import annotations

import re
from typing import Any, Callable

from .analyzer import analyze_post
from .api import PawchiveClient
from .models import Post


class SearchService:
    def __init__(self, aliases: dict[str, Any], api_client: PawchiveClient) -> None:
        self.aliases = aliases
        self.api = api_client

    @staticmethod
    def _valid_query(value: str) -> str:
        value = re.sub(r"\s+", " ", value).strip()
        return value if len(value) >= 3 else ""

    def expand_queries(self, user_query: str, game_key: str, use_aliases: bool = True, multilingual: bool = True, limit: int = 7) -> list[str]:
        raw = user_query.strip()
        if not raw:
            return []
        queries: list[str] = []

        def add(value: str) -> None:
            value = self._valid_query(value)
            if value and value.casefold() not in {q.casefold() for q in queries} and len(queries) < max(1, limit):
                queries.append(value)

        add(raw)
        if not use_aliases:
            return queries

        game = self.aliases.get(game_key, {}) if game_key else {}
        # Keep raw Korean tokens in mixed queries; add canonical aliases separately.
        tokens = [token for token in re.split(r"[\s,;|]+", raw) if token]
        character_entry: tuple[str, list[str]] | None = None
        outfit_entry: tuple[str, list[str]] | None = None

        for canonical, aliases in game.get("characters", {}).items():
            all_names = [canonical, *[str(x) for x in aliases]]
            if any(name.casefold() in raw.casefold() for name in all_names if name):
                character_entry = (canonical, all_names)
                break
        for canonical, aliases in game.get("outfits", {}).items():
            all_names = [canonical, *[str(x) for x in aliases]]
            if any(name.casefold() in raw.casefold() for name in all_names if name):
                outfit_entry = (canonical, all_names)
                break

        # Prioritize paired character/outfit forms. A small API query budget should
        # spend requests on meaningful combinations before generic aliases.
        if character_entry and outfit_entry:
            chars = character_entry[1]
            outfits = outfit_entry[1]
            english_chars = [x for x in chars if x.isascii() and len(x) >= 3]
            foreign_chars = [x for x in chars if not x.isascii() and len(x) >= 3]
            english_outfits = [x for x in outfits if x.isascii() and len(x) >= 3]
            foreign_outfits = [x for x in outfits if not x.isascii() and len(x) >= 3]
            pairs = [
                (character_entry[0], outfit_entry[0]),
                (english_chars[0] if english_chars else character_entry[0], english_outfits[0] if english_outfits else outfit_entry[0]),
            ]
            if foreign_chars and foreign_outfits:
                pairs.append((foreign_chars[-1], foreign_outfits[-1]))
            for char_name, outfit_name in pairs:
                add(f"{char_name} {outfit_name}")

        if character_entry:
            canonical, names = character_entry
            add(canonical)
            for alias in names[1:]:
                add(alias)
        if outfit_entry:
            canonical, names = outfit_entry
            add(canonical)
            for alias in names[1:]:
                add(alias)
        if multilingual:
            for alias in game.get("aliases", []):
                add(str(alias))

        # Include a few meaningful component queries for multiword input.
        for token in tokens:
            if len(token) >= 3:
                add(token)
        return queries

    def expand_local_queries(
        self, user_query: str, game_key: str, use_aliases: bool = True,
        multilingual: bool = True,
    ) -> list[str]:
        """Expand a local query broadly; local matching has no remote request budget."""
        raw = re.sub(r"\s+", " ", user_query).strip()
        if not raw:
            return []
        queries: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            key = text.casefold()
            if len(text) >= 2 and key not in seen:
                seen.add(key)
                queries.append(text)

        add(raw)
        if not use_aliases:
            return queries

        games = [(game_key, self.aliases.get(game_key, {}))] if game_key in self.aliases else list(self.aliases.items())
        lower = raw.casefold()
        matched_characters: list[list[str]] = []
        matched_outfits: list[list[str]] = []

        for _, game in games:
            for canonical, aliases in game.get("characters", {}).items():
                names = [str(canonical), *[str(value) for value in aliases]]
                if any(name and name.casefold() in lower for name in names):
                    matched_characters.append(names)
                    for name in names:
                        add(name)
            for canonical, aliases in game.get("outfits", {}).items():
                names = [str(canonical), *[str(value) for value in aliases]]
                if any(name and name.casefold() in lower for name in names):
                    matched_outfits.append(names)
                    for name in names:
                        add(name)
            if multilingual:
                for alias in game.get("aliases", []):
                    if str(alias).casefold() in lower:
                        add(alias)

        # Include combinations as well as independent aliases to catch titles
        # where the character and costume are indexed together.
        for characters in matched_characters:
            for outfits in matched_outfits:
                for character in characters:
                    for outfit in outfits:
                        add(f"{character} {outfit}")

        for token in re.split(r"[\s,;|]+", raw):
            add(token)
        return queries

    def search(
        self,
        user_query: str,
        game_key: str,
        use_aliases: bool = True,
        multilingual: bool = True,
        analyze_body: bool = True,
        analyze_attachments: bool = True,
        pages_per_query: int = 1,
        max_queries: int = 7,
        max_posts: int = 250,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        queries = self.expand_queries(user_query, game_key, use_aliases, multilingual, max_queries)
        if not queries:
            return [], []
        unique: dict[str, Post] = {}
        notes: list[str] = []
        page_count = max(1, min(int(pages_per_query), 5))
        for query_index, query in enumerate(queries, start=1):
            if progress:
                progress(f"API 검색 {query_index}/{len(queries)}: {query}")
            for page in range(page_count):
                offset = page * 50
                try:
                    results = self.api.get_posts(query, offset=offset)
                except Exception as exc:
                    notes.append(f"'{query}' 검색 실패: {exc}")
                    break
                if not results:
                    break
                for item in results:
                    post = Post.from_dict(item)
                    if not post.id:
                        continue
                    unique.setdefault(post.key, post)
                    if len(unique) >= max_posts:
                        break
                if len(results) < 50 or len(unique) >= max_posts:
                    break
            if len(unique) >= max_posts:
                notes.append(f"결과 안전 상한 {max_posts}개에 도달해 수집을 종료했습니다.")
                break

        analyzed: list[dict[str, Any]] = []
        for post in unique.values():
            result = analyze_post(
                post, self.aliases, selected_game=game_key,
                analyze_body=analyze_body, analyze_attachments=analyze_attachments,
            )
            # Search relevance blends explicit mod evidence with query token matches.
            search_blob = " ".join((post.title, result.get("clean_content", ""), " ".join(post.tags))).casefold()
            query_tokens = [t.casefold() for t in re.findall(r"[\w가-힣一-龯ぁ-んァ-ン]{2,}", user_query)]
            matched_tokens = sum(1 for token in query_tokens if token in search_blob)
            result["score"] = min(100, max(0, result["score"] + min(12, matched_tokens * 4)))
            result["post"] = post
            result["reasons"] = result["reasons"][:]
            analyzed.append(result)

        analyzed.sort(key=lambda item: (
            item["score"],
            item["post"].published or item["post"].added,
        ), reverse=True)
        return analyzed, notes
