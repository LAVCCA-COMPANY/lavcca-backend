# 📋 크리에이터 위기 관리 + 저작권 침해 통합 분석 보고서

---

> **보고서 ID** : `INC-{INCIDENT_ID}`
> **분석 일시** : `{ANALYZED_AT}`
> **대상 영상** : `{VIDEO_FILENAME}`
> **보고서 버전** : v1.9.3 (NATAM 위기관리 + copyright_detector 저작권 침해 통합)

---

## 1. 사건 요약 (Incident Summary)

### 1-1. 기본 정보

| 항목 | 내용 |
|------|------|
| 사건명 | {INCIDENT_TITLE} |
| 영상 길이 | {DURATION_SEC}초 |
| 전사 세그먼트 수 | {TOTAL_SEGMENTS}개 |
| Gemini 교정 횟수 | {TOTAL_CORRECTED}개 |
| 주요 논란 유형 | {PRIMARY_LABEL} |
| 확산 단계 | {SPREAD_STAGE} |
| 분석 생성 시각 | {ANALYZED_AT} |
| 분석 소요 시간 | {ELAPSED_SEC}초 |

### 1-2. 사건 개요

{INCIDENT_SUMMARY_TEXT}

> 위 내용은 입력된 사건 설명 및 자막 분석을 바탕으로 자동 생성되었습니다.

### 1-3. 자막에서 감지된 주요 발언

| # | 타임스탬프 | 발언 내용 | 논란 라벨 | 판단 근거 |
|---|-----------|-----------|-----------|-----------|
{TRANSCRIPT_ROWS}

---

## 2. 위험 분석 (Risk Analysis)

### 2-1. 논란 유형 분류

**주요 유형 : `{PRIMARY_LABEL}`**

| 감지된 라벨 | 설명 |
|-------------|------|
| {LABEL_1} | {LABEL_1_DESC} |
| {LABEL_2} | {LABEL_2_DESC} |

**판단 근거**

> {CLASSIFICATION_REASON}

### 2-2. 룰 엔진 스캔 결과

```
상태     : {RULE_HIT_STATUS}        ← "🚨 키워드 적발" 또는 "✅ 즉각 위험 없음"
정책     : {RULE_POLICY}
심각도   : {RULE_SEVERITY}          ← CRITICAL / HIGH / MEDIUM / LOW
적발 단어: {RULE_MATCHED_WORD}
권고 조치: {RULE_ACTION}
```

### 2-3. 영상 키프레임 분석

| 시점 | 관찰 요소 |
|------|-----------|
| Point 1 ({KEYFRAME_1_TIMESTAMP}) | {KEYFRAME_1_TAG} |
| Point 2 ({KEYFRAME_2_TIMESTAMP}) | {KEYFRAME_2_TAG} |
| Point 3 ({KEYFRAME_3_TIMESTAMP}) | {KEYFRAME_3_TAG} |

> 키프레임 분석은 객관적 시각 요소(자막, 행동, 구도)만 기술하며 위험도 판단을 포함하지 않습니다.

### 2-4. 종합 위험 신호

- 직접 발화 위험 : {DIRECT_SPEECH_RISK}         ← 자막 L01~L03 감지 여부
- 영상 시각 위험 : {VISUAL_RISK}                 ← 키프레임 이상 요소 여부
- 키워드 룰 위험 : {RULE_RISK}                   ← 룰 엔진 적발 여부
- 외부 확산 신호 : {EXTERNAL_SIGNAL}             ← 외부 링크/커뮤니티 유입 여부

### 2-5. 검증 필요 주장 (L04 사실확인 — Stage 1)

> 🔎 **검증이 필요한 사실 주장 {CLAIM_CHECK_COUNT}건**이 감지되었습니다.
> ⚠️ 아래 항목은 **'검증이 필요한 주장'을 식별한 것**이며, **거짓으로 판정한 것이 아닙니다.**
> 진위 확인은 사람 검토 또는 별도 팩트체크 단계가 필요합니다.

| # | 타임스탬프 | 검증 대상 주장 | 도메인 / 검증가치 | 검증 필요 이유 |
|---|-----------|----------------|-------------------|----------------|
{CLAIM_CHECK_ROWS}

