# NATAM 메인 앱 + 저작권 탐지기(서브프로세스) 단일 이미지 — Python 3.12, CPU 전용.
#   docker build -t natam:dev .                       (런타임)
#   docker build -t natam:test --build-arg LOCK=requirements/lock/linux-py312-dev.txt .   (pytest 포함)
# 자세한 사용법: requirements/README.md

# ── builder: 해시 고정 lock 으로 설치 ─────────────────────────────
FROM python:3.12-slim-bookworm AS builder
ARG LOCK=requirements/lock/linux-py312.txt
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*
COPY requirements/ /build/requirements/
# torch/torchvision 은 '+cpu' 빌드(PyTorch CPU 인덱스) — PyPI 기본 torch 는 CUDA 패키지 수 GB 를 끌어온다.
# 모든 줄이 --hash 로 고정되어 있어 어느 인덱스에서 받든 파일이 바뀌면 설치가 실패한다.
# --no-deps: lock 이 전체 의존성 목록이다. openai-whisper 가 Linux 에서 요구하는 triton(GPU 커널, ~250MB)은
# CPU 이미지에서 쓰지 않으므로 lock 에서 제외했다 (requirements/README.md).
RUN pip install --no-cache-dir --prefix=/install --require-hashes --no-deps \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r "/build/${LOCK}"

# ── runtime ─────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm
# ffmpeg/ffprobe: 오디오 추출·저작권 탐지 필수. libglib/libgomp: OpenCV(headless)·FAISS·torch 런타임.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 natam
COPY --from=builder /install /usr/local

# 모델 캐시는 /models 볼륨으로 (EasyOCR·CLIP·Whisper 를 매 컨테이너마다 다시 받지 않게)
# 과금·외부 전송이 생기는 기능은 기본 비활성 (P0/P1 정책)
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models/hf EASYOCR_MODULE_PATH=/models/easyocr XDG_CACHE_HOME=/models/cache \
    TRANSCRIBE_ENGINE=gemini USE_REKOGNITION=false YANDEX_PUBLIC_UPLOAD_ENABLED=false \
    NATAM_MAX_CONCURRENT_JOBS=2

COPY --chown=natam:natam ["mvp 1_9_3/", "/app/mvp 1_9_3/"]
COPY --chown=natam:natam templates/ /app/templates/
RUN mkdir -p "/app/mvp 1_9_3/reports" "/app/mvp 1_9_3/downloads" "/app/mvp 1_9_3/samples" \
             "/app/mvp 1_9_3/casedb" /models /data \
    && chown -R natam:natam /app /models /data
USER natam
WORKDIR "/app/mvp 1_9_3"
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=4)"

# app.py 의 __main__ 은 127.0.0.1 고정이라 컨테이너 밖에서 닿지 않는다 → gunicorn 으로 컨테이너 안에서만
# 0.0.0.0 에 바인딩하고, 호스트 노출 범위는 compose 의 ports(127.0.0.1)로 제한한다.
# 작업 상태가 프로세스 메모리(dict)에 있으므로 워커는 반드시 1개(스레드로 동시 요청 처리).
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:5000", "--timeout", "120", "app:app"]
