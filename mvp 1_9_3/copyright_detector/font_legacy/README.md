# font_legacy — 폰트 감지 (서비스에서 제외, 재개발 예정)

영상 자막에서 상업용 폰트를 찾던 코드. 정확도가 낮아(평범한 산세리프 구분 불가, 오탐 다수)
`enable_font_analysis=False` 로 꺼져 있었고, 2026-10-14 파이프라인·Docker 이미지에서 빼 이 폴더로 옮겼다.
SageMaker 등으로 새로 만들 때 참고용이며, 이 폴더의 코드는 지금 위치에서는 import 경로가 맞지 않아 그대로 돌지 않는다.

| 파일 | 원래 위치 |
|---|---|
| `font_analyzer.py` | `analyzers/` — 파이프라인 분석기(OCR → 폰트명/스타일 → 상업용 위험도) |
| `font_classifier.py` | `utils/` — CNN 폰트 분류기 추론 |
| `train_font_classifier.py` | `tools/` — 분류기 학습 |
| `fonts_train/` | 학습용 폰트 폴더(폰트 파일은 커밋 안 함) |

남겨 둔 것: 결과 형식의 `findings_by_type.font`(지금은 항상 빈 목록), 점수 가중치 `font`,
학습 DB 의 `KnownFont`/`known_fonts` 테이블과 DB 관리 화면의 폰트 탭. 새 감지기가 같은 자리에 결과를 넣으면 된다.
