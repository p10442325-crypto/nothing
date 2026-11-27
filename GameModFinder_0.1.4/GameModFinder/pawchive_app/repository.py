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
                CREATE TABLE IF NOT EXISTS mod_creator_evidence (
                    service TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    post_title TEXT NOT NULL DEFAULT '',
                    archive_name TEXT NOT NULL,
                    ini_count INTEGER NOT NULL DEFAULT 0,
                    ini_files_json TEXT NOT NULL DEFAULT '[]',
                    scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (service, creator_id, post_id, archive_name)
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
        self, service: str, creator_id: str, limit: int | None = None, offset: int = 0
    ) -> list[dict]:
        query = """
            SELECT * FROM posts WHERE service=? AND creator_id=?
            ORDER BY published DESC, added DESC
        """
        params: list[object] = [service, creator_id]
        if limit is None:
            if offset:
                query += " LIMIT -1 OFFSET ?"
                params.append(max(0, offset))
        else:
            query += " LIMIT ? OFFSET ?"
            params.extend((max(1, limit), max(0, offset)))
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def following_feed(self, limit: int | None = None, offset: int = 0) -> list[dict]:
        query = """
            SELECT p.*, c.name AS creator_name FROM posts p
            JOIN creators c USING(service, creator_id)
            WHERE c.followed=1
            ORDER BY p.published DESC, p.added DESC
        """
        params: list[object] = []
        if limit is None:
            if offset:
                query += " LIMIT -1 OFFSET ?"
                params.append(max(0, offset))
        else:
            query += " LIMIT ? OFFSET ?"
            params.extend((max(1, limit), max(0, offset)))
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
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


    def save_mod_creator_evidence(
        self,
        post: Post,
        archive_name: str,
        ini_files: Iterable[str],
        ini_count: int | None = None,
    ) -> None:
        paths = [str(value) for value in ini_files]
        if not paths:
            return
        count = max(len(paths), int(ini_count or len(paths)))
        with self._connect() as db:
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
                INSERT INTO mod_creator_evidence(
                    service, creator_id, post_id, post_title, archive_name,
                    ini_count, ini_files_json, scanned_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(service, creator_id, post_id, archive_name)
                DO UPDATE SET
                    post_title=excluded.post_title,
                    ini_count=excluded.ini_count,
                    ini_files_json=excluded.ini_files_json,
                    scanned_at=CURRENT_TIMESTAMP
                """,
                (
                    post.service, post.creator_id, post.post_id, post.title,
                    archive_name, count,
                    json.dumps(paths, ensure_ascii=False),
                ),
            )

    def mod_creator_candidates(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT
                    e.service, e.creator_id, COALESCE(c.name, '') AS creator_name,
                    COUNT(*) AS evidence_count,
                    MAX(e.scanned_at) AS last_scanned,
                    (
                        SELECT x.post_id FROM mod_creator_evidence x
                        WHERE x.service=e.service AND x.creator_id=e.creator_id
                        ORDER BY x.scanned_at DESC LIMIT 1
                    ) AS latest_post_id,
                    (
                        SELECT x.post_title FROM mod_creator_evidence x
                        WHERE x.service=e.service AND x.creator_id=e.creator_id
                        ORDER BY x.scanned_at DESC LIMIT 1
                    ) AS latest_post_title,
                    (
                        SELECT x.archive_name FROM mod_creator_evidence x
                        WHERE x.service=e.service AND x.creator_id=e.creator_id
                        ORDER BY x.scanned_at DESC LIMIT 1
                    ) AS latest_archive_name,
                    (
                        SELECT x.ini_count FROM mod_creator_evidence x
                        WHERE x.service=e.service AND x.creator_id=e.creator_id
                        ORDER BY x.scanned_at DESC LIMIT 1
                    ) AS latest_ini_count,
                    (
                        SELECT x.ini_files_json FROM mod_creator_evidence x
                        WHERE x.service=e.service AND x.creator_id=e.creator_id
                        ORDER BY x.scanned_at DESC LIMIT 1
                    ) AS latest_ini_files_json
                FROM mod_creator_evidence e
                LEFT JOIN creators c
                    ON c.service=e.service AND c.creator_id=e.creator_id
                GROUP BY e.service, e.creator_id
                ORDER BY evidence_count DESC, last_scanned DESC, e.service, e.creator_id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def mod_creator_evidence_for_creator(
        self, service: str, creator_id: str
    ) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM mod_creator_evidence
                WHERE service=? AND creator_id=?
                ORDER BY scanned_at DESC, post_id DESC
                """,
                (service, creator_id),
            ).fetchall()
            return [dict(row) for row in rows]
