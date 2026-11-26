from gamemodfinder.database import FinderDatabase
from gamemodfinder.sync import sync_creator


class FakeAPI:
    def get_creator_profile(self, service, user_id):
        return {"name": "Creator Test"}

    def get_creator_posts(self, service, user_id, offset=0):
        if offset == 0:
            return [
                {
                    "id": "new-2", "service": service, "user": user_id,
                    "title": "Raiden swimsuit MOD",
                    "content": "Download and install this outfit replacement",
                    "attachments": [{"name": "raiden.zip", "path": "/raiden.zip"}],
                    "published": "2026-11-26",
                },
                {
                    "id": "seen-1", "service": service, "user": user_id,
                    "title": "Older post",
                },
            ]
        return []


def test_creator_sync_stops_at_last_seen_post(tmp_path):
    db = FinderDatabase(tmp_path / "finder.sqlite3")
    db.add_creator("patreon", "creator-1", "creator-1", followed=True)
    db.record_creator_sync("patreon", "creator-1", "seen-1", 0)

    result = sync_creator(
        db, FakeAPI(), {}, "patreon", "creator-1", max_pages=3,
    )

    assert result["new_posts"] == 1
    assert result["latest_post_id"] == "new-2"
    assert result["reached_previous_post"] is True
    creator = db.get_creator("patreon", "creator-1")
    assert creator is not None
    assert creator["last_seen_post_id"] == "new-2"
    assert creator["name"] == "Creator Test"
    assert creator["post_count"] == 1
