import json
import re
import joblib
import numpy as np
from pathlib import Path
from collections import Counter
from output_filter import soften_output_with_intensity

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.multiclass import OneVsRestClassifier
from sklearn.svm import LinearSVC
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MultiLabelBinarizer
from sklearn.metrics import classification_report, f1_score
from sklearn.calibration import CalibratedClassifierCV

# ─────────────────────────────────────────────
# 1. 전처리 및 데이터 저장 함수
# ─────────────────────────────────────────────

KO_STOPWORDS = {"이", "그", "저", "것", "들", "의", "를", "을", "에", "와", "과", "도"}

# L05(기만)·L10(저작권)은 별도 Copyright Detector가 담당 → 문장 분류 학습에서 제외.
# 데이터에 섞여 들어와도 여기서 자동으로 걸러 모델에 클래스가 생기지 않게 한다.
EXCLUDED_LABELS = {"L05", "L10"}

def normalize_text(text):
    text = re.sub(r'[^가-힣a-zA-Z0-9\s]', ' ', text)
    tokens = text.split()
    tokens = [t for t in tokens if t not in KO_STOPWORDS]
    return " ".join(tokens)

def save_to_json(file_path, new_text, labels):
    """입력받은 데이터를 JSON 파일에 추가 저장"""
    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        
        # 새로운 ID 생성
        new_id = max([item['id'] for item in data]) + 1 if data else 1
        
        # 데이터 추가
        data.append({
            "id": new_id,
            "text": new_text,
            "labels": labels
        })
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"✅ 데이터베이스에 저장됨 (ID: {new_id})")
    except Exception as e:
        print(f"❌ 저장 실패: {e}")

# ─────────────────────────────────────────────
# 2. 학습 함수
# ─────────────────────────────────────────────

def train(data_path, save_path):
    print(f"📦 데이터 로딩: {data_path}")
    with open(data_path, 'r', encoding='utf-8-sig') as f:
        raw_data = json.load(f)

    # 안전장치: 제외 라벨(L05/L10) 제거 후, 라벨이 비게 된 샘플은 학습에서 드롭
    cleaned = []
    for item in raw_data:
        labs = [l for l in item.get('labels', []) if l not in EXCLUDED_LABELS]
        if labs:
            cleaned.append({**item, 'labels': labs})
    if len(cleaned) != len(raw_data):
        print(f"🧹 제외 라벨 정리: {len(raw_data)} → {len(cleaned)}개 (L05/L10 단독 샘플 드롭)")
    raw_data = cleaned

    texts = [normalize_text(item['text']) for item in raw_data]
    labels = [item['labels'] for item in raw_data]

    # 1. 모든 라벨의 빈도수 계산
    all_labels = [l for sublist in labels for l in sublist]
    label_counts = Counter(all_labels)
    
    # 2. 데이터가 너무 적은 라벨 필터링 (최소 2개 이상 필요, 권장 5개)
    MIN_SAMPLES = 2
    valid_labels = [l for l, count in label_counts.items() if count >= MIN_SAMPLES]
    
    print(f"📊 총 라벨 수: {len(label_counts)}")
    print(f"⚠️ 학습 제외 라벨 (데이터 부족): {[l for l in label_counts if l not in valid_labels]}")

    # 3. 유효한 라벨만 데이터에 남기기
    filtered_labels = []
    for l_list in labels:
        filtered = [l for l in l_list if l in valid_labels]
        # 만약 필터링 후 라벨이 하나도 없다면 'L12'(해당없음) 처리
        filtered_labels.append(filtered if filtered else ["L12"])

    mlb = MultiLabelBinarizer()
    y = mlb.fit_transform(filtered_labels)

    # 4. 모델 설정 (데이터가 적을 때는 cv 숫자를 줄여야 함)
    # 데이터가 아주 적을 때는 cv='prefit'을 쓰거나 숫자를 2~3으로 낮춤
    cv_folds = 2 if min(label_counts.values()) < 5 else 5

    pipeline = Pipeline([
        ('tfidf', TfidfVectorizer(ngram_range=(1, 2), min_df=1)), # min_df를 1로 낮춰서 적은 데이터도 수용
        ('clf', OneVsRestClassifier(CalibratedClassifierCV(LinearSVC(dual=False, C=1.0), cv=cv_folds)))
    ])

    print(f"🚀 모델 학습 시작 (CV Folds: {cv_folds})...")
    pipeline.fit(texts, y)

    model_data = {
        'model': pipeline,
        'mlb': mlb,
        'label_names': {
            'L01': '직접적 욕설', 'L02': '인신공격/비하', 'L03': '혐오 표현',
            'L04': '허위 정보', 'L06': '위험/자해 행동',
            'L07': '정치적 편향', 'L08': '사생활 침해', 'L09': '성적 불쾌감',
            'L11': '피해자 조롱', 'L12': '해당 없음'
            # L05(기만)·L10(저작권)은 별도 Copyright Detector 담당 → 제외
        }
    }
    joblib.dump(model_data, save_path)
    print(f"✅ 모델 저장 완료: {save_path}")
    return pipeline, mlb

