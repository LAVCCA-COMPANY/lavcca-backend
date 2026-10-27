"""
utils/model_load.py - torch 모델 로드 직렬화용 프로세스 전역 잠금

transformers from_pretrained 는 빈 가중치(meta 장치)로 모델을 만든 뒤 채우는데, 그동안 다른 스레드에서
만든 torch 모듈(EasyOCR Reader)까지 meta 장치에 생겨 readtext 가 전부 실패한다
("Tensor on device meta is not on the expected device cpu!", 컨테이너 transformers 4.57 에서 재현).
CLIP·EasyOCR 로드는 이 잠금 안에서만 한다.
"""
import threading

MODEL_LOAD_LOCK = threading.Lock()