---

## 3. 확산 단계 판정 (Spread Stage Assessment)

### 3-1. 현재 단계

```
╔══════════════════════════════════════════════════════╗
║                                                      ║
║   Early  ──────►  Mid  ──────►  Late                 ║
{SPREAD_ARROW_LINE}
║  [ 현재 위치: {SPREAD_STAGE} ]                        ║
║                                                      ║
╚══════════════════════════════════════════════════════╝
```

> **{SPREAD_STAGE}** 단계: {SPREAD_STAGE_DESCRIPTION}

### 3-2. 판정 근거

| 지표 | 수치 | 임계값 | 평가 |
|------|------|--------|------|
{SPREAD_TABLE_ROWS}

**판정 사유**

{SPREAD_REASONS}

### 3-3. 단계별 위기 전망

| 단계 | 예상 시나리오 | 주요 징후 |
|------|--------------|-----------|
| Early (현재) | 소수 시청자 인지, 제한적 공유 | 조회수 완만한 상승, 댓글 소수 |
| Mid | 커뮤니티 확산, 미디어 관심 | 조회수 급등, 외부 링크 유입 증가 |
| Late | 대중 인지, 추가 확산 정체 | 조회수 정체, 기존 구독자 이탈 |

### 3-4. 외부 확산 신호 (2층 교차 확인)

> 유튜브 영상 지표만으로는 놓치는 **플랫폼 밖 확산**을 교차 확인합니다.
> **A층**은 크리에이터 전반 관심도(이름 검색), **B층**은 이번 사안이 실제로 번졌는지
> (이름 + 사건 키워드 + 최신 시점)를 봅니다. 외부 신호는 확산 단계를 **올리기만** 합니다.

{SPREAD_EXTERNAL_BLOCK}

---

## 4. 최악의 행동 리스트 (Worst Actions to Avoid)

> ⚠️ 아래 행동들은 과거 유사 사례에서 위기를 **심화**시킨 것으로 관찰된 대응 패턴입니다.
> 이는 권고 또는 금지 사항이 아니며, 사례 데이터 기반의 객관적 관찰 결과입니다.

**적용 기준** : 논란 유형 `{DETECTED_TYPE}` / 확산 단계 `{SPREAD_STAGE}`

| 순위 | 금기 행동 | 예상되는 역효과 |
|------|-----------|----------------|
| 1 | {WORST_ACTION_1} | {WORST_ACTION_1_EFFECT} |
| 2 | {WORST_ACTION_2} | {WORST_ACTION_2_EFFECT} |
| 3 | {WORST_ACTION_3} | {WORST_ACTION_3_EFFECT} |
| 4 | {WORST_ACTION_4} | {WORST_ACTION_4_EFFECT} |

**과거 사례에서 관찰된 악화 트리거**

{TRIGGER_SUMMARY}

---

## 5. 유사 사례 분석 (Similar Case Analysis)

### 5-1. 검색된 유사 사례 Top 3 (유사도 거리 0.5 이하만)

{SIMILAR_CASES_BLOCK}

### 5-2. 공통 패턴 요약

**① 위기 확산의 공통 경로**

{PATTERN_SPREAD_PATH}

**② 대응 방식별 여론 반응 패턴**

{PATTERN_RESPONSE_REACTION}

**③ 위기 심화의 결정적 트리거**

{PATTERN_TRIGGER}

---

## 6. NATAM Risk Framework v2.0

> NATAM(Network Audience Tension & Algorithmic Mapping) 프레임워크 기반 리스크 평가입니다.
> 단계: 🟢 SAFE / 🔵 CARE / 🟡 ALERT / 🟠 DANGER / 🔴 CRITICAL
> **본 평가는 위험 신호 탐지 및 확산 가능성 예측이며, 법률 판단·도덕적 심판을 포함하지 않습니다.**
> 평가 상태: {NATAM_EVAL_STATUS}

### 6-1. A축 — 커뮤니티 리스크 (사건화·확산 가능성)

**종합 단계 : `{NATAM_OVERALL_A}`**

