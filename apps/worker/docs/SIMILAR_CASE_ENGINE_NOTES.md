# 유사 사례 엔진 v2 — 운영·인수인계 노트

NocoDB "사례집"(약 10만 건 리스크 실사례/기사)을 임베딩해 **하이브리드 검색**으로
유사 사례를 찾는 엔진. 기존 `case_db.json`(수작업 4필드) 방식을 대체한다.

이 문서는 (1) 구조·명령 요약, (2) **GPU PC에서 로컬 임베딩 모델로 전환하는 절차**,
(3) **다른 Claude Code 인스턴스가 해야 할 일 체크리스트**를 담는다.

---

## ⚠️ 현재 상태 (부분 빌드 — 2026-07-30)

전체 100,604건 중 **48,000건만 임베딩 완료**된 **부분 인덱스**로 가동 중이다.
나머지 52,604건은 **Gemini 프로젝트 월 지출 한도(spend cap)** 초과로 중단됐다(속도 제한 아님).
`casedb_manifest.json`의 `count=48000, partial=true, full_n=100604` 가 이 상태를 나타낸다.

**완성(나머지 임베딩) 방법 — 아래 중 하나로 한도를 풀고 재개:**
```bash
# (A) ai.studio/spend 에서 월 지출 상한을 올린 뒤, 또는
# (B) 월 한도가 리셋된 뒤(~매월 1일)
python similar_case_engine.py build     # embed_state(done=48000)에서 자동 재개 → 완성 시 count=100604
```
> 한도가 걸린 동안에는 **쿼리 임베딩도 실패**하여 유사 사례 검색이 동작하지 않는다(그 외 NATAM·
> 저작권 분석 등은 정상 진행 — `find_similar_cases`가 실패를 삼키고 빈 결과 반환).

**중단된 빌드를 '현재까지'로 부분 마감**(이미 수행됨, 재실행 불필요):
```bash
python similar_case_engine.py finalize   # embed_state의 done 만큼으로 매니페스트 마감(재개 정보는 보존)
```

---

## 1. 구성 파일

| 파일 | 역할 |
|------|------|
| `tools/casedb_sync.py` | NocoDB 공개 뷰 → 로컬 스냅샷 `casedb/casedb_news.jsonl` 동기화(재개 가능) |
| `similar_case_engine.py` | 임베딩 빌드 + 하이브리드 검색 엔진. 임베딩 백엔드 **교체 가능**(gemini/local) |
| `risk_trigger.py` | 위험 신호(NATAM A/B축 + 위험 문장) → 검색 트리거. 축↔코퍼스 리스크 매핑 |
| `mvp_ver_1_9_2.py` | `CrisisConsultantSystem` — 엔진이 여기에 연결됨(`self.sim_engine`) |
| `mvp_ver_1_9_3.py` | 오케스트레이터(활성 진입점). 텍스트/영상 경로에서 엔진 호출 |
| `casedb/` | 데이터·인덱스 산출물(아래) |

### casedb/ 산출물
| 파일 | 내용 |
|------|------|
| `casedb_news.jsonl` | NocoDB 원천 스냅샷(1행=1사례) |
| `casedb_meta.jsonl` | 인덱스 정렬된 사례 메타(표시용 + 키워드셋) |
| `casedb_embeddings.npy` | `(N, dim)` float32, **L2 정규화** |
| `casedb_manifest.json` | 백엔드/모델/차원/건수/해시/시각 |
| `casedb_embed_state.json` | 재개용 진행 상태(빌드 완료 시 자동 삭제) |
| `sync_state.json` | 동기화 진행 상태 |

> **FAISS 인덱스 파일은 저장하지 않는다.** Windows에서 `faiss.write_index`가 한글
> 경로(`바탕 화면`)를 못 열어 실패하기 때문. Flat 인덱스라 로드 시 `.npy`에서
> 인메모리로 재구성해도 10만 건에 1초 미만. (엔진 로드는 항상 npy → IndexFlatIP)

---

## 2. 명령 요약

