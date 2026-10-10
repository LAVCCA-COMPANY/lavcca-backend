reports 파일에 이제까지 돌려본 결과들 있음

api 돌리면 로컬로 웹사이트에서 실행 가능, pdf 리포트 생성 가능

지금은 mvp 1_9_2.py 로 문장 분석 및 PRS CRS 판단

곧 copyright detector도 합쳐서 최종본 MVP 만들 예정

학습·갱신 스크립트는 전부 mvp 1_9_3/ 에 있음 (1_9_2 에서 이동)

mvp 1_9_3/controversy_samples.json을 이용해서 controversy_model.joblib를 classifier_model.py로 문장 학습시킴

trend_updater.py를 사용해서 최근 뉴스/기사 분석, 키워드, 학습문장을 찾고 review_updates.py로 검토 후 classifier_model.py로 재학습 (사례 수집은 case_db.json 폐기로 비활성, 유사 사례는 casedb_sync.py + similar_case_engine.py)

모델 성능 확인: eval_harness.py golden_set.json

현재 API 노출로 인해 다시 조금 수정해서 사용하고 싶으면 개인적으로 API 보내줘야됨

python 3.14로 실행