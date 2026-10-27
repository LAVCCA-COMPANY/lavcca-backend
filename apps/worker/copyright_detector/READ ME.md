tui.py 실행

DB 수정 사용법

python main.py --db-admin

또는

python tools/db_admin.py
실행하면 브라우저가 자동으로 열리고 (http://127.0.0.1:8765), 학습 데이터가 종류별 표로 나옵니다.

화면에서 할 수 있는 것
기능	방법
보기	임베딩·로고·음악·폰트·밈·클립이 각각 표로. 상단에 요약(총 학습/작업/발견 수)
수정	제목·권리자·위험도 칸을 직접 고치고 [저장] 클릭
삭제	[삭제] 클릭 (확인 팝업 → 복구 불가)
출처 추적	각 항목에 "어느 작업(job)의 몇 초 프레임"에서 배웠는지 표시 → "왜 이게 학습됐지?" 확인
안전장치
로컬 전용 (127.0.0.1) — 외부에서 접근 불가, 본인 컴퓨터에서만
화이트리스트 보호 — 수정 가능한 필드(제목·권리자·위험도 등)만 열려 있고, id·embedding 같은 핵심 필드는 웹에서 못 바꿈 (검증 완료)
의존성 0 — 파이썬 내장 웹서버만 사용, 새 패키지 설치 불필요
정리하면 3가지 방법이 생겼습니다
웹 페이지 (--db-admin) — 직관적, 표에서 클릭 수정·삭제 ← 방금 추가
CLI 목록 (--learned) — 터미널에서 전체 목록
CLI 삭제 (--forget emb:3) — 터미널에서 개별 삭제
검증도 마쳤습니다 — 렌더링·수정·삭제·필드보호·실제 HTTP 응답 전부 정상입니다. 종료는 터미널에서 Ctrl+C입니다.


-----------------예전-----------------
실행 방법: python main.py "영상파일명.mp4"

다시 처음부터 강제로 시작: python main.py "영상파일명.mp4" --force


영상 다운로드:

python utils/downloader.py "https://youtu.be/xxxx"
저장 폴더 지정:

python utils/downloader.py "https://youtu.be/xxxx" --output ./downloads
화질 선택 (기본 1080p):

python utils/downloader.py "https://youtu.be/xxxx" --quality 720
정보만 보기 (다운로드 없이):

python utils/downloader.py "https://youtu.be/xxxx" --info
다운로드 후 바로 분석:

python utils/downloader.py "https://youtu.be/xxxx"
python main.py "downloads/영상제목.mp4"
유튜브 외에도 인스타그램, 트위터/X, 틱톡 등 yt-dlp가 지원하는 1000개 이상 사이트에서 다 됩니다.