# 워커 이미지 스모크 테스트 — 컨테이너 안에서 stdin 으로 실행된다(.github/actions/build-worker).
# 비밀값·AWS·Gemini 는 쓰지 않는다(분석 실행 없음).
import os, subprocess, tempfile, warnings
warnings.filterwarnings("ignore")
import mvp_ver_1_9_3                      # whisper·torch·faiss·cv2·genai import
import torch, joblib
assert not torch.cuda.is_available()
print("torch", torch.__version__)
b = joblib.load("models/controversy_model.joblib")
print("classifier classes:", len(b["mlb"].classes_))

import pdf_from_md, fitz
d = tempfile.mkdtemp()
md = os.path.join(d, "t.md")
with open(md, "w", encoding="utf-8") as f:
    f.write("# 한글 테스트\n\n| 항목 | 값 |\n|---|---|\n| 위험도 | 🔴 높음 |\n\n```\ncode 코드\n```\n")
pdf = pdf_from_md.render(md, os.path.join(d, "t.pdf"))
assert pdf, "PDF 생성 실패"
doc = fitz.open(pdf)
fonts = {f[3] for p in doc for f in p.get_fonts()}
print("pdf fonts:", sorted(fonts))
assert any("Nanum" in f for f in fonts), fonts
assert "한글" in doc[0].get_text()

subprocess.run(["python", "-c", "import pipeline"], cwd="copyright_detector", check=True)  # 저작권 분석기 import
subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True)
print("smoke ok")