```bash
# 1) 데이터 동기화 (있으면 이어받기)  →  casedb/casedb_news.jsonl
python tools/casedb_sync.py
python tools/casedb_sync.py --fresh        # 처음부터
python tools/casedb_sync.py --profile      # 품질 프로파일만

# 2) 인덱스 빌드 (현재 기본: Gemini 임베딩, 768차원, 8워커 병렬)
python similar_case_engine.py build            # 전체
python similar_case_engine.py build --limit 400 --test   # 소규모 검증(_test 태그)
python similar_case_engine.py info             # 매니페스트 확인

# 3) 검색 테스트
python similar_case_engine.py search "생방송 중 특정 성별 비하 발언 논란" --k 5
python similar_case_engine.py search "뒷광고 논란" --cat "광고주 비친화성 리스크" --kw "광고,협찬"
```

### 환경변수(.env 또는 셸)
| 변수 | 기본값 | 설명 |
|------|--------|------|
| `EMBED_BACKEND` | `gemini` | `gemini` \| `local` |
| `EMBED_MODEL` | `models/gemini-embedding-2` | Gemini 임베딩 모델 |
| `EMBED_DIM` | `768` | Gemini 출력 차원(MRL 축소) |
| `EMBED_BATCH` | `100` | 배치당 텍스트 수 |
| `EMBED_WORKERS` | `8` | 코퍼스 빌드 병렬도 |
| `LOCAL_EMBED_MODEL` | `intfloat/multilingual-e5-large` | 로컬 백엔드 모델 |
| `SIM_W_DENSE / SIM_W_KW / SIM_W_CAT` | `1.0 / 0.35 / 0.25` | 하이브리드 가중치 |
| `SIM_POOL` | `60` | dense 후보 풀 크기 |

---

## 3. 🖥️ GPU PC에서 **로컬 임베딩 모델로 전환**하기 (백엔드 #2)

현재 이 PC는 **GPU가 없어(CPU 전용)** Gemini API 임베딩을 쓴다. GPU가 있는 PC로
옮기면 로컬 모델로 **완전 오프라인·무료·무제한 재임베딩**이 가능하다. 코드 수정은
필요 없다 — 임베딩 계층이 백엔드 교체형으로 설계돼 있어 **환경변수만 바꾸면 된다.**

### 절차
```bash
# (0) GPU 확인
python -c "import torch; print('cuda', torch.cuda.is_available())"

# (1) 라이브러리 설치
pip install sentence-transformers

# (2) 데이터가 없으면 먼저 동기화
python tools/casedb_sync.py

# (3) 로컬 백엔드로 재임베딩 (인덱스 자동 재구성, 옛 npy 덮어씀)
#     한국어 강함 + 긴 문맥이면 BAAI/bge-m3(1024차원) 권장
EMBED_BACKEND=local LOCAL_EMBED_MODEL=BAAI/bge-m3 python similar_case_engine.py build
#     (또는 기본 multilingual-e5-large 그대로)
EMBED_BACKEND=local python similar_case_engine.py build

# (4) 검증
EMBED_BACKEND=local python similar_case_engine.py info      # embedder=local:... 확인
EMBED_BACKEND=local python similar_case_engine.py search "뒷광고 논란" --k 5

# (5) 엔진 사용 시에도 같은 백엔드로 (예: 셸/서비스 환경변수 고정)
#     .env 에 EMBED_BACKEND=local 추가하면 mvp_ver_1_9_3 실행도 로컬로 동작
```

> Windows PowerShell에서는 `EMBED_BACKEND=local ...` 대신
> `$env:EMBED_BACKEND="local"; python ...` 형태로 설정한다.

### 주의점(중요)
- **코퍼스와 쿼리는 반드시 같은 모델**을 써야 한다. 엔진이 매니페스트의 백엔드로
  쿼리 임베딩도 수행하므로, 빌드와 검색·서비스의 `EMBED_BACKEND`를 일치시킬 것.
- 로컬 모델은 **차원이 달라진다**(e5-large=1024, bge-m3=1024). npy/인덱스는 빌드 시
  자동으로 새 차원으로 재생성되므로 신경 쓸 필요 없음. 단 옛 Gemini npy는 덮어써짐.
- e5 계열은 `query:`/`passage:` 프리픽스가 필요한데 엔진이 자동 처리한다.
- CPU에서 로컬 대형 모델로 10만 건 임베딩은 매우 느리다(수 시간). **GPU에서만** 권장.

