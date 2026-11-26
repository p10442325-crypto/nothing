import pytest

from gamemodfinder.api import PawchiveAPIError, PawchiveClient


def test_parse_posts_payload_accepts_list():
    payload = '[{"id":"1"},{"id":"2"},null]'
    assert PawchiveClient._parse_posts_payload(payload) == [{"id": "1"}, {"id": "2"}]


def test_parse_posts_payload_accepts_wrapped_results():
    payload = '{"results":[{"id":"3"}],"count":1}'
    assert PawchiveClient._parse_posts_payload(payload) == [{"id": "3"}]


def test_parse_posts_payload_rejects_html():
    with pytest.raises(PawchiveAPIError, match="JSON이 아닌 응답"):
        PawchiveClient._parse_posts_payload("<html><body>not json</body></html>")


def test_parse_posts_payload_rejects_unknown_shape():
    with pytest.raises(PawchiveAPIError, match="응답 형식"):
        PawchiveClient._parse_posts_payload('{"count":0,"message":"ok"}')


def test_short_query_does_not_launch_browser():
    client = PawchiveClient("https://pawchive.pw/api/v1")
    assert client.get_posts("ab") == []
    client.close()


def test_offset_must_be_multiple_of_50():
    client = PawchiveClient("https://pawchive.pw/api/v1")
    with pytest.raises(ValueError):
        client.get_posts("Raiden", offset=10)
    client.close()
