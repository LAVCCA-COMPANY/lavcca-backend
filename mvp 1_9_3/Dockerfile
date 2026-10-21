# Lavcca(NATAM 엔진) 분석 워커 이미지 (위기 분석 + 저작권 감지기, 큰 이미지 1개). 빌드 컨텍스트 = mvp 1_9_3/
#
#   docker build -t lavcca-worker .
#   docker run --rm \
#     --env-file .env --env-file copyright_detector/.env \
#     -v "<영상 폴더>:/data/in:ro" -v "<결과 폴더>:/data/out" \
#     lavcca-worker --job-id test1 --input /data/in/video.mp4 --output-dir /data/out/test1
#
# 비밀값은 이미지에 넣지 않는다(.dockerignore). 실행 시 환경변수로 주입한다.
#   · docker --env-file 은 값의 따옴표를 벗기지 않는다. .env 값이 "..." 로 감싸져 있으면 따옴표를 빼고 쓸 것.
#   · Google Vision 키 파일은 따로 마운트: -v "<키 파일>:/secrets/gcp.json:ro" -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp.json
# 사례 인덱스(casedb/)는 이미지에 없다 → 유사 사례 검색은 꺼진 채로 돈다(인덱스 배포는 T3 이후).
# Whisper 모델은 넣지 않았다. Gemini 전사 실패 시 Whisper 폴백이 돌면 실행 중 내려받는다.

FROM python:3.11-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WHISPER_FALLBACK=0
# WHISPER_FALLBACK=0: Gemini 전사가 일시 장애로 실패하면 Whisper(모델 미포함, CPU 라 느림) 대신
#   worker.py 가 retry_later 로 끝낸다.

# ffmpeg: 오디오·프레임 추출 / fonts-nanum: 보고서 PDF 한글(없으면 PDF 생성 실패). OCR 은 EasyOCR(pip)
#   trixie 에는 fonts-nanum-coding 이 따로 없다(fonts-nanum 에 합쳐짐). 코드용 폰트가 없으면
#   pdf_from_md 가 본문 폰트로 대체한다.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-nanum \
        libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && ls /usr/share/fonts/truetype/nanum \
    && test -f /usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf \
    && test -f /usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf

WORKDIR /app

# CPU 전용 torch 를 먼저 깐다 → requirements 의 torch==2.6.0 이 이미 만족돼 CUDA 휠(수 GB)을 받지 않는다
RUN pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt ./requirements.txt
COPY copyright_detector/requirements.txt ./copyright_detector/requirements.txt
RUN pip install -r requirements.txt -r copyright_detector/requirements.txt

RUN useradd --create-home app && chown app /app
USER app

# 실행 중 내려받던 모델을 빌드 때 받아 둔다(EasyOCR 한/영, 저작권 CLIP)
RUN python -c "import easyocr; easyocr.Reader(['ko', 'en'], gpu=False, verbose=False)" \
 && python -c "from transformers import CLIPModel, CLIPProcessor; m = 'openai/clip-vit-base-patch32'; CLIPModel.from_pretrained(m); CLIPProcessor.from_pretrained(m)"

COPY --chown=app:app . .

ENTRYPOINT ["python", "worker.py"]