| 항목 ID | 항목명 | 단계 | 판단 근거 |
|---------|--------|------|-----------|
| A-01 | {NATAM_A_01_NAME} | {NATAM_A_01_LEVEL} | {NATAM_A_01_REASON} |
| A-02 | {NATAM_A_02_NAME} | {NATAM_A_02_LEVEL} | {NATAM_A_02_REASON} |
| A-03 | {NATAM_A_03_NAME} | {NATAM_A_03_LEVEL} | {NATAM_A_03_REASON} |
| A-04 | {NATAM_A_04_NAME} | {NATAM_A_04_LEVEL} | {NATAM_A_04_REASON} |
| A-05 | {NATAM_A_05_NAME} | {NATAM_A_05_LEVEL} | {NATAM_A_05_REASON} |
| A-06 | {NATAM_A_06_NAME} | {NATAM_A_06_LEVEL} | {NATAM_A_06_REASON} |
| A-07 | {NATAM_A_07_NAME} | {NATAM_A_07_LEVEL} | {NATAM_A_07_REASON} |
| A-08 | {NATAM_A_08_NAME} | {NATAM_A_08_LEVEL} | {NATAM_A_08_REASON} |
| A-09 | {NATAM_A_09_NAME} | {NATAM_A_09_LEVEL} | {NATAM_A_09_REASON} |
| A-10 | {NATAM_A_10_NAME} | {NATAM_A_10_LEVEL} | {NATAM_A_10_REASON} |

### 6-2. B축 — 플랫폼 리스크 (정책 충돌·광고 제한 가능성)

**종합 단계 : `{NATAM_OVERALL_B}`**

| 항목 ID | 항목명 | 단계 | 판단 근거 |
|---------|--------|------|-----------|
| B-01 | {NATAM_B_01_NAME} | {NATAM_B_01_LEVEL} | {NATAM_B_01_REASON} |
| B-02 | {NATAM_B_02_NAME} | {NATAM_B_02_LEVEL} | {NATAM_B_02_REASON} |
| B-03 | {NATAM_B_03_NAME} | {NATAM_B_03_LEVEL} | {NATAM_B_03_REASON} |
| B-04 | {NATAM_B_04_NAME} | {NATAM_B_04_LEVEL} | {NATAM_B_04_REASON} |
| B-05 | {NATAM_B_05_NAME} | {NATAM_B_05_LEVEL} | {NATAM_B_05_REASON} |

---

## 7. 추천 대응 행동 (Recommended Response Actions)

> 아래는 유사 사례 데이터를 바탕으로 도출된 대응 방향입니다.
> 실제 대응은 법률·PR 전문가와 함께 검토하는 것을 권장합니다.

### 7-1. 단계별 대응 타임라인

```
[즉시 — 24시간 이내]
  □ {ACTION_IMMEDIATE_1}
  □ {ACTION_IMMEDIATE_2}

[단기 — 3일 이내]
  □ {ACTION_SHORT_1}
  □ {ACTION_SHORT_2}

[중기 — 1주일 이내]
  □ {ACTION_MID_1}
  □ {ACTION_MID_2}
```

### 7-2. 대응 우선순위 매트릭스

| 우선순위 | 행동 | 예상 효과 | 리스크 |
|----------|------|-----------|--------|
| 🔴 높음 | {PRIORITY_HIGH_ACTION} | {PRIORITY_HIGH_EFFECT} | {PRIORITY_HIGH_RISK} |
| 🟡 중간 | {PRIORITY_MID_ACTION} | {PRIORITY_MID_EFFECT} | {PRIORITY_MID_RISK} |
| 🟢 낮음 | {PRIORITY_LOW_ACTION} | {PRIORITY_LOW_EFFECT} | {PRIORITY_LOW_RISK} |

### 7-3. 유사 사례에서 관찰된 효과적 대응 패턴

{EFFECTIVE_RESPONSE_PATTERNS}

### 7-4. 모니터링 체크리스트

```
커뮤니티 모니터링
  □ 주요 온라인 커뮤니티 언급량 추적
  □ 관련 해시태그 및 키워드 알림 설정

콘텐츠 관리
  □ 문제 영상/발언 구간 내부 검토
  □ 댓글 섹션 상태 점검

지표 추적
  □ 조회수 변화율 (6시간 단위)
  □ 구독자 증감 추이
  □ 외부 링크 유입 경로 확인
```

