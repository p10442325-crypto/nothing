import json

from gamemodfinder.database import FinderDatabase
from gamemodfinder.models import Post


def test_search_cache_round_trip(tmp_path):
    db = FinderDatabase(tmp_path / "finder.sqlite3")
    raw = {
        "id": "42", "user": "creator", "service": "patreon",
        "title": "Raiden outfit mod", "content": "<p>Install this</p>",
        "attachments": [{"name": "raiden.zip", "path": "/raiden.zip"}],
    }
    post = Post.from_dict(raw)
    result = {
        "post": post, "score": 88, "verdict": "모드 가능성 높음",
        "category": "게임 모드", "game_key": "genshin",
        "character": "라이덴 쇼군", "reasons": ["압축 파일 첨부 (+20)"],
        "extensions": {"zip": 1}, "selected_game_match": True,
        "clean_content": "Install this",
    }
    options = {"aliases": True, "multilingual": True, "body": True, "attachments": True}
    db.save_search_cache("라이덴 의상", "genshin", options, [result])
    loaded = db.load_search_cache("라이덴 의상", "genshin", options)
    assert loaded is not None and len(loaded) == 1
    assert loaded[0]["post"].id == "42"
    assert loaded[0]["score"] == 88
    assert loaded[0]["post"].attachments[0].name == "raiden.zip"
