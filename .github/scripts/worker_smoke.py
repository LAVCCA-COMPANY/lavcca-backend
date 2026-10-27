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
# 실제 이미지 경로: JPEG 인코딩·디코딩(Pillow·OpenCV) → EasyOCR → CLIP 전처리 → reportlab 이미지 PDF
import cv2, numpy as np, easyocr
from PIL import Image, ImageDraw, ImageFont, __version__ as pil_version
print("pillow", pil_version)
img = Image.new("RGB", (640, 200), "white")
font = ImageFont.truetype("/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf", 64)
ImageDraw.Draw(img).text((30, 60), "LAVCCA 2026", fill="black", font=font)
jpg = os.path.join(d, "sample.jpg")
img.save(jpg, quality=90)
with Image.open(jpg) as im:
    im.load()
    assert im.size == (640, 200) and im.mode == "RGB"
arr = cv2.imread(jpg)
assert arr is not None and arr.shape[:2] == (200, 640)
reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
text = " ".join(t for _, t, _ in reader.readtext(arr)).upper()
print("ocr:", text)
assert "LAVCCA" in text.replace(" ", ""), text

from transformers import CLIPProcessor
px = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")(images=Image.open(jpg), return_tensors="pt")["pixel_values"]
assert tuple(px.shape) == (1, 3, 224, 224), px.shape

from reportlab.pdfgen import canvas
rpdf = os.path.join(d, "img.pdf")
c = canvas.Canvas(rpdf); c.drawImage(jpg, 50, 600, width=320, height=100); c.save()
assert fitz.open(rpdf)[0].get_images(), "reportlab PDF 에 이미지 없음"

print("smoke ok")