def predict(model, mlb, text, threshold=0.3):
    text_norm = normalize_text(text)
    probs = model.predict_proba([text_norm])[0]
    
    results = []
    for i, label_code in enumerate(mlb.classes_):
        if probs[i] >= threshold:
            results.append({
                'code': label_code,
                'probability': float(probs[i])
            })
    
    results.sort(key=lambda x: -x['probability'])
    return results if results else [{'code': 'L12', 'probability': 0.0}]

# ─────────────────────────────────────────────
# 3. 실행 및 인터랙티브 테스트
# ─────────────────────────────────────────────

if __name__ == '__main__':
    # 파일 경로 설정
    BASE_DIR = Path(__file__).parent
    DATA_PATH = BASE_DIR / 'controversy_samples.json'
    SAVE_PATH = BASE_DIR / 'controversy_model.joblib'
    
    # 1. 초기 학습
    model, mlb = train(DATA_PATH, SAVE_PATH)
    
    print("\n" + "=" * 60)
    print("🔥 실시간 논란 분류기 테스트 및 데이터 수집 모드")
    print(" - 문장을 입력하면 모델이 분석합니다.")
    print(" - 결과가 맞으면 'y', 틀리면 직접 라벨(예: L01,L02)을 입력하세요.")
    print(" - 종료하려면 'q' 또는 'exit'를 입력하세요.")
    print("=" * 60)

    while True:
        user_input = input("\n[분석 문장 입력] > ").strip()
        
        if user_input.lower() in ['q', 'exit', 'quit']:
            print("프로그램을 종료합니다.")
            break
        
        if not user_input:
            continue

        # 모델 추론
        predictions = predict(model, mlb, user_input)

        predicted_codes = [res['code'] for res in predictions]

        for res in predictions:
            raw_msg = f"{user_input}은 {res['code']} 리스크입니다."

            safe_msg = soften_output_with_intensity(raw_msg, res['probability'])
            print(f"Final Report: {safe_msg}")
        
        print(f"\n🔍 분석 결과:")
        for res in predictions:
            label_name = {
                'L01': '직접적 욕설', 'L02': '인신공격/비하', 'L03': '혐오 표현',
                'L04': '허위 정보', 'L06': '위험/자해 행동',
                'L07': '정치적 편향', 'L08': '사생활 침해', 'L09': '성적 불쾌감',
                'L11': '피해자 조롱', 'L12': '해당 없음'
            }.get(res['code'], '알 수 없음')
            print(f" - [{res['code']}] {label_name}: {res['probability']:.1%}")

        # DB 저장 여부 확인
        prompt_msg = f"\n저장할까요? (맞으면 'y' / 직접 입력(예: L01) / 취소는 엔터): "
        save_choice = input(prompt_msg).strip().lower()
        
        if save_choice == 'y':
            # 모델이 예측한 라벨 그대로 저장
            save_to_json(DATA_PATH, user_input, predicted_codes)
        elif save_choice:
            # 사용자가 직접 입력한 라벨 저장
            selected_labels = [label.strip().upper() for label in save_choice.split(',')]
            save_to_json(DATA_PATH, user_input, selected_labels)
        else:
            print("⏭️ 저장을 건너뛰었습니다.")
            continue
            
        # 데이터가 추가되었으므로 모델 재학습 여부 묻기
        retrain = input("데이터가 추가되었습니다. 모델을 다시 학습할까요? (y/n): ").strip().lower()
        if retrain == 'y':
            model, mlb = train(DATA_PATH, SAVE_PATH)