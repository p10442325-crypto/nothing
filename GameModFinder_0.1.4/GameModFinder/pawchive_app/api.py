from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .models import Creator, Post


DEFAULT_API = "https://pawchive.pw/api/v1"
APP_USER_AGENT = "GameModFinder-Pawchive/0.2 (+https://github.com/p10442325-crypto/nothing)"


class PawchiveError(RuntimeError):
    pass


class PawchiveClient:
    """Small public Pawchive API client with conservative request pacing.

    Pawchive's documented API returns creator and creator-post collections as
    JSON arrays. This client does not attach account cookies to API calls.
    """

    def __init__(
        self,
        api_base: str = DEFAULT_API,
        timeout: int = 25,
        min_interval: float = 1.0,
        user_agent: str = APP_USER_AGENT,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = max(5, int(timeout))
        self.min_interval = max(1.0, float(min_interval))
        self.user_agent = user_agent
        self._last_request = 0.0

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        url = self.api_base + "/" + path.lstrip("/")
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urlencode(clean)
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                self._last_request = time.monotonic()
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            self._last_request = time.monotonic()
            if exc.code == 404:
                return []
            if exc.code == 429:
                raise PawchiveError("Pawchive 요청 제한에 도달했습니다. 잠시 후 다시 시도해 주세요.") from exc
            raise PawchiveError(f"Pawchive API 오류: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            self._last_request = time.monotonic()
            raise PawchiveError(f"Pawchive에 연결하지 못했습니다: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PawchiveError("Pawchive 응답을 JSON으로 읽지 못했습니다.") from exc

    def list_creators(self) -> list[Creator]:
        payload = self._get_json("/creators")
        if not isinstance(payload, list):
            raise PawchiveError("크리에이터 목록 응답 형식이 예상과 다릅니다.")
        creators: list[Creator] = []
        for item in payload:
            try:
                creators.append(Creator.from_api(item))
            except ValueError:
                continue
        return creators

    def search_creators(
        self, query: str = "", service: str | None = None, creator_id: str | None = None
    ) -> list[Creator]:
        needle = query.casefold().strip()
        return [
            creator for creator in self.list_creators()
            if (not service or creator.service.casefold() == service.casefold())
            and (not creator_id or creator.creator_id == creator_id)
            and (
                not needle
                or needle in creator.name.casefold()
                or needle in creator.creator_id.casefold()
            )
        ]

    def creator_posts(
        self, service: str, creator_id: str, offset: int = 0, query: str | None = None
    ) -> list[Post]:
        if offset < 0 or offset % 50:
            raise ValueError("Pawchive 게시물 offset은 0 이상의 50 단위여야 합니다.")
        path = f"/{quote(service, safe='')}/user/{quote(creator_id, safe='')}/posts"
        payload = self._get_json(path, {"o": offset, "q": query})
        if not isinstance(payload, list):
            raise PawchiveError("게시물 목록 응답 형식이 예상과 다릅니다.")
        creator = Creator(service=service, creator_id=creator_id)
        posts: list[Post] = []
        for item in payload:
            try:
                posts.append(Post.from_api(item, creator))
            except ValueError:
                continue
        return posts

    def post_detail(self, service: str, creator_id: str, post_id: str) -> dict[str, Any]:
        path = (
            f"/{quote(service, safe='')}/user/{quote(creator_id, safe='')}"
            f"/post/{quote(post_id, safe='')}"
        )
        payload = self._get_json(path)
        if isinstance(payload, dict) and isinstance(payload.get("post"), dict):
            return payload["post"]
        if isinstance(payload, dict):
            return payload
        raise PawchiveError("게시물 상세 응답 형식이 예상과 다릅니다.")
