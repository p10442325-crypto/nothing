from gamemodfinder.analyzer import analyze_post, clean_html
from gamemodfinder.models import Post
from gamemodfinder.search import SearchService

GAMES = {
    "genshin": {
        "name": "원신", "aliases": ["Genshin Impact", "原神"],
        "characters": {"라이덴 쇼군": ["라이덴", "Raiden Shogun", "Raiden", "雷電将軍"]},
        "outfits": {"수영복": ["Swimsuit", "Bikini", "水着"]},
    }
}


def test_clean_html():
    assert clean_html("<p>Install <b>the mod</b></p>") == "Install the mod"


def test_mod_post_scores_higher_than_fanart():
    mod = Post.from_dict({
        "id": "1", "user": "2", "service": "patreon",
        "title": "Raiden Shogun Swimsuit MOD",
        "content": "<p>Download and install this outfit replacement. Version 1.2</p>",
        "attachments": [{"name": "mod.zip", "path": "/mod.zip"}, {"name": "mod.ini", "path": "/mod.ini"}],
    })
    art = Post.from_dict({
        "id": "2", "user": "2", "service": "patreon",
        "title": "Raiden Shogun fanart illustration",
        "content": "<p>My new artwork wallpaper collection</p>",
        "attachments": [{"name": "art.png", "path": "/art.png"}],
    })
    mod_result = analyze_post(mod, GAMES, "genshin")
    art_result = analyze_post(art, GAMES, "genshin")
    assert mod_result["score"] > art_result["score"]
    assert mod_result["category"] in {"게임 모드", "모드 관련 자료"}


def test_expand_queries_has_original_and_aliases():
    class DummyAPI:
        pass
    service = SearchService(GAMES, DummyAPI())
    queries = service.expand_queries("라이덴 수영복", "genshin", limit=7)
    assert "라이덴 수영복" in queries
    assert any("Raiden" in query for query in queries)
    assert any("Swimsuit" in query for query in queries)


def test_model_word_does_not_count_as_mod_keyword():
    post = Post.from_dict({
        "id": "3", "user": "2", "service": "patreon",
        "title": "Raiden character model showcase",
        "content": "<p>3D model preview and render</p>",
        "attachments": [{"name": "character.fbx", "path": "/character.fbx"}],
    })
    result = analyze_post(post, GAMES, "genshin")
    assert not any(reason.startswith("모드 키워드: mod ") for reason in result["reasons"])
