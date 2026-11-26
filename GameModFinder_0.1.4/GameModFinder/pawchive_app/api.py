from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .models import Creator, Post

DEFAULT_API = "https://pawchive.pw/api/v1"
APP_USER_AGENT = "PawchiveModFinder/0.2 (+https://github.com/p10442325-crypto/nothing)"


class PawchiveError(RuntimeError):
    pass


class PawchiveClient:
    """Conservative client for Pawchive's public JSON API."""

    def __init__(self, api_base: str = DEFAULT_API, timeout: int = 25,
                 min_interval: float = 1.0, user_agent: str = APP_USER_AGENT) -> None:
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
            clean = {key: value for key, value in params.items() if value is not None}
            if clean:
                url += "?" + urlencode(clean)
        request = Request(url, headers={"Accept": "application/json", "User-Agent": self.user_agent})
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

    @staticmethod
    def _list_payload(payload: Any, label: str) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("posts", "creators", "results", "data", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        raise PawchiveError(f"{label} 응답 형식이 예상과 다릅니다.")

    def list_creators(self) -> list[Creator]:
        raw = self._list_payload(self._get_json("/creators"), "크리에이터 목록")
        result = []
        for item in raw:
            try:
                result.append(Creator.from_api(item))
            except ValueError:
                continue
        return result

    def resolve_creator(self, service: str, creator_id: str) -> Creator:
        service, creator_id = service.strip(), creator_id.strip()
        if not service or not creator_id:
            raise ValueError("서비스와 크리에이터 ID를 모두 입력해 주세요.")
        path = f"/{quote(service, safe='')}/user/{quote(creator_id, safe='')}/profile"
        payload = self._get_json(path)
        if isinstance(payload, dict):
            candidate = payload.get("creator") if isinstance(payload.get("creator"), dict) else payload
            try:
                return Creator.from_api(candidate)
            except ValueError:
                pass
        for creator in self.list_creators():
            if creator.service.casefold() == service.casefold() and creator.creator_id == creator_id:
                return creator
        raise PawchiveError("크리에이터를 찾지 못했습니다. 서비스와 ID를 확인해 주세요.")

    def search_creators(self, query: str = "", service: str | None = None,
                        creator_id: str | None = None) -> list[Creator]:
        needle = query.casefold().strip()
        return [
            creator for creator in self.list_creators()
            if (not service or creator.service.casefold() == service.casefold())
            and (not creator_id or creator.creator_id == creator_id)
            and (not needle or needle in creator.name.casefold()
                 or needle in creator.creator_id.casefold())
        ]

    def creator_posts(self, service: str, creator_id: str, offset: int = 0,
                      query: str | None = None) -> list[Post]:
        if offset < 0 or offset % 50:
            raise ValueError("Pawchive offset은 0 이상의 50 단위여야 합니다.")
        path = f"/{quote(service, safe='')}/user/{quote(creator_id, safe='')}/posts"
        raw = self._list_payload(self._get_json(path, {"q": query, "o": offset}), "크리에이터 게시물 목록")
        creator = Creator(service=service, creator_id=creator_id)
        result = []
        for item in raw:
            try:
                result.append(Post.from_api(item, creator))
            except ValueError:
                continue
        return result

    def recent_posts(self, query: str | None = None, offset: int = 0) -> list[Post]:
        if offset < 0 or offset % 50:
            raise ValueError("Pawchive offset은 0 이상의 50 단위여야 합니다.")
        raw = self._list_payload(self._get_json("/posts", {"q": query, "o": offset}), "게시물 탐색")
        result = []
        for item in raw:
            try:
                result.append(Post.from_api(item))
            except ValueError:
                continue
        return result

    def post_detail(self, service: str, creator_id: str, post_id: str) -> dict[str, Any]:
        path = f"/{quote(service, safe='')}/user/{quote(creator_id, safe='')}/post/{quote(post_id, safe='')}"
        payload = self._get_json(path)
        if isinstance(payload, dict) and isinstance(payload.get("post"), dict):
            return payload["post"]
        if isinstance(payload, dict):
            return payload
        raise PawchiveError("게시물 상세 응답 형식이 예상과 다릅니다.")
