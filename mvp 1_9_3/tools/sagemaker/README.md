# tools/sagemaker — 논란 분류기 학습 (로컬 / SageMaker 공통)

SageMaker 사전 연습 1단계. `train.py` 하나가 로컬과 SageMaker script mode에서 똑같이 돈다.
이 폴더의 코드는 AWS를 호출하지 않는다(boto3·sagemaker SDK 없음).

## 실행 (mvp 1_9_3/ 에서)

```bash
python tools/sagemaker/train.py            # 홀드아웃 평가만 → out/metrics.json
python tools/sagemaker/train.py --final    # + 전체 데이터 최종 모델 → out/controversy_model.joblib
```

연습용 의존성은 `tools/sagemaker/requirements.txt` (서비스 `requirements.txt`와 분리).

## 입출력 경로 규칙

| 무엇 | 환경변수 (SageMaker가 설정) | 없을 때 (로컬) | 인자로 덮어쓰기 |
|---|---|---|---|
| 학습 데이터 폴더 | `SM_CHANNEL_TRAIN` (`/opt/ml/input/data/train`) | `mvp 1_9_3/data/` | `--data-dir` |
| 모델·지표 저장 폴더 | `SM_MODEL_DIR` (`/opt/ml/model`) | `mvp 1_9_3/out/` | `--model-dir` |

- 데이터 폴더에서 `controversy_samples.json` 한 파일만 읽는다. `golden_set*.json`은 평가 전용이라 학습에 쓰지 않는다.
- `models/`(서비스 모델 폴더)를 저장 위치로 주면 바로 종료한다. 서비스 모델은 이 스크립트로 덮어쓰지 않는다.
- `out/`은 `.gitignore`·`.dockerignore`에 들어 있다.

### S3 데이터 위치 (데이터레이크, 제안 — PM 확인 전)

| 무엇 | 위치 |
|---|---|
| 학습 데이터 | `s3://lavcca-dev-datalake/datasets/controversy/<버전>/controversy_samples.json` |
| 평가 골든셋 | `s3://lavcca-dev-datalake/datasets/golden/<버전>/golden_set.json` |

SageMaker 학습 작업의 `train` 채널에 `datasets/controversy/<버전>/` 폴더를 주면 `SM_CHANNEL_TRAIN` 에
그대로 내려오므로 `train.py` 는 고칠 것이 없다. `<버전>` 은 날짜(`2026-10-19`) 같은 고정 이름으로 두고 덮어쓰지 않는다.
고객 분석 결과(`ai-output/`)는 동의·익명화가 정해지기 전에는 학습 데이터로 옮기지 않는다.

SageMaker 방식 흉내 (로컬, AWS 없음):

```bash
SM_CHANNEL_TRAIN=/tmp/sm/train SM_MODEL_DIR=/tmp/sm/model python tools/sagemaker/train.py --final True
```

## 하이퍼파라미터

| 인자 | 기본 | 뜻 |
|---|---|---|
| `--seed` | 42 | 홀드아웃 분할 시드 |
| `--holdout` | 0.2 | 검증 비율. 0이면 평가 생략 |
| `--threshold` | 0.15 | Tier1 트리거 임계값 (`TIER1_TRIGGER_THRESHOLD`) |
| `--display-threshold` | 0.3 | 라벨별 P/R/F1 판정 임계값 (`eval_harness.py`와 같음) |
| `--final` | 꺼짐 | 전체 데이터로 최종 모델 저장. `--final`, `--final True` 둘 다 됨 |
| `--ngram-min` / `--ngram-max` | 2 / 4 | `TfidfVectorizer(char_wb)` n-gram 범위 |
| `--C` | 1.0 | `LinearSVC` C |

SageMaker는 하이퍼파라미터를 `--이름 값` 문자열로 넘기므로 bool도 `True`/`False` 문자열을 받는다. 모르는 인자는 경고만 하고 무시한다.

## 학습 로직

`classifier_model.prepare_training_data`(제외 라벨 L05/L10, `MIN_SAMPLES=2`, 빈 라벨 → L12, cv_folds)와
`classifier_model.build_pipeline`(char_wb TF-IDF + OneVsRest(CalibratedClassifierCV(LinearSVC)))을 그대로 쓴다.
`python classifier_model.py`도 같은 함수를 쓰므로 둘의 학습 결과는 같다. 저장 형식도 같다: `{'model', 'mlb', 'label_names'}`.

## 출력

표준출력 (SageMaker `metric_definitions` 정규식용, 한 줄씩):

```
val_macro_f1=0.7427
val_trigger_recall=0.9437
```

- `val_macro_f1`: 검증 세트에 실제로 나온 위험 라벨(L12 제외)의 F1 평균, 판정 임계값 `--display-threshold`.
- `val_trigger_recall`: 위험 라벨이 있는 검증 문장 중 `--threshold`에서 위험 라벨이 하나라도 맞게 예측된 비율 (`eval_harness.py`의 `tier1_trigger_recall`과 같은 정의).

`metrics.json`: 파라미터, 데이터 sha256, 라이브러리 버전, 홀드아웃 라벨별 지표, 최종 모델 클래스.

519문장 데이터라 지표는 분할에 따라 크게 흔들린다. 숫자 비교보다 재현·동등성 확인이 목적이다.
홀드아웃 지표는 데이터 자체 라벨 기준이라, Tier2 silver 기준인 `eval_harness.py` 수치와 직접 비교하면 안 된다.

## SageMaker로 올릴 때 (3단계, 실행 미확인)

- `train.py`는 `mvp 1_9_3/` 루트의 `classifier_model.py`, `output_filter.py`를 import한다.
  그래서 `source_dir`은 `tools/sagemaker/`가 아니라 이 모듈들이 들어간 폴더여야 하고, `entry_point`는 그 안의 `tools/sagemaker/train.py`가 된다.
  `mvp 1_9_3/` 전체를 올리면 모델·영상 등 불필요한 파일까지 올라가므로, 필요한 파일만 담은 임시 폴더를 만드는 쪽을 검토.
- SageMaker 프레임워크 컨테이너는 `source_dir` 루트의 `requirements.txt`를 설치한다(실행 미확인). 이 폴더의 `requirements.txt`를 그 위치로 복사해야 한다.
- scikit-learn 프레임워크 컨테이너가 1.8.0을 제공하는지는 확인 안 함. 다르면 `requirements.txt`로 1.8.0을 설치하거나, 서비스 쪽 버전과 맞춰야 joblib 로드가 안전하다.
- 런처 코드(Estimator 등)는 SDK 버전마다 API가 달라 현재 공식 문서를 기준으로 작성한다.
