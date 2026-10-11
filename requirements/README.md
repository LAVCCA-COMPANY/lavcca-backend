# NATAM 의존성 · 실행 환경 (Python 3.12)

메인 앱과 저작권 탐지기는 **같은 Python 환경**에서 실행된다(탐지기는 `sys.executable` 서브프로세스).

| 파일 | 용도 |
|---|---|
| `main.in` | 메인 엔진 직접 의존성 + 호환 범위 |
| `copyright.in` | 저작권 탐지기 직접 의존성 + 호환 범위 |
| `dev.in` | 테스트 전용 (`pytest`) |
| `lock/linux-py312.txt` | Docker(Linux x86_64) 런타임 — 버전·해시 고정 |
| `lock/linux-py312-dev.txt` | 위 + 테스트 |
| `lock/windows-py312.txt`, `lock/windows-py312-dev.txt` | Windows 로컬 개발 |

`mvp 1_9_3/copyright_detector/requirements.txt` 는 탐지기 단독 Dockerfile 용이며 범위를 `copyright.in` 과 같게 유지한다.

## 버전 고정 원칙
- `scikit-learn==1.8.0` — `controversy_model.joblib` 이 1.8.0 으로 저장됨. 모델 재학습 전에는 바꾸지 않는다.
- `opencv-python-headless<5`, `transformers<5` — 5.x 는 미검증. `opencv-python`(GUI판)은 같은 `cv2` 를 제공해 충돌하므로 쓰지 않는다.
- `torch`/`torchvision` 은 `+cpu` 빌드 (PyTorch CPU 인덱스). PyPI 기본 Linux torch 는 CUDA 패키지 수 GB 를 끌어온다.
- `google-search-results` 가 `from serpapi import GoogleSearch` 를 제공한다. PyPI `serpapi` 패키지는 `GoogleSearch` 가 없고 모듈명이 같아 충돌하므로 설치하지 않는다.
- `openai-whisper`, `google-search-results` 는 PyPI 에 휠이 없어 소스(순수 파이썬)로 설치된다. 그 외는 휠만 허용.
- `openai-whisper`(+torch)는 `mvp_ver_1_9_2.py` 최상단 import 라 전사 엔진이 Gemini 여도 필요하다.
- Linux lock 은 `triton` 을 제외한다(`--no-emit-package triton`). whisper 가 Linux x86_64 에서 요구하지만 CUDA 타이밍 커널
  전용이라 CPU 이미지에서는 쓰이지 않는다(다운로드 ~250MB 절약). 그래서 Docker 는 `pip install --no-deps` 로 lock 만 설치한다.
  GPU 이미지를 만들 때는 이 제외를 없애고 torch 도 CUDA 빌드로 다시 고정할 것.

## 로컬 개발 (Windows)
```powershell
uv venv --python 3.12 .venv
uv pip sync --python .venv requirements/lock/windows-py312-dev.txt --torch-backend cpu
# 또는 pip:  .venv\Scripts\python -m pip install --require-hashes --extra-index-url https://download.pytorch.org/whl/cpu -r requirements/lock/windows-py312-dev.txt
cd "mvp 1_9_3"; ..\.venv\Scripts\python -m pytest tests -q
```
ffmpeg 는 별도 설치 후 PATH 에 두거나 `FFMPEG_BIN` 으로 지정한다.

## Docker
```bash
cp .env.example .env            # GEMINI_API_KEY 등 입력 (커밋 금지)
docker compose build            # 수 GB 다운로드 — 아래 '용량' 참고
docker compose up -d            # http://127.0.0.1:5000 (localhost 에만 게시)
docker compose --profile tools run --rm tests
```
- 사례 DB 는 저장소 루트 `./casedb/` 에 두면 읽기 전용으로 마운트된다. 없으면 유사 사례 검색만 비활성.
- `app.py` 는 시작 시 Gemini 클라이언트를 만들므로 `GEMINI_API_KEY` 가 없으면 컨테이너가 기동하지 않는다.
- `USE_REKOGNITION`, `YANDEX_PUBLIC_UPLOAD_ENABLED` 는 기본 `false`. 과금·외부 전송에 동의한 경우에만 켠다.

## lock 재생성
```bash
cd requirements
for plat in linux windows; do
  p=$([ $plat = linux ] && echo x86_64-manylinux_2_28 || echo x86_64-pc-windows-msvc)
  x=$([ $plat = linux ] && echo "--no-emit-package triton")
  uv pip compile main.in copyright.in        -o lock/$plat-py312.txt     --python-version 3.12 --python-platform $p \
     --torch-backend cpu --only-binary :all: --no-binary openai-whisper --no-binary google-search-results \
     --index-url https://pypi.org/simple --emit-index-url --generate-hashes --custom-compile-command "see requirements/README.md" $x
  uv pip compile main.in copyright.in dev.in -o lock/$plat-py312-dev.txt --python-version 3.12 --python-platform $p \
     --torch-backend cpu --only-binary :all: --no-binary openai-whisper --no-binary google-search-results \
     --index-url https://pypi.org/simple --emit-index-url --generate-hashes --custom-compile-command "see requirements/README.md" $x
done
```
