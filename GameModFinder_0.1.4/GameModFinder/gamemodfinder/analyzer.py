from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

from .models import Post


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def clean_html(value: str) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(text)).strip()


MOD_TERMS = {
    "mod": 15, "modded": 12, "skin": 15, "outfit": 15, "costume": 12,
    "character swap": 18, "model replacement": 18, "texture replacement": 18,
    "model edit": 12, "reshade": 8, "3dmigoto": 18, "3d migoto": 18,
    "gamebanana": 8, "swap": 8,
}
INSTALL_TERMS = ("install", "installation", "how to use", "how to install", "설치", "적용 방법", "설치 방법", "导入", "導入")
DOWNLOAD_TERMS = ("download", "mediafire", "mega.nz", "drive.google", "patreon.com", "다운로드", "배포", "获取")
VERSION_TERMS = ("version", "ver.", "v1.", "v2.", "update", "업데이트", "버전")
ART_TERMS = ("fanart", "fan art", "illustration", "artwork", "drawing", "commission", "wallpaper", "팬아트", "일러스트", "그림", "插画", "イラスト")
VIDEO_EXTS = {"mp4", "webm", "mov", "mkv", "gif"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "avif"}
ARCHIVE_EXTS = {"zip", "7z", "rar", "tar", "gz"}
MOD_FILE_EXTS = {"ini", "buf", "ib", "dds", "vb", "fmt", "ps-t", "ps-c", "vs-t", "vs-c", "ini"}
MODEL_EXTS = {"fbx", "obj", "blend", "pmx", "pmd", "vrm", "glb", "gltf"}


def _contains(text: str, terms: Any) -> bool:
    low = text.casefold()
    for term in terms:
        term = str(term).casefold()
        if term.isascii() and term.replace(" ", "").isalnum():
            pattern = r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
            if re.search(pattern, low):
                return True
        elif term in low:
            return True
    return False


def _extension_counts(post: Post) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attachment in post.all_attachments:
        ext = attachment.extension
        if ext:
            counts[ext] = counts.get(ext, 0) + 1
    return counts


def identify_game(text: str, games: dict[str, Any]) -> str:
    low = text.casefold()
    for key, game in games.items():
        candidates = [game.get("name", ""), *game.get("aliases", [])]
        if any(item and str(item).casefold() in low for item in candidates):
            return key
    return ""


def identify_character(text: str, games: dict[str, Any], game_key: str = "") -> str:
    low = text.casefold()
    game_items = [(game_key, games.get(game_key, {}))] if game_key in games else list(games.items())
    for _, game in game_items:
        for canonical, aliases in game.get("characters", {}).items():
            if canonical.casefold() in low or any(str(alias).casefold() in low for alias in aliases):
                return canonical
    return ""


def analyze_post(
    post: Post,
    games: dict[str, Any],
    selected_game: str = "",
    analyze_body: bool = True,
    analyze_attachments: bool = True,
) -> dict[str, Any]:
    title = post.title or ""
    body = clean_html(post.content) if analyze_body else ""
    filenames = " ".join(a.name or a.path for a in post.all_attachments) if analyze_attachments else ""
    tags = " ".join(post.tags)
    combined = " ".join((title, body, filenames, tags))
    low = combined.casefold()
    score = 0
    reasons: list[str] = []

    for term, points in MOD_TERMS.items():
        if _contains(combined, (term,)):
            score += points
            reasons.append(f"모드 키워드: {term} (+{points})")

    if _contains(body, INSTALL_TERMS):
        score += 25
        reasons.append("설치·적용 안내 발견 (+25)")
    if _contains(body, DOWNLOAD_TERMS):
        score += 10
        reasons.append("다운로드 안내 발견 (+10)")
    if _contains(combined, VERSION_TERMS):
        score += 5
        reasons.append("버전·업데이트 표현 (+5)")

    ext_counts = _extension_counts(post) if analyze_attachments else {}
    if any(ext in ARCHIVE_EXTS for ext in ext_counts):
        score += 20
        reasons.append("압축 파일 첨부 (+20)")
    mod_exts = sorted(set(ext_counts) & MOD_FILE_EXTS)
    if mod_exts:
        score += min(30, 10 + 5 * len(mod_exts))
        reasons.append(f"게임 모드 파일 확장자: {', '.join(mod_exts)}")
    model_exts = sorted(set(ext_counts) & MODEL_EXTS)
    if model_exts:
        score += 8
        reasons.append(f"3D 모델 파일: {', '.join(model_exts)} (+8)")

    if _contains(combined, ART_TERMS):
        # Art labels describe presentation, not necessarily relevance. Penalize only
        # when no concrete mod-file/install evidence has been found.
        concrete_mod_evidence = bool(mod_exts or any(ext in ARCHIVE_EXTS for ext in ext_counts) or _contains(body, INSTALL_TERMS))
        if not concrete_mod_evidence:
            score -= 20
            reasons.append("일러스트·팬아트 중심 표현 (-20)")

    game_key = identify_game(combined, games)
    character = identify_character(combined, games, game_key or selected_game)
    selected_match = not selected_game or game_key == selected_game
    if game_key:
        score += 10
        reasons.append("게임 이름/별칭 확인 (+10)")
    elif selected_game and character:
        score += 8
        reasons.append("선택한 게임의 캐릭터 별칭 확인 (+8)")

    # If user explicitly selected a game but the post does not mention it, do not
    # delete the post: apply a small relevance demotion instead.
    if selected_game and game_key and game_key != selected_game:
        score -= 12
    score = max(0, min(100, score))

    if score >= 70:
        mod_verdict = "모드 가능성 높음"
    elif score >= 40:
        mod_verdict = "모드 가능성 있음"
    else:
        mod_verdict = "모드 근거 부족"

    exts = set(ext_counts)
    if score >= 70 and (exts & (ARCHIVE_EXTS | MOD_FILE_EXTS)):
        category = "게임 모드"
    elif score >= 40:
        category = "모드 관련 자료"
    elif exts & MODEL_EXTS:
        category = "3D 모델"
    elif exts and exts <= VIDEO_EXTS:
        category = "영상"
    elif _contains(combined, ART_TERMS) or (exts and exts <= IMAGE_EXTS):
        category = "일러스트"
    elif exts & VIDEO_EXTS and not (exts & (ARCHIVE_EXTS | MOD_FILE_EXTS)):
        category = "영상"
    else:
        category = "기타"

    # Keep the classification axes independent: a post can be an illustration
    # containing a genuine mod file, so score/verdict remain primary ranking data.
    return {
        "score": score,
        "verdict": mod_verdict,
        "category": category,
        "game_key": game_key or selected_game,
        "character": character,
        "reasons": reasons,
        "extensions": ext_counts,
        "selected_game_match": selected_match,
        "clean_content": body,
    }
