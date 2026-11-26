from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .models import Creator, Post

DEFAULT_API = "https://pawchive.pw/api/v1"
APP_USER_AGENT = "PawchiveModFinder/1.0 (+https://github.com/p10442325-crypto/nothing)"


class PawchiveError(RuntimeError):
    pass


class PawchiveClient:
    """Public API client with a real-browser fallback for Windows TLS resets.

    Normal API calls do not carry Pawchive account cookies. If Python's TLS
    connection is reset by the remote host, retry the same public URL through
    the locally installed Chrome or Edge browser via Playwright.
    """

    def __init__(self, api_base: str = DEFAULT_API, timeout: int = 25,
                 min_interval: float = 1.0, user_agent: str = APP_USER_AGENT) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = max(5, int(timeout))
        self.min_interval = max(1.0, float(min_interval))
        self.user_agent = user_agent
        self._last_request = 0.0
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._browser_channel = ""

    def close(self) -> None:
        """Release only the temporary browser session created by this client."""
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        self._context = self._browser = self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None

    def __enter__(self) -> "PawchiveClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _browser_get_text(self, url: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PawchiveError(
                "Python 연결이 차단됐고 브라우저 연결 패키지도 없습니다. "
                "앱 폴더에서 run.bat을 다시 실행해 주세요."
            ) from exc

        if self._page is None:
            self._playwright = sync_playwright().start()
            launch_errors: list[str] = []
            for channel in ("chrome", "msedge"):
                try:
                    self._browser = self._playwright.chromium.launch(
                        channel=channel,
                        headless=True,
                    )
                    self._browser_channel = channel
                    break
                except Exception as exc:
                    launch_errors.append(f"{channel}: {exc}")
            if self._browser is None:
                self._playwright.stop()
                self._playwright = None
                raise PawchiveError(
                    "Python 연결이 끊겨 Chrome/Edge로 재시도했지만 브라우저를 실행하지 못했습니다. "
                    "Google Chrome 또는 Microsoft Edge를 설치해 주세요. "
                    + " | ".join(launch_errors[-2:])
                )
            self._context = self._browser.new_context(
                locale="ko-KR",
                user_agent=self.user_agent,
                ignore_https_errors=False,
            )
            self._page = self._context.new_page()
            self._page.set_default_navigation_timeout(self.timeout * 1000)

        try:
            response = self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout * 1000,
            )
            if response is None:
                raise PawchiveError("Chrome/Edge에서 Pawchive HTTP 응답을 받지 못했습니다.")
            if response.status == 404:
                return "[]"
            if response.status == 429:
                raise PawchiveError("Pawchive 요청 제한(429)에 도달했습니다. 잠시 후 다시 시도해 주세요.")
            if response.status >= 400:
                raise PawchiveError(f"Chrome/Edge Pawchive API 오류: HTTP {response.status}")
            return response.text()
        except PawchiveError:
            raise
        except Exception as exc:
            raise PawchiveError(
                f"Chrome/Edge에서도 Pawchive 연결이 실패했습니다: {exc}"
            ) from exc

    def _decode_json(self, body: str) -> Any:
        try:
            return json.loads(body)
        except (TypeError, ValueError) as exc:
            preview = (body or "").strip().replace("\\r", " ").replace("\\n", " ")[:180]
            raise PawchiveError(
                "Pawchive가 JSON이 아닌 응답을 반환했습니다. "
                f"응답 일부: {preview or '(내용 없음)'}"
            ) from exc

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        url = self.api_base + "/" + path.lstrip("/")
        if params:
            clean = {key: value for key, value in params.items() if value is not None}
            if clean:
                url += "?" + urlencode(clean)
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                self._last_request = time.monotonic()
                body = response.read().decode("utf-8")
            return self._decode_json(body)
        except HTTPError as exc:
            self._last_request = time.monotonic()
            if exc.code == 404:
                return []
            if exc.code == 429:
                raise PawchiveError("Pawchive 요청 제한(429)에 도달했습니다. 잠시 후 다시 시도해 주세요.") from exc
            if exc.code not in (403, 500, 502, 503, 520, 521, 522, 524):
                raise PawchiveError(f"Pawchive API 오류: HTTP {exc.code}") from exc
            python_error: Exception = exc
        except (URLError, TimeoutError, OSError) as exc:
            self._last_request = time.monotonic()
            python_error = exc

        # Some Windows networks reset Python's TLS socket while the installed
        # browser can still load the same HTTPS endpoint. Retry without cookies.
        self._last_request = time.monotonic()
        try:
            return self._decode_json(self._browser_get_text(url))
        except PawchiveError as browser_error:
            raise PawchiveError(
                "Pawchive 연결에 실패했습니다. "
                f"Python 연결: {python_error}. 브라우저 재시도: {browser_error}"
            ) from browser_error

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
        result: list[Creator] = []
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

    def creator_tags(self, service: str, creator_id: str) -> list[str]:
        """Return the Pawchive tags registered for one creator."""
        path = f"/{quote(service, safe='')}/user/{quote(creator_id, safe='')}/tags"
        payload = self._get_json(path)
        if isinstance(payload, dict):
            for key in ("tags", "results", "data", "items"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    payload = candidate
                    break
                if isinstance(candidate, dict):
                    payload = [{"tag": name} for name in candidate]
                    break
            else:
                if any(key in payload for key in ("tag", "name", "label", "value")):
                    payload = [payload]
                else:
                    payload = [{"tag": key} for key in payload]

        if not isinstance(payload, list):
            return []

        result: list[str] = []
        seen: set[str] = set()
        for item in payload:
            if isinstance(item, str):
                value = item
            elif isinstance(item, dict):
                value = (
                    item.get("tag") or item.get("name") or item.get("label")
                    or item.get("value") or item.get("id") or ""
                )
                if isinstance(value, dict):
                    value = value.get("name") or value.get("tag") or ""
            else:
                continue
            tag = str(value).strip()
            key = tag.casefold()
            if tag and key not in seen:
                seen.add(key)
                result.append(tag)
        return sorted(result, key=str.casefold)

    def creator_posts(self, service: str, creator_id: str, offset: int = 0,
                      query: str | None = None, tag: str | None = None) -> list[Post]:
        if offset < 0 or offset % 50:
            raise ValueError("Pawchive offset은 0 이상의 50 단위여야 합니다.")
        path = f"/{quote(service, safe='')}/user/{quote(creator_id, safe='')}"
        raw = self._list_payload(
            self._get_json(path, {"q": query, "tag": tag, "o": offset}),
            "크리에이터 게시물 목록",
        )
        creator = Creator(service=service, creator_id=creator_id)
        result: list[Post] = []
        for item in raw:
            try:
                result.append(Post.from_api(item, creator))
            except ValueError:
                continue
        return result

    def all_creator_posts(self, service: str, creator_id: str, query: str | None = None,
                         max_pages: int | None = None, tag: str | None = None) -> list[Post]:
        """Fetch every available 50-item page, optionally restricted to a creator tag."""
        result: list[Post] = []
        offset = 0
        pages = 0
        while max_pages is None or pages < max_pages:
            if tag:
                page = self.creator_posts(
                    service, creator_id, offset=offset, query=query, tag=tag
                )
            else:
                page = self.creator_posts(
                    service, creator_id, offset=offset, query=query
                )
            result.extend(page)
            pages += 1
            if len(page) < 50:
                break
            offset += 50
        return result

    def recent_posts(self, query: str | None = None, offset: int = 0) -> list[Post]:
        if offset < 0 or offset % 50:
            raise ValueError("Pawchive offset은 0 이상의 50 단위여야 합니다.")
        raw = self._list_payload(
            self._get_json("/posts", {"q": query, "o": offset}),
            "게시물 탐색",
        )
        result: list[Post] = []
        for item in raw:
            try:
                result.append(Post.from_api(item))
            except ValueError:
                continue
        return result

    def all_recent_posts(self, query: str | None = None, max_pages: int | None = None) -> list[Post]:
        """Fetch every available 50-item page from the global feed."""
        result: list[Post] = []
        offset = 0
        pages = 0
        while max_pages is None or pages < max_pages:
            page = self.recent_posts(query=query, offset=offset)
            result.extend(page)
            pages += 1
            if len(page) < 50:
                break
            offset += 50
        return result

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
