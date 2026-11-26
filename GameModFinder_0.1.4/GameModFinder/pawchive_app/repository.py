from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Creator, Post


class LocalRepository:
    """SQLite persistence for creators, followed creators, posts and sync cursors."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS creators (
                    service TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    indexed TEXT NOT NULL DEFAULT '',
                    updated TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    followed INTEGER NOT NULL DEFAULT 0,
                    followed_at TEXT,
                    last_synced TEXT,
                    next_offset INTEGER NOT NULL DEFAULT 0,
                    sync_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (service, creator_id)
                );
                CREATE TABLE IF NOT EXISTS posts (
                    service TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    published TEXT NOT NULL DEFAULT '',
                    added TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (service, creator_id, post_id),
                    FOREIGN KEY (service, creator_id)
                        REFERENCES creators(service, creator_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_posts_published
                    ON posts(published DESC, added DESC);
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    destination TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            db.execute("PRAGMA user_version = 1")

    def save_creator(self, creator: Creator) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO creators(service, creator_id, name, indexed, updated, raw_json)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(service, creator_id) DO UPDATE SET
                    name=excluded.name, indexed=excluded.indexed,
                    updated=excluded.updated, raw_json=excluded.raw_json
                """,
                (
                    creator.service, creator.creator_id, creator.name,
                    creator.indexed, creator.updated,
                    json.dumps(creator.raw, ensure_ascii=False),
                ),
            )

    def save_creators(self, creators: Iterable[Creator]) -> None:
        for creator in creators:
            self.save_creator(creator)

    def follow(self, service: str, creator_id: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE creators SET followed=1,
                    followed_at=COALESCE(followed_at, CURRENT_TIMESTAMP),
                    sync_error=''
                WHERE service=? AND creator_id=?
                """,
                (service, creator_id),
            )
            if db.total_changes == 0:
                raise KeyError(f"Unknown creator: {service}:{creator_id}")

    def unfollow(self, service: str, creator_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE creators SET followed=0 WHERE service=? AND creator_id=?",
                (service, creator_id),
            )

    def followed_creators(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM creators WHERE followed=1 ORDER BY name COLLATE NOCASE"
            ).fetchall()
            return [dict(row) for row in rows]

    def save_posts(self, posts: Iterable[Post]) -> int:
        count = 0
        with self._connect() as db:
            for post in posts:
                db.execute(
                    """
                    INSERT INTO creators(service, creator_id)
                    VALUES(?, ?)
                    ON CONFLICT(service, creator_id) DO NOTHING
                    """,
                    (post.service, post.creator_id),
                )
                db.execute(
                    """
                    INSERT INTO posts(
                        service, creator_id, post_id, title, content,
                        published, added, raw_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(service, creator_id, post_id) DO UPDATE SET
                        title=excluded.title, content=excluded.content,
                        published=excluded.published, added=excluded.added,
                        raw_json=excluded.raw_json
                    """,
                    (
                        post.service, post.creator_id, post.post_id, post.title,
                        post.content, post.published, post.added,
                        json.dumps(post.raw, ensure_ascii=False),
                    ),
                )
                count += 1
        return count

    def posts_for_creator(
        self, service: str, creator_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM posts WHERE service=? AND creator_id=?
                ORDER BY published DESC, added DESC LIMIT ? OFFSET ?
                """,
                (service, creator_id, max(1, limit), max(0, offset)),
            ).fetchall()
            return [dict(row) for row in rows]

    def following_feed(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT p.*, c.name AS creator_name FROM posts p
                JOIN creators c USING(service, creator_id)
                WHERE c.followed=1
                ORDER BY p.published DESC, p.added DESC
                LIMIT ? OFFSET ?
                """,
                (max(1, limit), max(0, offset)),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_sync_state(
        self, service: str, creator_id: str, next_offset: int,
        synced_at: str = "", error: str = ""
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE creators SET next_offset=?, last_synced=?,
                    sync_error=? WHERE service=? AND creator_id=?
                """,
                (max(0, next_offset), synced_at or None, error, service, creator_id),
            )

    def record_download(
        self, service: str, creator_id: str, post_id: str, file_path: str,
        destination: str = "", status: str = "complete", error: str = ""
    ) -> int:
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO downloads(
                    service, creator_id, post_id, file_path,
                    destination, status, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (service, creator_id, post_id, file_path, destination, status, error),
            )
            return int(cursor.lastrowid)
