from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Post


class FinderDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS posts (
                    post_key TEXT PRIMARY KEY,
                    id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    title TEXT,
                    content TEXT,
                    published TEXT,
                    added TEXT,
                    post_url TEXT,
                    file_json TEXT,
                    attachments_json TEXT,
                    tags_json TEXT,
                    score INTEGER,
                    verdict TEXT,
                    category TEXT,
                    game_key TEXT,
                    character_name TEXT,
                    reasons_json TEXT,
                    raw_json TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    post_key TEXT PRIMARY KEY,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(post_key) REFERENCES posts(post_key)
                );
                CREATE TABLE IF NOT EXISTS search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    game_key TEXT,
                    searched_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS search_cache (
                    query TEXT NOT NULL,
                    game_key TEXT NOT NULL,
                    options_key TEXT NOT NULL,
                    results_json TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(query, game_key, options_key)
                );
                CREATE INDEX IF NOT EXISTS idx_posts_score ON posts(score DESC);
                CREATE INDEX IF NOT EXISTS idx_posts_game ON posts(game_key);
            """)

    def upsert_results(self, results: list[dict[str, Any]]) -> None:
        with self.connect() as con:
            for result in results:
                post: Post = result["post"]
                con.execute("""
                    INSERT INTO posts (
                        post_key, id, user_id, service, title, content, published, added,
                        post_url, file_json, attachments_json, tags_json, score, verdict,
                        category, game_key, character_name, reasons_json, raw_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(post_key) DO UPDATE SET
                        title=excluded.title, content=excluded.content, published=excluded.published,
                        added=excluded.added, post_url=excluded.post_url, file_json=excluded.file_json,
                        attachments_json=excluded.attachments_json, tags_json=excluded.tags_json,
                        score=excluded.score, verdict=excluded.verdict, category=excluded.category,
                        game_key=excluded.game_key, character_name=excluded.character_name,
                        reasons_json=excluded.reasons_json, raw_json=excluded.raw_json,
                        updated_at=CURRENT_TIMESTAMP
                """, (
                    post.key, post.id, post.user, post.service, post.title, post.content,
                    post.published, post.added, post.post_url,
                    json.dumps(post.file.__dict__, ensure_ascii=False),
                    json.dumps([item.__dict__ for item in post.attachments], ensure_ascii=False),
                    json.dumps(post.tags, ensure_ascii=False), result.get("score", 0),
                    result.get("verdict", ""), result.get("category", "기타"),
                    result.get("game_key", ""), result.get("character", ""),
                    json.dumps(result.get("reasons", []), ensure_ascii=False),
                    json.dumps(post.raw, ensure_ascii=False),
                ))


    def save_search_cache(self, query: str, game_key: str, options: dict[str, Any], results: list[dict[str, Any]]) -> None:
        options_key = json.dumps(options, sort_keys=True, ensure_ascii=False)
        payload = []
        for result in results:
            post: Post = result["post"]
            payload.append({
                "raw": post.raw,
                "score": result.get("score", 0),
                "verdict": result.get("verdict", ""),
                "category": result.get("category", "기타"),
                "game_key": result.get("game_key", ""),
                "character": result.get("character", ""),
                "reasons": result.get("reasons", []),
                "extensions": result.get("extensions", {}),
                "selected_game_match": result.get("selected_game_match", True),
                "clean_content": result.get("clean_content", ""),
            })
        with self.connect() as con:
            con.execute("""
                INSERT INTO search_cache(query, game_key, options_key, results_json, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(query, game_key, options_key) DO UPDATE SET
                    results_json=excluded.results_json, updated_at=CURRENT_TIMESTAMP
            """, (query, game_key, options_key, json.dumps(payload, ensure_ascii=False)))

    def load_search_cache(self, query: str, game_key: str, options: dict[str, Any]) -> list[dict[str, Any]] | None:
        from .analyzer import clean_html

        options_key = json.dumps(options, sort_keys=True, ensure_ascii=False)
        with self.connect() as con:
            row = con.execute(
                "SELECT results_json FROM search_cache WHERE query=? AND game_key=? AND options_key=?",
                (query, game_key, options_key),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["results_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        results: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("raw"), dict):
                continue
            post = Post.from_dict(item["raw"])
            results.append({
                "post": post,
                "score": int(item.get("score", 0)),
                "verdict": item.get("verdict", ""),
                "category": item.get("category", "기타"),
                "game_key": item.get("game_key", ""),
                "character": item.get("character", ""),
                "reasons": item.get("reasons", []),
                "extensions": item.get("extensions", {}),
                "selected_game_match": item.get("selected_game_match", True),
                "clean_content": item.get("clean_content", clean_html(post.content)),
            })
        return results

    def add_history(self, query: str, game_key: str) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO search_history(query, game_key) VALUES (?, ?)", (query, game_key))

    def is_favorite(self, post_key: str) -> bool:
        with self.connect() as con:
            row = con.execute("SELECT 1 FROM favorites WHERE post_key=?", (post_key,)).fetchone()
            return row is not None

    def toggle_favorite(self, post_key: str) -> bool:
        with self.connect() as con:
            exists = con.execute("SELECT 1 FROM favorites WHERE post_key=?", (post_key,)).fetchone()
            if exists:
                con.execute("DELETE FROM favorites WHERE post_key=?", (post_key,))
                return False
            con.execute("INSERT OR IGNORE INTO favorites(post_key) VALUES (?)", (post_key,))
            return True

    def load_cached(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("""
                SELECT p.*, CASE WHEN f.post_key IS NULL THEN 0 ELSE 1 END AS is_favorite
                FROM posts p LEFT JOIN favorites f USING(post_key)
                ORDER BY p.score DESC, COALESCE(p.published, p.added) DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]
