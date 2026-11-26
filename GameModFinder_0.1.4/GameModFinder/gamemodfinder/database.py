from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .models import Post


class FinderDatabase:
    """SQLite store for posts, creator subscriptions, and local full-text search."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_tokenizer = "trigram"
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
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
                CREATE TABLE IF NOT EXISTS creators (
                    service TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    mod_ratio REAL NOT NULL DEFAULT 0,
                    games_json TEXT NOT NULL DEFAULT '{}',
                    followed INTEGER NOT NULL DEFAULT 0,
                    last_synced TEXT,
                    last_seen_post_id TEXT,
                    new_post_count INTEGER NOT NULL DEFAULT 0,
                    post_count INTEGER NOT NULL DEFAULT 0,
                    discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(service, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_posts_score ON posts(score DESC);
                CREATE INDEX IF NOT EXISTS idx_posts_game ON posts(game_key);
                CREATE INDEX IF NOT EXISTS idx_posts_creator ON posts(service, user_id);
                CREATE INDEX IF NOT EXISTS idx_creators_followed ON creators(followed, name);
            """)
            # Trigram tokenization gives substring matching for Korean, Japanese,
            # and Chinese. Older SQLite builds may lack that tokenizer; retain a
            # regular FTS table and use LIKE as a safe fallback in that case.
            try:
                con.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
                        post_key UNINDEXED, title, content, filenames, tags,
                        tokenize='trigram'
                    )
                """)
                self._fts_tokenizer = "trigram"
            except sqlite3.OperationalError:
                con.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
                        post_key UNINDEXED, title, content, filenames, tags
                    )
                """)
                self._fts_tokenizer = "unicode"

            # Older databases already have posts/favorites; seed creator rows
            # from that library without changing or deleting any saved records.
            con.execute("""
                INSERT OR IGNORE INTO creators(service, user_id, name)
                SELECT DISTINCT service, user_id, user_id
                FROM posts WHERE service <> '' AND user_id <> ''
            """)
            post_count = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            fts_count = con.execute("SELECT COUNT(*) FROM posts_fts").fetchone()[0]
            if fts_count != post_count:
                rows = con.execute("""
                    SELECT post_key, title, content, file_json, attachments_json,
                           tags_json, service, user_id
                    FROM posts
                """).fetchall()
                for row in rows:
                    self._replace_fts(con, row)
            # Recompute compact creator aggregates once at startup.
            creator_keys = con.execute("SELECT service, user_id FROM creators").fetchall()
            for creator in creator_keys:
                self._refresh_creator_stats(con, creator["service"], creator["user_id"])

    @staticmethod
    def _filenames(row: sqlite3.Row | dict[str, Any]) -> str:
        names: list[str] = []
        try:
            primary = json.loads(row["file_json"] or "{}")
            if isinstance(primary, dict):
                names.extend([str(primary.get("name") or ""), str(primary.get("path") or "")])
        except (TypeError, json.JSONDecodeError):
            pass
        try:
            attachments = json.loads(row["attachments_json"] or "[]")
            if isinstance(attachments, list):
                for item in attachments:
                    if isinstance(item, dict):
                        names.extend([str(item.get("name") or ""), str(item.get("path") or "")])
        except (TypeError, json.JSONDecodeError):
            pass
        return " ".join(part for part in names if part)

    @classmethod
    def _replace_fts(cls, con: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]) -> None:
        post_key = str(row["post_key"])
        con.execute("DELETE FROM posts_fts WHERE post_key=?", (post_key,))
        try:
            tags_value = json.loads(row["tags_json"] or "[]")
            if isinstance(tags_value, list):
                tags = " ".join(str(x) for x in tags_value)
            else:
                tags = str(tags_value)
        except (TypeError, json.JSONDecodeError):
            tags = str(row["tags_json"] or "")
        con.execute("""
            INSERT INTO posts_fts(post_key, title, content, filenames, tags)
            VALUES (?, ?, ?, ?, ?)
        """, (
            post_key, str(row["title"] or ""), str(row["content"] or ""),
            cls._filenames(row), tags,
        ))

    @staticmethod
    def _ensure_creator(con: sqlite3.Connection, service: str, user_id: str) -> None:
        if service and user_id:
            con.execute("""
                INSERT OR IGNORE INTO creators(service, user_id, name)
                VALUES (?, ?, ?)
            """, (service, user_id, user_id))

    @staticmethod
    def _refresh_creator_stats(con: sqlite3.Connection, service: str, user_id: str) -> None:
        if not service or not user_id:
            return
        rows = con.execute("""
            SELECT game_key, score, category FROM posts
            WHERE service=? AND user_id=?
        """, (service, user_id)).fetchall()
        total = len(rows)
        # Category is assigned before discovery relevance boosts are applied;
        # using it avoids inflating the creator's mod ratio for keyword matches.
        mod_count = sum(
            1 for row in rows
            if row["category"] in ("게임 모드", "모드 관련 자료")
        )
        games: dict[str, int] = {}
        for row in rows:
            key = str(row["game_key"] or "")
            if key:
                games[key] = games.get(key, 0) + 1
        con.execute("""
            UPDATE creators
            SET post_count=?, mod_ratio=?, games_json=?, updated_at=CURRENT_TIMESTAMP
            WHERE service=? AND user_id=?
        """, (
            total, (mod_count / total) if total else 0.0,
            json.dumps(games, ensure_ascii=False, sort_keys=True), service, user_id,
        ))

    def upsert_results(self, results: list[dict[str, Any]]) -> None:
        creator_keys: set[tuple[str, str]] = set()
        with self.connect() as con:
            for result in results:
                post: Post = result["post"]
                service, user_id = post.service, post.user
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
                row = con.execute(
                    "SELECT * FROM posts WHERE post_key=?", (post.key,)
                ).fetchone()
                self._replace_fts(con, row)
                self._ensure_creator(con, service, user_id)
                if service and user_id:
                    creator_keys.add((service, user_id))
            for service, user_id in creator_keys:
                self._refresh_creator_stats(con, service, user_id)

    def add_creator(self, service: str, user_id: str, name: str = "", followed: bool = False) -> None:
        service = str(service or "").strip().strip("/")
        user_id = str(user_id or "").strip().strip("/")
        if not service or not user_id or "/" in service or "/" in user_id:
            raise ValueError("Pawchive 제작자 service와 ID를 확인해 주세요.")
        with self.connect() as con:
            con.execute("""
                INSERT INTO creators(service, user_id, name, followed)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(service, user_id) DO UPDATE SET
                    name=CASE WHEN excluded.name <> '' THEN excluded.name ELSE creators.name END,
                    followed=CASE WHEN excluded.followed=1 THEN 1 ELSE creators.followed END,
                    updated_at=CURRENT_TIMESTAMP
            """, (service, user_id, name.strip(), int(followed)))
            self._refresh_creator_stats(con, service, user_id)

    def remove_creator(self, service: str, user_id: str) -> None:
        # Unfollowing/removing a creator never deletes their archived posts or favorites.
        with self.connect() as con:
            con.execute("DELETE FROM creators WHERE service=? AND user_id=?", (service, user_id))

    def set_creator_followed(self, service: str, user_id: str, followed: bool) -> None:
        with self.connect() as con:
            self._ensure_creator(con, service, user_id)
            con.execute("""
                UPDATE creators SET followed=?, updated_at=CURRENT_TIMESTAMP
                WHERE service=? AND user_id=?
            """, (int(followed), service, user_id))

    def get_creator(self, service: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM creators WHERE service=? AND user_id=?",
                (service, user_id),
            ).fetchone()
            return dict(row) if row else None

    def get_creators(self, followed_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM creators"
        args: tuple[Any, ...] = ()
        if followed_only:
            sql += " WHERE followed=1"
        sql += " ORDER BY followed DESC, mod_ratio DESC, post_count DESC, name COLLATE NOCASE"
        with self.connect() as con:
            rows = con.execute(sql, args).fetchall()
        creators: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["games"] = json.loads(item.pop("games_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["games"] = {}
            creators.append(item)
        return creators

    def record_creator_sync(
        self, service: str, user_id: str, last_seen_post_id: str, new_count: int
    ) -> None:
        with self.connect() as con:
            self._ensure_creator(con, service, user_id)
            con.execute("""
                UPDATE creators
                SET last_synced=CURRENT_TIMESTAMP,
                    last_seen_post_id=CASE WHEN ? <> '' THEN ? ELSE last_seen_post_id END,
                    new_post_count=?, updated_at=CURRENT_TIMESTAMP
                WHERE service=? AND user_id=?
            """, (
                str(last_seen_post_id or ""), str(last_seen_post_id or ""),
                max(0, int(new_count)), service, user_id,
            ))

    def favorite_creator_suggestions(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("""
                SELECT p.service, p.user_id, COUNT(*) AS favorite_count,
                       COALESCE(c.name, p.user_id) AS name
                FROM favorites f JOIN posts p USING(post_key)
                LEFT JOIN creators c ON c.service=p.service AND c.user_id=p.user_id
                GROUP BY p.service, p.user_id
                ORDER BY favorite_count DESC
            """).fetchall()
        return [dict(row) for row in rows]

    def import_creators(self, payload: Any) -> int:
        if isinstance(payload, dict):
            payload = payload.get("creators", [])
        if not isinstance(payload, list):
            raise ValueError("제작자 JSON은 creators 배열 또는 배열 자체여야 합니다.")
        added = 0
        for item in payload:
            if not isinstance(item, dict):
                continue
            service = str(item.get("service") or "").strip()
            user_id = str(item.get("user_id") or item.get("user") or item.get("id") or "").strip()
            if not service or not user_id:
                continue
            self.add_creator(
                service, user_id, str(item.get("name") or ""),
                followed=bool(item.get("followed", True)),
            )
            added += 1
        return added

    def export_creators(self) -> dict[str, Any]:
        creators = self.get_creators()
        export_items = [{
            "service": c["service"], "user_id": c["user_id"], "name": c["name"],
            "followed": bool(c["followed"]),
        } for c in creators]
        return {"version": 1, "creators": export_items}

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        pieces = re.findall(r"[\w가-힣一-龯ぁ-んァ-ン.-]{2,}", query or "", flags=re.UNICODE)
        seen: set[str] = set()
        terms: list[str] = []
        for piece in pieces:
            normalized = piece.strip(" .-_")
            if normalized and normalized.casefold() not in seen:
                seen.add(normalized.casefold())
                terms.append(normalized)
        return terms

    def search_local(
        self, queries: list[str], game_key: str = "", limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Search only the local archive and rank by relevance + creator quality."""
        query_terms: list[str] = []
        seen: set[str] = set()
        for query in queries:
            for term in self._query_terms(query):
                if term.casefold() not in seen:
                    seen.add(term.casefold())
                    query_terms.append(term)
        if not query_terms:
            return []

        candidate_rows: list[sqlite3.Row] = []
        with self.connect() as con:
            if self._fts_tokenizer == "trigram":
                fts_terms = [term for term in query_terms if len(term) >= 3]
                if fts_terms:
                    expression = " OR ".join(
                        '"' + term.replace('"', '""') + '"' for term in fts_terms[:300]
                    )
                    try:
                        candidate_rows = con.execute("""
                            SELECT p.*,
                                   bm25(posts_fts, 5.0, 1.0, 0.8, 1.5) AS fts_rank,
                                   COALESCE(c.mod_ratio, 0) AS creator_mod_ratio,
                                   COALESCE(c.games_json, '{}') AS creator_games,
                                   COALESCE(f.post_key, '') AS favorite_key
                            FROM posts_fts
                            JOIN posts p ON p.post_key=posts_fts.post_key
                            LEFT JOIN creators c ON c.service=p.service AND c.user_id=p.user_id
                            LEFT JOIN favorites f ON f.post_key=p.post_key
                            WHERE posts_fts MATCH ?
                              AND (?='' OR p.game_key=?)
                            ORDER BY fts_rank
                            LIMIT ?
                        """, (expression, game_key, game_key, max(limit * 4, limit))).fetchall()
                    except sqlite3.OperationalError:
                        candidate_rows = []
            if not candidate_rows:
                clauses: list[str] = []
                params: list[Any] = []
                for term in query_terms[:80]:
                    pattern = f"%{term}%"
                    clauses.append("(p.title LIKE ? OR p.content LIKE ? OR p.tags_json LIKE ? OR p.file_json LIKE ? OR p.attachments_json LIKE ?)")
                    params.extend([pattern] * 5)
                where = " OR ".join(clauses) if clauses else "1=0"
                sql = f"""
                    SELECT p.*, 0.0 AS fts_rank,
                           COALESCE(c.mod_ratio, 0) AS creator_mod_ratio,
                           COALESCE(c.games_json, '{{}}') AS creator_games,
                           COALESCE(f.post_key, '') AS favorite_key
                    FROM posts p
                    LEFT JOIN creators c ON c.service=p.service AND c.user_id=p.user_id
                    LEFT JOIN favorites f ON f.post_key=p.post_key
                    WHERE ({where}) AND (?='' OR p.game_key=?)
                    ORDER BY p.score DESC, COALESCE(p.published, p.added) DESC
                    LIMIT ?
                """
                candidate_rows = con.execute(
                    sql, [*params, game_key, game_key, max(limit * 4, limit)]
                ).fetchall()

        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in candidate_rows:
            item = dict(row)
            try:
                creator_games = json.loads(item.get("creator_games") or "{}")
            except (TypeError, json.JSONDecodeError):
                creator_games = {}
            total_game_posts = sum(int(value or 0) for value in creator_games.values())
            game_focus = (
                int(creator_games.get(game_key, 0)) / total_game_posts
                if game_key and total_game_posts else 0.0
            )
            base_score = float(item.get("score") or 0)
            creator_ratio = float(item.get("creator_mod_ratio") or 0)
            favorite_boost = 8.0 if item.get("favorite_key") else 0.0
            creator_boost = creator_ratio * 18.0 + game_focus * 7.0
            # BM25 values are normally negative; convert rank to a bounded
            # relevance adjustment without letting creator affinity dominate.
            fts_rank = float(item.get("fts_rank") or 0)
            relevance_boost = max(0.0, min(15.0, -fts_rank * 2.0))
            rank_score = base_score + creator_boost + favorite_boost + relevance_boost
            raw = {}
            try:
                raw = json.loads(item.get("raw_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
            post = Post.from_dict(raw) if isinstance(raw, dict) and raw else Post(
                id=str(item.get("id") or ""), user=str(item.get("user_id") or ""),
                service=str(item.get("service") or ""), title=str(item.get("title") or ""),
                content=str(item.get("content") or ""), published=str(item.get("published") or ""),
            )
            try:
                reasons = json.loads(item.get("reasons_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                reasons = []
            try:
                extensions = {}
                for attachment in post.all_attachments:
                    if attachment.extension:
                        extensions[attachment.extension] = extensions.get(attachment.extension, 0) + 1
            except Exception:
                extensions = {}
            result = {
                "post": post,
                "score": min(100, int(round(rank_score))),
                "base_score": int(base_score),
                "verdict": item.get("verdict") or "",
                "category": item.get("category") or "기타",
                "game_key": item.get("game_key") or "",
                "character": item.get("character_name") or "",
                "reasons": reasons if isinstance(reasons, list) else [],
                "extensions": extensions,
                "selected_game_match": bool(game_key and item.get("game_key") == game_key),
                "clean_content": "",
            }
            ranked.append((rank_score, result))
        ranked.sort(key=lambda pair: (
            pair[0], pair[1]["post"].published or pair[1]["post"].added,
        ), reverse=True)
        return [item for _, item in ranked[:limit]]

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
            row = con.execute(
                "SELECT service, user_id FROM posts WHERE post_key=?", (post_key,)
            ).fetchone()
            if row:
                self._ensure_creator(con, row["service"], row["user_id"])
            return True

    def load_cached(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("""
                SELECT p.*, CASE WHEN f.post_key IS NULL THEN 0 ELSE 1 END AS is_favorite
                FROM posts p LEFT JOIN favorites f USING(post_key)
                ORDER BY p.score DESC, COALESCE(p.published, p.added) DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]
