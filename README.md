reports 파일에 이제까지 돌려본 결과들 있음

api 돌리면 로컬로 웹사이트에서 실행 가능, pdf 리포트 생성 가능

지금은 mvp 1_9_2.py 로 문장 분석 및 PRS CRS 판단

곧 copyright detector도 합쳐서 최종본 MVP 만들 예정

controversy_samples.json과 cade_db.json을 이용해서 controversy_model.joblib를 classifier_model.py로 문장 학습시킴

trend_updater.py를 사용해서 최근 뉴스/기사 분석, 키워드, 사건 등을 찾고 review_update.py로 검토 후 controversy_model.joblib 학습

현재 API 노출로 인해 다시 조금 수정해서 사용하고 싶으면 개인적으로 API 보내줘야됨

python 3.14로 실행
## 웹 실행 및 보안 (베타 전 필수 확인)

- `mvp 1_9_3/app.py`는 **인증이 없기 때문에 `127.0.0.1:5000`(이 PC)에서만 접속되도록** 실행됩니다.
- **인증(로그인/접근 제어)을 도입하기 전에는 외부(인터넷·사내망)에 공개하지 마세요.** `0.0.0.0` 바인딩, 포트 포워딩, 리버스 프록시 공개 모두 해당됩니다.
- 웹 입력은 **YouTube URL · Google Drive URL · 텍스트**만 지원합니다. 서버의 로컬 파일 경로는 웹에서 받지 않습니다(텍스트로 취급). 로컬 영상 분석은 CLI(`python mvp_ver_1_9_3.py`)를 사용하세요.
- 분석 오류 시 사용자에게는 일반 안내 문구만 보여 주고, 상세 원인은 서버 콘솔 로그에 남깁니다.
- 동시 분석은 기본 2건까지만 실행되고, 초과 요청은 `429`(잠시 후 다시 시도)로 거절됩니다. 한도는 환경변수 `NATAM_MAX_CONCURRENT_JOBS`로 바꿀 수 있습니다.
- 웹 화면의 리포트는 서식 태그(제목·표·목록·코드·링크 등)만 남기고 스크립트·이벤트 속성·`javascript:` 링크를 제거한 뒤 표시합니다.
- Yandex 역이미지 검색은 영상 프레임을 공개 임시 호스트(0x0.st/tmpfiles.org)에 업로드해야 하므로 **기본 비활성**입니다. 필요하면 `copyright_detector/.env`에 `YANDEX_PUBLIC_UPLOAD_ENABLED=true`(및 `SERPAPI_KEY`)를 명시적으로 설정하세요.
