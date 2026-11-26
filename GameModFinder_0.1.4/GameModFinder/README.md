# Game Mod Finder 0.1.4

Pawchive 공개 API의 게시물을 검색하고, 제목·본문·태그(제공되는 경우)·첨부 파일명에 규칙을 적용해 게임 모드 관련성을 점수화하는 Windows 데스크톱 MVP입니다.

## 0.1.4 주요 변경

- 검색 스레드가 완전히 종료되기 전에 QThread 참조를 해제하던 문제 수정
- 검색 도중 창을 닫으면 작업 스레드가 안전하게 종료된 뒤 프로그램이 닫히도록 변경
- 검색 실패 시에도 스레드 객체 수명이 안전하게 관리되도록 수정

## 0.1.3 주요 변경

- Pawchive 연결을 Python `requests` 방식에서 백그라운드 브라우저 방식으로 변경
- 설치된 Google Chrome을 우선 사용하고, 사용할 수 없으면 Microsoft Edge를 시도
- 일반 Chrome 프로필, 로그인 쿠키, 저장된 비밀번호는 사용하지 않는 임시 브라우저 세션 사용
- 브라우저 응답의 JSON을 기존 검색·분석 파이프라인에 전달
- TLS 연결 실패와 브라우저 실행 실패 메시지 개선

## 현재 구현된 기능

- 원신, 붕괴: 스타레일, 젠레스 존 제로용 기본 게임/캐릭터/의상 별칭 데이터
- 한국어 검색어를 영문·중문·일문 별칭으로 확장
- Pawchive 공개 API 검색 및 중복 게시물 통합
- 게시물 제목, HTML 본문, 파일명, 태그(응답에 존재할 경우), 파일 확장자 분석
- 모드 점수, 카테고리, 분석 근거 표시
- 결과 탭 필터, 원본 게시물 열기, 즐겨찾기
- 검색어·게임·분석 옵션별 검색 결과 캐시와 SQLite 즐겨찾기 저장

## 실행 (Windows)

1. Python 3.10 이상을 설치합니다: https://www.python.org/downloads/windows/
2. 설치 화면에서 **Add Python to PATH**와 **Python Launcher**를 활성화합니다.
3. Google Chrome 또는 Microsoft Edge를 설치합니다. 사용자가 평소 쓰는 Chrome을 종료할 필요는 없습니다.
4. ZIP을 압축 해제합니다.
5. `GameModFinder` 폴더 안의 `run.bat`을 파일 탐색기에서 더블클릭합니다.

첫 실행에는 Python 패키지 설치가 필요하므로 인터넷 연결이 필요합니다. 별도의 Chromium 브라우저를 다운로드하지 않고, PC에 이미 설치된 Chrome/Edge를 백그라운드로 사용합니다.

### PowerShell에서 실행하고 싶다면

`GameModFinder` 폴더로 이동한 뒤 다음 명령 한 줄을 실행하세요.

```powershell
cmd.exe /c ".\run.bat"
```

중요: `run.bat` 파일의 내용을 PowerShell 창에 복사해서 한 줄씩 실행하면 안 됩니다. `.bat` 파일은 명령 프롬프트(cmd.exe)가 실행해야 합니다.

### PowerShell에서 수동 실행

아래 명령을 `GameModFinder` 폴더에서 차례대로 실행할 수도 있습니다.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

## 데이터 저장 위치

검색 결과와 즐겨찾기는 사용자 폴더에 저장됩니다.

- Windows: `%APPDATA%\GameModFinder\finder.sqlite3`

별칭 데이터는 `data/game_aliases.json`에서 직접 편집할 수 있습니다.

## 판별 방식과 한계

이 버전은 AI 분류가 아닌 규칙 기반 점수화입니다. 설치 안내, 모드 관련 표현, 압축 파일, 모드 파일 확장자 등의 근거를 가산하고, 팬아트 표현은 구체적인 모드 근거가 없을 때만 감점합니다. 이미지나 영상이 함께 있다는 이유만으로 모드 가능성을 낮추지는 않습니다.

Pawchive 응답에 게시물 태그가 없을 수 있으므로, 태그는 응답에 들어올 때만 분석합니다. 첨부 파일은 검색 응답에 있는 이름과 경로만 확인합니다. 현재 버전은 원격 압축 파일을 자동 다운로드하거나 실행하지 않으며, 압축파일 내부의 파일 목록 검사 기능은 아직 포함하지 않았습니다.

## 주의 사항

- 브라우저 연결은 설치된 Chrome 또는 Edge를 사용합니다. 두 브라우저가 모두 없거나 브라우저 실행이 정책으로 제한된 PC에서는 Pawchive 검색을 사용할 수 없습니다.
- 브라우저는 검색 중에만 백그라운드로 실행되고 검색 종료 후 닫힙니다.
- 앱은 사용자의 기존 Chrome 프로필을 열지 않으며, 개인 로그인 쿠키나 저장된 비밀번호에 접근하지 않습니다.
- Pawchive의 이용 조건과 제작자 권리를 존중해 주세요.
