# copyright_detector — 영상/이미지 저작권 탐지 설계 요약
> Claude Code 컨텍스트용 압축 문서

---

## 프로젝트 목적
업로드 **전** 내 영상에 타인의 클립/이미지/음악이 포함되어 있는지 자동 검사.
외부 DB 구축 불필요 — 영상에서 단서를 추출해 실시간으로 원본을 역추적하는 방식.

---

## 현재 스택 (기존 구현)
- Python 3.13, Windows/PowerShell, AWS (Rekognition 비활성)
- ACRCloud — 음악 탐지 (오디오 지문)
- Google Cloud Vision API
- YouTube Data API v3
- SQLite (로컬 캐시)
- yt-dlp (테스트 영상 다운로드)

---

## 탐지 파이프라인 설계

```
영상 입력
  ├─ 오디오 추출 (ffmpeg)
  │     └─ ACRCloud → 음악/OST 탐지          ← 이미 구현됨
  │
  ├─ 키프레임 추출 (장면 전환 기반)
  │     ├─ Google Vision Web Detection → 이미지 출처 역검색
  │     └─ OCR → 텍스트 추출 → YouTube Search → 클립 출처 추적
  │
  └─ 결과 통합 → 타임스탬프 + 리스크 등급 + 출처 URL
```

---

## 구성 요소별 상세

### 1. 프레임 샘플링
```python
# 장면 전환 감지 방식 (고정 간격보다 효율적)
strategy = "scene_change"
threshold = 30.0  # absdiff mean
# 연속 유사 프레임은 첫 프레임만 처리 (중복 제거)
```

### 2. 퍼셉추얼 해싱 (1차 필터, 빠름)
```python
import imagehash
# pHash — 색조/밝기 변환에 강인 (일반 이미지)
# dHash — 크롭/패딩 변화에 강인 (사진)
# 해시 거리 임계값: <5 HIGH, 5~15 MEDIUM, >15 LOW
```

### 3. Google Vision Web Detection (2차, 정밀)
```python
response = client.web_detection(image=image)
# full_matching_images → HIGH 위험
# partial_matching_images → MEDIUM 위험
# best_guess_labels → 콘텐츠 유형 파악
# 비용 절감: pHash 1차 필터 통과한 것만 API 호출
```

### 4. OCR → YouTube 역추적
```python
# Vision API로 프레임 내 텍스트 추출
# (워터마크, 채널명, 게임 UI 텍스트 등)
# → YouTube Data API v3 검색 쿼리로 사용
# → 후보 영상 메타데이터(길이, 채널, 날짜)로 교차검증
```

### 5. ACRCloud 확장 (영상 클립 오디오)
```python
# ffmpeg으로 영상에서 오디오 트랙 추출
# 기존 ACRCloud 파이프라인에 동일하게 통과
# 영화 클립의 BGM/대사도 탐지 가능
subprocess.run(['ffmpeg', '-i', video, '-vn', '-ar', '44100', '-ac', '2', audio_out])
```

---

## 탐지 커버리지

| 콘텐츠 유형 | 방법 | 탐지율 |
|---|---|---|
| 배경음악 | ACRCloud | 높음 |
| 영화/드라마 OST | ACRCloud (오디오 추출) | 높음 |
| 유명 사진/이미지 | Vision Web Detection | 중간 |
| 영화 클립 (오디오 있음) | ACRCloud | 중간 |
| 유튜버 클립 (워터마크 있음) | OCR + YouTube 검색 | 중간 |
| 유튜버 클립 (워터마크 없음) | ❌ | 낮음 |
| 화면 재촬영(캠) | ❌ | 매우 낮음 |

---

## 리스크 등급 기준

| 등급 | 조건 |
|---|---|
| 🔴 HIGH | pHash 거리 < 5 또는 Vision 완전 일치 |
| 🟡 MEDIUM | pHash 거리 5~15 또는 부분 일치 |
| 🟢 LOW | 거리 > 15, 매칭 없음 |

---

## 성능 최적화

```python
# 1. pHash로 1차 필터 → API는 의심 프레임만
# 2. ThreadPoolExecutor로 API 호출 병렬화 (max_workers=4)
# 3. SQLite에 해시 캐시 → 재분석 방지
# 4. 연속 유사 프레임 중복 제거
```

---

## 다음 구현 우선순위

1. **ffmpeg 오디오 추출 → ACRCloud 연결** (기존 파이프라인 확장, 공수 적음)
2. **Vision Web Detection 모듈 추가** (프레임 역검색)
3. **OCR → YouTube 검색 연결** (텍스트 기반 클립 추적)
4. 결과 통합 리포터 (타임스탬프 + 출처 + 리스크 등급)

---

## 한계 (설계 시 고려)
- 완벽한 탐지는 기술적으로 불가능 (YouTube Content ID도 동일)
- 변형(좌우반전, 필터, 무음)된 클립은 탐지율 급감
- 구글 미인덱싱 콘텐츠는 Vision으로 탐지 불가
- **목표: "명백한 무단 사용"을 효율적으로 잡는 것**