---

## 4. ✅ 다른 Claude Code 인스턴스가 할 일 (체크리스트)

이 저장소를 다른 PC(특히 GPU 보유)에서 이어받는 Claude Code가 수행할 작업:

1. **환경 점검**
   - `python -c "import torch; print(torch.cuda.is_available())"` → GPU 유무 확인
   - `faiss`, `numpy`, `google.genai`, (`sentence_transformers`) 설치 여부 확인
   - `.env`에 `GEMINI_API_KEY` 존재 확인(Gemini 백엔드 쓸 경우)

2. **데이터 준비**
   - `casedb/casedb_news.jsonl` 있는지 확인. 없거나 오래됐으면 `python tools/casedb_sync.py`
   - `python tools/casedb_sync.py --profile`로 건수·리스크 카테고리 확인(현재 10종, A축과 1:1)

3. **인덱스 빌드**
   - GPU 있음 → **로컬 백엔드 전환**(3장 절차). 모델은 `BAAI/bge-m3` 권장(한국어·롱컨텍스트)
   - GPU 없음 → 기존 Gemini 백엔드 유지(`python similar_case_engine.py build`)
   - 소규모(`--limit 400 --test`)로 먼저 파이프라인 검증 후 전체 빌드

4. **동작 검증**
   - `python similar_case_engine.py search "..."`로 검색 품질 확인
   - 엔진↔본체 연결 검증: 아래 스니펫으로 `find_similar_cases` 왕복 확인
     ```python
     import mvp_ver_1_9_2 as mvp
     s = mvp.CrisisConsultantSystem('case_db.json','config/rules.yaml','config/controversy_labels.yaml',
                                    'config/worst_actions_map.yaml','config/report_template_v1_9_3.md','reports')
     natam = {'A':{'A-02':{'level':'DANGER','reason':'성별 비하'}},'B':{}}
     print(s.find_similar_cases(base_text='성별 비하 논란', natam_result=natam, make_summary=False)[0])
     ```

5. **하지 말 것 / 알아둘 것**
   - `faiss.write_index`/`read_index`를 다시 도입하지 말 것(한글 경로 버그). npy 재구성 유지.
   - 빌드/검색/서비스의 `EMBED_BACKEND`를 항상 일치시킬 것.
   - 트리거 매핑(`risk_trigger.py`의 `AXIS_TO_CORPUS_RISK`)은 코퍼스 `리스크` 10종이
     NATAM A축과 1:1이라는 사실에 기반. 코퍼스 카테고리가 바뀌면 이 표를 갱신할 것.

---

## 5. 검색 방식(하이브리드) 요약

1. **dense**: 트리거 질의문을 임베딩 → FAISS(IndexFlatIP, 정규화=코사인) 상위 `SIM_POOL`개
2. **keyword**: 트리거 키워드가 사례 텍스트(제목/핵심문장/키워드/요약)에 등장하는 비율
3. **category**: 사례 `리스크`가 트리거의 리스크 카테고리와 일치하면 보정
4. **fuse**: `W_DENSE·dense + W_KW·kw + W_CAT·cat` 로 재정렬 → 상위 k

트리거는 **입력 전체가 아니라** NATAM에서 실제 발화된(ALERT↑) A/B축 항목의 이름·근거
+ 위험 문장 + 분류 라벨을 모아 구성한다(`risk_trigger.build_trigger`). A축은 코퍼스
리스크와 1:1 직접 매핑, B축(플랫폼)은 근접 매핑.

> 정확도를 더 높이려면(선택) 상위 후보에 LLM 재정렬을 얹을 수 있으나, 현재는 사용자
> 선택에 따라 **하이브리드까지만** 적용(하이브리드+LLM 재정렬은 미적용).

---

## 6. 데이터 갱신(사례집이 늘어났을 때)

```bash
python tools/casedb_sync.py            # 새 행 이어받기(또는 --fresh 로 전량 재동기화)
python similar_case_engine.py build   # 재임베딩(재개 가능)
```
매니페스트의 `source_hash`로 스냅샷 변경을 대략 감지할 수 있다. 대량 갱신 시 `--fresh`
동기화 후 전체 재빌드를 권장.
