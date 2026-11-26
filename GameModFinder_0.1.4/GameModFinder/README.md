# Pawchive Mod Finder v2

Pawchive에서 원하는 크리에이터를 찾아 팔로우하고, 게시물을 모아 보고, 첨부 파일을 직접 다운로드하는 Windows 데스크톱 앱입니다.

> 이 프로그램은 모드 탐색·열람·다운로드 도구입니다. 설치된 모드 관리 프로그램이 아닙니다.

## 실행 방법 (Windows)

1. Python 3.10 이상을 설치합니다. https://www.python.org/downloads/windows/
2. Google Chrome 또는 Microsoft Edge를 설치합니다. Python 연결이 Windows에서 끊기는 경우 설치된 브라우저로 다시 연결합니다.
3. ZIP 파일을 내려받아 압축 해제합니다.
4. `GameModFinder` 폴더 안의 `run.bat`을 더블클릭합니다.
5. 첫 실행은 가상환경과 Python 패키지를 자동으로 설치합니다.

## 화면

- **팔로잉**: 팔로우한 크리에이터의 최신 게시물을 모아 보기. 직접 새로고침과 60분 기본 자동 동기화 지원.
- **크리에이터**: 팔로우 목록과 크리에이터별 게시물 보기.
- **크리에이터 찾기**: Pawchive 프로필 URL 또는 서비스 + 크리에이터 ID로 추가.
- **전체 탐색**: Pawchive 전체 최신 게시물 및 키워드 검색.
- **게시물 보기**: 본문 서식(HTML/Markdown)을 살려 표시하고, 이미지 첨부는 Pawchive 썸네일을 불러와 미리보기.
- **다운로드**: 게시물 첨부 파일을 앱에서 다운로드하고 다운로드 기록 확인.
- **설정**: 공통 저장 폴더, 게임별 저장 폴더, 동기화 간격 설정.

## 저장과 압축 해제

- 다운로드 위치는 공통 폴더 또는 사용자가 지정한 게임별 폴더 중에서 다운로드마다 선택합니다. 선택한 기준 폴더에 바로 저장하거나 그 안의 하위 폴더를 골라 저장할 수 있습니다. 자동 게임 판별은 하지 않습니다.
- ZIP 및 TAR 계열 파일은 다운로드 후 안전 검사를 거쳐 자동 압축 해제합니다.
- 기존 파일은 덮어쓰지 않습니다. 압축 파일의 경로 탈출과 심볼릭 링크는 차단합니다.
- 지원하지 않는 압축 형식은 원본 파일로 보관합니다.

## 데이터 위치

설정과 SQLite 데이터베이스는 사용자 데이터 폴더에 저장됩니다.

- Windows: `%APPDATA%\PawchiveModFinder\`
- 설정: `settings.json`
- 데이터베이스: `pawchive.sqlite3`

## Pawchive API와 다운로드

기본 API 주소는 `https://pawchive.pw/api/v1`, 원본 파일 호스트는 `https://file.pawchive.pw/data/...`, 이미지 미리보기는 `https://img.pawchive.pw/thumbnail/data/...` 입니다. API 요청에는 계정 쿠키를 보내지 않습니다. Windows에서 Python HTTPS 연결이 끊기는 API 요청은 설치된 Chrome 또는 Edge의 임시 세션으로 재시도합니다. **파일과 이미지 요청에는 브라우저를 흉내 내는 User-Agent를 사용하지 않습니다.** Pawchive 파일 서버 정책에 맞춰 프로젝트 주소를 포함한 앱 식별 User-Agent, HTTP/1.1, 초당 1회 연결 제한, 파일명 `f` 파라미터를 사용해 `curl.exe`로 전송합니다. 376바이트 반응은 서버의 자동 요청 제한으로 판별해 파일로 저장하지 않습니다. 설정한 선택적 세션 키는 파일/이미지 요청에만 사용됩니다.

참고 문서:
- https://ktoolbox.readthedocs.io/latest/about-pawchive/
- https://ktoolbox.readthedocs.io/latest/api/
- https://ktoolbox.readthedocs.io/latest/faq/

## 범위에서 제외한 기능

설치된 모드 목록 관리, 활성화/비활성화, 모드 업데이트 관리, 삭제/복구, 게임 실행, 제작자 자동 등급화, 게시물 자동 모드 판별, 다른 플랫폼 통합은 제공하지 않습니다.

## 테스트

`python -m unittest discover -s tests -v`
