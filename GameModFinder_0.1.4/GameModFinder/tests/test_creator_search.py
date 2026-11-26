from gamemodfinder.database import FinderDatabase
from gamemodfinder.models import Post


def _result(post):
    return {
        "post": post,
        "score": 84,
        "verdict": "모드 가능성 높음",
        "category": "게임 모드",
        "game_key": "genshin",
        "character": "라이덴 쇼군",
        "reasons": ["모드 키워드 발견"],
    }


def test_creator_roster_import_export_and_follow(tmp_path):
    db = FinderDatabase(tmp_path / "finder.sqlite3")
    db.add_creator("patreon", "creator-1", "Creator One", followed=True)
    db.add_creator("fanbox", "creator-2", "Creator Two", followed=False)

    followed = db.get_creators(followed_only=True)
    assert len(followed) == 1
    assert followed[0]["name"] == "Creator One"

    exported = db.export_creators()
    assert len(exported["creators"]) == 2

    other = FinderDatabase(tmp_path / "other.sqlite3")
    assert other.import_creators(exported) == 2
    assert len(other.get_creators(followed_only=True)) == 1


def test_local_search_indexes_post_and_creator_stats(tmp_path):
    db = FinderDatabase(tmp_path / "finder.sqlite3")
    post = Post.from_dict({
        "id": "post-42",
        "user": "creator-42",
        "service": "patreon",
        "title": "라이덴 수영복 MOD",
        "content": "<p>Install this outfit replacement</p>",
        "published": "2026-11-20",
        "attachments": [{"name": "raiden_mod.zip", "path": "/raiden_mod.zip"}],
        "tags": ["Genshin", "Raiden"],
    })
    db.upsert_results([_result(post)])

    results = db.search_local(["라이덴 수영복"], game_key="genshin")
    assert len(results) == 1
    assert results[0]["post"].id == "post-42"
    assert results[0]["category"] == "게임 모드"

    creator = db.get_creator("patreon", "creator-42")
    assert creator is not None
    assert creator["post_count"] == 1
    assert creator["mod_ratio"] == 1.0
    assert creator["games_json"] == '{"genshin": 1}'


def test_favorite_creator_suggestions_include_writer(tmp_path):
    db = FinderDatabase(tmp_path / "finder.sqlite3")
    post = Post.from_dict({
        "id": "post-9", "user": "favorite-author", "service": "fanbox",
        "title": "Test mod",
    })
    db.upsert_results([_result(post)])
    assert db.toggle_favorite(post.key) is True
    suggestions = db.favorite_creator_suggestions()
    assert suggestions == [{
        "service": "fanbox",
        "user_id": "favorite-author",
        "favorite_count": 1,
        "name": "favorite-author",
    }]
