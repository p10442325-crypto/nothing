from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode, urljoin


class PawchiveAPIError(RuntimeError):
    pass


class PawchiveClient:
    """Fetch Pawchive's public JSON API through an isolated headless Chrome session.

    This avoids Python TLS clients and uses the locally installed Google Chrome,
    which is useful on systems where Chrome can reach Pawchive but Python cannot.
    No existing Chrome profile, account cookies, or saved passwords are accessed.
    """

    def __init__(self, base_url: str, timeout: int = 20, min_interval: float = 1.0) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = max(5, int(timeout))
        self.min_interval = max(0.0, float(min_interval))
        self._last_request_at = 0.0
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._browser_channel = ""

    def _ensure_browser(self) -> None:
        if self._page is not None:
            return

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PawchiveAPIError(
                "브라우저 연결 기능이 설치되지 않았습니다. Game Mod Finder 폴더에서 "
                "run.bat을 실행해 필요한 패키지를 설치해 주세요."
            ) from exc

        self._playwright = sync_playwright().start()
        launch_errors: list[str] = []
        # Playwright controls the already-installed branded browser. It does not
        # download a second browser or use the user's normal Chrome profile.
        for channel in ("chrome", "msedge"):
            try:
                self._browser = self._playwright.chromium.launch(
                    channel=channel,
                    headless=True,
                )
                self._browser_channel = channel
                break
            except Exception as exc:  # Browser may not be installed at its standard path.
                launch_errors.append(f"{channel}: {exc}")

        if self._browser is None:
            self._playwright.stop()
            self._playwright = None
            raise PawchiveAPIError(
                "백그라운드 브라우저를 실행하지 못했습니다. Google Chrome 또는 Microsoft Edge가 "
                "설치되어 있는지 확인해 주세요.\n\n"
                "브라우저 실행 오류: " + " | ".join(launch_errors[-2:])
            )

        try:
            self._context = self._browser.new_context(
                locale="ko-KR",
                ignore_https_errors=False,
            )
            self._page = self._context.new_page()
            self._page.set_default_navigation_timeout(self.timeout * 1000)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close only the temporary browser session created by this client."""
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        self._context = None
        self._browser = None
        self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _parse_posts_payload(body: str) -> list[dict[str, Any]]:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            preview = (body or "").strip().replace("\r", " ").replace("\n", " ")[:180]
            if "cloudflare" in preview.casefold() or "challenge" in preview.casefold():
                raise PawchiveAPIError(
                    "Pawchive가 브라우저 확인 페이지를 반환했습니다. Chrome에서 사이트 접속이 가능한지 확인한 뒤 다시 시도해 주세요."
                ) from exc
            raise PawchiveAPIError(
                "Pawchive가 JSON이 아닌 응답을 반환했습니다. 응답 일부: " + (preview or "(내용 없음)")
            ) from exc

        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("posts", "results", "data", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        raise PawchiveAPIError("예상하지 못한 Pawchive 응답 형식입니다.")

    def get_posts(self, query: str, offset: int = 0) -> list[dict[str, Any]]:
        if len(query.strip()) < 3:
            return []
        if offset < 0 or offset % 50:
            raise ValueError("Pawchive offset must be a non-negative multiple of 50")

        self._ensure_browser()
        url = urljoin(self.base_url, "posts")
        url = f"{url}?{urlencode({'q': query.strip(), 'o': offset})}"

        for attempt in range(2):
            self._pace()
            try:
                response = self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                self._last_request_at = time.monotonic()
            except Exception as exc:
                self._last_request_at = time.monotonic()
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                raise PawchiveAPIError(
                    "Chrome에서 Pawchive에 접속하지 못했습니다. "
                    f"브라우저 네트워크 오류: {exc}"
                ) from exc

            if response is None:
                raise PawchiveAPIError("Pawchive에서 HTTP 응답을 받지 못했습니다.")

            status = response.status
            if status == 429:
                try:
                    retry_after = response.headers.get("retry-after", "2")
                    wait = min(30.0, max(1.0, float(retry_after)))
                except (AttributeError, TypeError, ValueError):
                    wait = 3.0
                if attempt == 0:
                    time.sleep(wait)
                    continue
                raise PawchiveAPIError("요청이 너무 많아 Pawchive가 잠시 제한했습니다. 잠시 후 다시 시도해 주세요.")

            if status >= 500 and attempt == 0:
                time.sleep(1.5)
                continue
            if status >= 400:
                try:
                    detail = response.text()[:240].strip()
                except Exception:
                    detail = ""
                raise PawchiveAPIError(f"Pawchive API 오류 ({status}): {detail}")

            try:
                body = response.text()
            except Exception as exc:
                raise PawchiveAPIError(f"Pawchive 응답을 읽지 못했습니다: {exc}") from exc
            return self._parse_posts_payload(body)

        return []


    def _get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        """Fetch one public API endpoint using the same isolated browser session."""
        self._ensure_browser()
        url = urljoin(self.base_url, endpoint.lstrip("/"))
        if params:
            url = f"{url}?{urlencode(params)}"

        for attempt in range(2):
            self._pace()
            try:
                response = self._page.goto(
                    url, wait_until="domcontentloaded", timeout=self.timeout * 1000
                )
                self._last_request_at = time.monotonic()
            except Exception as exc:
                self._last_request_at = time.monotonic()
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                raise PawchiveAPIError(
                    f"Pawchive API 접속 실패 ({endpoint}): {exc}"
                ) from exc

            if response is None:
                raise PawchiveAPIError("Pawchive에서 HTTP 응답을 받지 못했습니다.")
            status = response.status
            if status == 429:
                try:
                    retry_after = response.headers.get("retry-after", "2")
                    wait = min(30.0, max(1.0, float(retry_after)))
                except (AttributeError, TypeError, ValueError):
                    wait = 3.0
                if attempt == 0:
                    time.sleep(wait)
                    continue
                raise PawchiveAPIError("요청이 너무 많아 Pawchive가 잠시 제한했습니다.")
            if status >= 500 and attempt == 0:
                time.sleep(1.5)
                continue
            if status >= 400:
                try:
                    detail = response.text()[:240].strip()
                except Exception:
                    detail = ""
                raise PawchiveAPIError(f"Pawchive API 오류 ({status}): {detail}")
            try:
                body = response.text()
                return json.loads(body)
            except (TypeError, ValueError) as exc:
                # Reuse the established HTML/challenge diagnostics.
                self._parse_posts_payload(body)
                raise PawchiveAPIError(f"Pawchive JSON 응답을 해석하지 못했습니다: {exc}") from exc
        return None

    @staticmethod
    def _extract_collection(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    for nested_key in keys:
                        nested = value.get(nested_key)
                        if isinstance(nested, list):
                            return [item for item in nested if isinstance(item, dict)]
        raise PawchiveAPIError("예상하지 못한 Pawchive 목록 응답 형식입니다.")

    def get_creator_posts(
        self, service: str, creator_id: str, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return posts from one creator (GET /{service}/user/{id}?o=offset)."""
        service = str(service or "").strip().strip("/")
        creator_id = str(creator_id or "").strip().strip("/")
        if not service or not creator_id or "/" in service or "/" in creator_id:
            raise ValueError("service와 creator_id가 올바르지 않습니다.")
        if offset < 0 or offset % 50:
            raise ValueError("Pawchive offset must be a non-negative multiple of 50")
        payload = self._get_json(f"{service}/user/{creator_id}", {"o": offset})
        return self._extract_collection(payload, ("posts", "results", "data", "items"))

    def get_creators(self) -> list[dict[str, Any]]:
        """Return the public creator index when the upstream exposes it."""
        payload = self._get_json("creators")
        return self._extract_collection(payload, ("creators", "results", "data", "items"))


    def get_creator_profile(self, service: str, creator_id: str) -> dict[str, Any]:
        """Fetch the public profile for a creator."""
        service = str(service or "").strip().strip("/")
        creator_id = str(creator_id or "").strip().strip("/")
        if not service or not creator_id or "/" in service or "/" in creator_id:
            raise ValueError("service와 creator_id가 올바르지 않습니다.")
        payload = self._get_json(f"{service}/user/{creator_id}/profile")
        if isinstance(payload, dict):
            for key in ("creator", "profile", "data"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    return nested
            return payload
        raise PawchiveAPIError("예상하지 못한 Pawchive 프로필 응답 형식입니다.")