<!-- COPYRIGHT_SECTION_START -->
---

## 8. 저작권 침해 분석 (Copyright Infringement Analysis)

> 🎬 `copyright_detector` 자동 분석 결과입니다. 영상 내 음악·영상클립·이미지·로고·폰트의
> 저작권 침해 가능성과 유튜브 Content ID·수익화 영향을 예측합니다.
> **본 분석은 자동 탐지 결과이며, 최종 저작권 판단은 권리자·법률 검토가 필요합니다.**
> {CR_ENGINE_STATUS}

**분석 상태 : {CR_STATUS}**

### 8-1. 종합 결과

| 항목 | 내용 |
|------|------|
| 전체 저작권 위험도 | {CR_OVERALL_EMOJI} {CR_OVERALL_LEVEL} — {CR_OVERALL_SCORE}% |
| 발견 항목 수 | {CR_TOTAL_ISSUES}건 |
| 🔴 HIGH | {CR_HIGH_COUNT}건 |
| 🟡 MEDIUM | {CR_MEDIUM_COUNT}건 |
| 분석 영상 길이 | {CR_VIDEO_DURATION} |

### 8-2. 유튜브 스튜디오 예측 (저작권 관점)

{CR_YOUTUBE_BLOCK}

### 8-3. 분류별 탐지 건수

| 유형 | 탐지 건수 |
|------|-----------|
{CR_BYTYPE_ROWS}

### 8-4. 위험 타임라인 (위험도 높은 순 · 상위 항목)

| 시점 | 유형 | 제목 / 출처 | 위험도 | 유튜브 예상 조치 |
|------|------|-------------|--------|------------------|
{CR_TIMELINE_ROWS}

> 🎵 음악·🎬 영상클립은 Content ID 자동 조치 대상이 될 가능성이 높고,
> 🖼️ 이미지·🏷️ 로고는 자동 조치 가능성이 낮습니다(권리자 수동 신고 시 조치 가능).

<!-- COPYRIGHT_SECTION_END -->
---

## 부록 (Appendix)

### A. 논란 라벨 정의

| 라벨 ID | 명칭 | 설명 |
|---------|------|------|
| L01 | 직접적 욕설 | 명시적 비속어·욕설 포함 발언 |
| L02 | 인신공격/비하 | 특정인·집단을 대상으로 한 비하 표현 |
| L03 | 혐오 표현 | 성별·인종·종교 등 기반 혐오 발언 |
| L04 | 허위 정보 | 사실과 다른 정보의 의도적·비의도적 유포 |
| L05 | 기만 행위 | 광고 미표기, 뒷광고 등 시청자 기만 |
| L06 | 위험/자해 행동 | 신체적 위험을 초래하거나 조장하는 콘텐츠 |
| L07 | 정치적 편향 | 특정 정치 성향의 일방적 주장 또는 선동 |
| L08 | 사생활 침해 | 동의 없는 개인정보·사생활 노출 |
| L09 | 성적 불쾌감 | 성적 발언·표현으로 인한 불쾌감 유발 |
| L10 | 저작권 위반 | 허가 없는 타인 저작물 사용 |
| L11 | 피해자 조롱 | 사건·사고 피해자를 대상으로 한 조롱 |
| L12 | 해당 없음 | 위 유형에 해당하지 않음 |

### B. 분석에 쓰인 모델

| 단계 | 모델 |
|------|------|
| 음성 받아쓰기 | {MODEL_ASR} |
| 자막 교정 | {MODEL_REFINE} |
| 위기 분석 | {MODEL_ANALYSIS} |
| 유사 사례 검색 | {MODEL_EMBED} |

*이번 분석에서 실제로 응답한 모델입니다. 기본 모델에 일시 장애가 있으면 대체 모델로 바뀔 수 있습니다.*

---

*본 보고서는 AI 기반 자동 분석 결과이며, 최종 판단은 전문가 검토를 거쳐야 합니다.*
*LAVCCA v1.9.3 — 위기관리 + 저작권 통합 자동 생성*