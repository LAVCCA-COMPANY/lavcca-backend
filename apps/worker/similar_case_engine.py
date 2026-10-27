# -*- coding: utf-8 -*-
"""
similar_case_engine.py — NATAM 유사 사례 검색 엔진 (v2, NocoDB 사례집 기반)

기존 case_db.json(수작업 4필드) 방식을 대체한다. NocoDB 사례집 스냅샷
(casedb/casedb_news.jsonl, 약 10만 건)을 임베딩해 FAISS 인덱스로 만들고,
'하이브리드' 방식(의미검색 + 키워드 + 리스크 카테고리)으로 유사 사례를 찾는다.

핵심 설계
  1) 임베딩 백엔드 교체 가능:  EMBED_BACKEND=gemini(기본) | local
     - gemini : 현재 PC(GPU 없음)용. 연산을 API로 넘겨 10만 건도 배치로 처리.
     - local  : 나중에 GPU PC에서 multilingual-e5 등으로 완전 오프라인 재임베딩.
     두 백엔드 모두 코퍼스/쿼리에 '같은' 모델을 쓰도록 강제한다.
  2) 재개(resume) 가능한 임베딩: 중간에 끊겨도 embed_state에서 이어서 진행.
  3) 하이브리드 검색: dense(코사인) 후보 풀 → 키워드 겹침 + 리스크 카테고리
     보정으로 재정렬. (LLM 재정렬은 상위 오케스트레이터에서 선택적으로 수행)

파일 산출물 (casedb/ 아래)
  casedb_news.jsonl        원천 스냅샷 (casedb_sync.py 가 생성)
  casedb_meta.jsonl        인덱스 정렬된 사례 메타(표시용 + 키워드셋)
  casedb_embeddings.npy    (N, dim) float32, L2 정규화
  casedb.faiss             FAISS IndexFlatIP (정규화 → 코사인)
  build_manifest.json      백엔드/모델/차원/건수/필드/해시/시각
  embed_state.json         재개용 진행 상태(빌드 완료 시 삭제)

CLI
  python similar_case_engine.py build            # 전체 빌드(스냅샷 필요)
  python similar_case_engine.py build --limit 200 --test   # 소규모 검증 빌드
  python similar_case_engine.py search "질의문"  # 검색 테스트
  python similar_case_engine.py info             # 매니페스트 출력
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from typing import Iterable, Optional

import numpy as np

# Windows 콘솔(cp949) 이모지/한글 출력 보호
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

# ════════════════════════════════════════════════════════════════
# 설정
# ════════════════════════════════════════════════════════════════
HERE     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "casedb")

EMBED_BACKEND = os.getenv("EMBED_BACKEND", "gemini").lower()   # "gemini" | "local"

# Gemini 임베딩(현 PC 기본). 모델명이 유효하지 않으면 아래 폴백을 순차 시도.
GEMINI_EMBED_MODEL = os.getenv("EMBED_MODEL", "models/gemini-embedding-2")
GEMINI_EMBED_FALLBACKS = [
    GEMINI_EMBED_MODEL,
    "models/gemini-embedding-2-preview",
    "gemini-embedding-2-preview",
    "gemini-embedding-001",
    "models/gemini-embedding-001",
    "models/text-embedding-004",
    "text-embedding-004",
]
# 차원 축소(gemini-embedding 계열은 MRL 지원). 파일 크기·검색 속도 절약.
GEMINI_EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
GEMINI_BATCH     = int(os.getenv("EMBED_BATCH", "100"))

# 로컬 백엔드(나중에 GPU PC). e5 계열은 'query:'/'passage:' 프리픽스 필요.
LOCAL_EMBED_MODEL = os.getenv("LOCAL_EMBED_MODEL", "intfloat/multilingual-e5-large")

# 임베딩에 사용할 사례 필드(순서 = 문서 내 서술 순서)
DOC_FIELDS = ["제목", "핵심 문장", "리스크", "세부 태그",
              "기사 요약", "리스크 포인트", "AI 학습 포인트", "키워드"]

# 검색 결과·표시에 쓰는 메타 필드
META_FIELDS = ["News ID", "제목", "핵심 문장", "리스크", "세부 태그",
               "기사 요약", "리스크 포인트", "관련 법 및 정책",
               "뉴스 링크", "언론사", "기사 작성일", "키워드"]

# 하이브리드 가중치 (dense 코사인 ~0.5-0.9, kw/cat 0-1 스케일)
W_DENSE = float(os.getenv("SIM_W_DENSE", "1.0"))
W_KW    = float(os.getenv("SIM_W_KW",    "0.35"))
W_CAT   = float(os.getenv("SIM_W_CAT",   "0.25"))
DENSE_POOL = int(os.getenv("SIM_POOL", "60"))   # dense 후보 풀 크기


def _paths(tag: str = "") -> dict:
    return {
        "jsonl":    os.path.join(DATA_DIR, "casedb_news.jsonl"),
        "meta":     os.path.join(DATA_DIR, f"casedb{tag}_meta.jsonl"),
        "emb":      os.path.join(DATA_DIR, f"casedb{tag}_embeddings.npy"),
        "faiss":    os.path.join(DATA_DIR, f"casedb{tag}.faiss"),
        "manifest": os.path.join(DATA_DIR, f"casedb{tag}_manifest.json"),
        "state":    os.path.join(DATA_DIR, f"casedb{tag}_embed_state.json"),
    }


# ════════════════════════════════════════════════════════════════
# 텍스트 유틸
# ════════════════════════════════════════════════════════════════
def _s(v) -> str:
    return (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v).strip())


def build_doc(row: dict) -> str:
    """사례 1건을 임베딩용 문서 문자열로 직렬화(라벨 포함, 빈 필드 생략)."""
    lines = []
    for f in DOC_FIELDS:
        val = _s(row.get(f))
        if val:
            lines.append(f"{f}: {val}")
    return "\n".join(lines)


def keyword_set(row: dict) -> list:
    """행에서 키워드 토큰 집합 추출(키워드 필드 + 세부 태그)."""
    toks = set()
    for f in ("키워드", "세부 태그"):
        raw = _s(row.get(f))
        for part in raw.replace("·", ",").replace("/", ",").split(","):
            t = part.strip().lower()
            if len(t) >= 2:
                toks.add(t)
    return sorted(toks)


def _row_text_blob(m: dict) -> str:
    """키워드 매칭용 결합 텍스트(소문자)."""
    return " ".join(_s(m.get(f)) for f in ("제목", "핵심 문장", "키워드", "세부 태그", "기사 요약")).lower()


_KW_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")


def keyword_tokens(text: str) -> set:
    """어절 단위 토큰. 부분문자열(예: '광고' ⊂ '광고추천문') 일치를 막는다."""
    return {tok.lower() for tok in _KW_TOKEN_RE.findall(text or "") if len(tok) >= 2}


# ════════════════════════════════════════════════════════════════
# 임베딩 백엔드
# ════════════════════════════════════════════════════════════════
class _RateLimiter:
    """스레드 안전 토큰 버킷. 분당 N개(=임베딩 텍스트 수)로 처리율을 제한.
    Gemini 임베딩 유료 티어 쿼터(모델당 분당 요청 수)를 넘지 않도록 게이트."""

    def __init__(self, per_minute: int, burst: Optional[int] = None):
        # 버스트(capacity)를 작게 두어 첫 1분 오버슈트를 방지:
        #   임의 60초 창 최대 ≈ capacity + per_minute 이므로 capacity를 낮춤.
        self.capacity = float(burst if burst else per_minute)
        self.tokens   = self.capacity
        self.rate     = per_minute / 60.0          # 초당 토큰
        self.last     = time.monotonic()
        self.lock     = threading.Lock()

    def acquire(self, n: int) -> None:
        n = float(max(1, n))
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            time.sleep(min(wait, 3.0))


def _parse_retry_delay(msg: str) -> Optional[float]:
    """429 에러 메시지에서 서버가 요구한 재시도 지연(초)을 추출."""
    for pat in (r"retry in ([\d.]+)s", r"'retryDelay':\s*'([\d.]+)s'", r"retryDelay['\"]?:\s*([\d.]+)"):
        m = re.search(pat, msg)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def _l2norm(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype="float32")
    if mat.ndim == 1:
        mat = mat[None, :]
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype("float32")


class BaseEmbedder:
    name = "base"
    _dim: Optional[int] = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            v = self._embed(["차원 확인용 텍스트"], is_query=False)
            self._dim = int(np.asarray(v).shape[1])
        return self._dim

    def embed_passages(self, texts: list) -> np.ndarray:
        return _l2norm(self._embed(list(texts), is_query=False))

    def embed_query(self, text: str) -> np.ndarray:
        return _l2norm(self._embed([text], is_query=True))

    def _embed(self, texts: list, is_query: bool) -> np.ndarray:
        raise NotImplementedError


class GeminiEmbedder(BaseEmbedder):
    """google-genai embed_content 기반. 배치 우선, 실패 시 단건 폴백."""

    def __init__(self, model: str = None, out_dim: int = GEMINI_EMBED_DIM, batch: int = GEMINI_BATCH):
        from google import genai
        try:
            from google.genai import types
            self._types = types
        except Exception:
            self._types = None
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY 가 .env 에 없습니다.")
        self._client = genai.Client(api_key=api_key)
        self.out_dim = out_dim
        self.batch   = batch
        self.workers = int(os.getenv("EMBED_WORKERS", "4"))   # 코퍼스 빌드 병렬도
        # 분당 텍스트 처리율 제한(Gemini 임베딩 유료 티어 쿼터=분당 3,000 → 여유 두고 2,700)
        self.rpm     = int(os.getenv("EMBED_RPM", "2700"))
        self.limiter = _RateLimiter(self.rpm, burst=max(batch, 200))
        self._batch_ok = True
        if model:
            self.model = model            # 알려진 모델(빌드 매니페스트) → 프로브(API 호출) 생략
        else:
            self.model = self._resolve_model(None)
        self.name = f"gemini:{self.model}:{out_dim}"

    def _resolve_model(self, model: Optional[str]) -> str:
        candidates = [model] if model else GEMINI_EMBED_FALLBACKS
        last = None
        for cand in candidates:
            if not cand:
                continue
            try:
                self._call(cand, ["연결 확인"], is_query=True)
                if cand != (model or GEMINI_EMBED_MODEL):
                    print(f"   ℹ️  임베딩 모델 폴백 사용: {cand}")
                return cand
            except Exception as e:  # noqa: BLE001
                last = e
                print(f"   ⚠️  모델 '{cand}' 사용 불가: {str(e)[:120]}")
        raise RuntimeError(f"사용 가능한 Gemini 임베딩 모델이 없습니다: {last}")

    def _config(self, is_query: bool):
        task = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        kw = {"task_type": task}
        if self.out_dim and self.out_dim > 0:
            kw["output_dimensionality"] = self.out_dim
        if self._types is not None:
            try:
                return self._types.EmbedContentConfig(**kw)
            except Exception:
                return None
        return kw  # dict 폴백(구버전 SDK)

    def _call(self, model: str, texts: list, is_query: bool) -> np.ndarray:
        cfg = self._config(is_query)
        kwargs = {"model": model, "contents": texts}
        if cfg is not None:
            kwargs["config"] = cfg
        self.limiter.acquire(len(texts))   # 쿼터 초과 방지(요청 직전 게이트)
        resp = self._client.models.embed_content(**kwargs)
        return np.array([e.values for e in resp.embeddings], dtype="float32")

    def _embed_one_batch(self, batch: list, is_query: bool, retries: int = 8) -> np.ndarray:
        """단일 배치 임베딩(재시도 포함). 429는 서버가 요구한 지연을 존중."""
        for attempt in range(retries):
            try:
                if self._batch_ok and len(batch) > 1:
                    return self._call(self.model, batch, is_query)
                return np.vstack([self._call(self.model, [t], is_query) for t in batch])
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                # 배치 미지원 SDK면 단건 모드로 강등(재시도 소모 없이 재진입)
                if self._batch_ok and len(batch) > 1 and (
                        "batch" in msg.lower() or "contents" in msg.lower() or "list" in msg.lower()):
                    print("   ↪️  배치 임베딩 미지원 → 단건 모드로 전환")
                    self._batch_ok = False
                    continue
                # 지출 한도 초과는 재시도로 풀리지 않음 → 즉시 중단(쿼리 지연 낭비 방지)
                if "spending cap" in msg.lower() or "spend cap" in msg.lower():
                    raise
                if attempt == retries - 1:
                    raise
                is_429 = ("429" in msg) or ("RESOURCE_EXHAUSTED" in msg)
                server = _parse_retry_delay(msg) if is_429 else None
                wait = min((server + 1.0) if server else min(2 ** attempt, 30), 60.0)
                tag  = "429 쿼터" if is_429 else "오류"
                print(f"   ⚠️  임베딩 재시도({attempt + 1}/{retries}, {tag}): {msg[:80]} → {wait:.1f}s")
                time.sleep(wait)

    def _embed(self, texts: list, is_query: bool, retries: int = 8) -> np.ndarray:
        # 쿼리·소량은 단순 순차
        if is_query or len(texts) <= self.batch:
            return self._embed_one_batch(texts, is_query, retries)
        # 코퍼스 빌드: 배치 단위로 나눠 스레드 병렬(순서 보존)
        batches = [texts[i:i + self.batch] for i in range(0, len(texts), self.batch)]
        results = [None] * len(batches)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(self._embed_one_batch, b, is_query, retries): idx
                    for idx, b in enumerate(batches)}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
        return np.vstack(results)


class LocalEmbedder(BaseEmbedder):
    """sentence-transformers 기반(나중에 GPU PC). 설치·모델 다운로드 필요."""

    def __init__(self, model: str = LOCAL_EMBED_MODEL):
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "local 백엔드는 sentence-transformers 설치가 필요합니다: "
                "pip install sentence-transformers  (자세한 건 docs/SIMILAR_CASE_ENGINE_NOTES.md)"
            ) from e
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
        print(f"   🔄 로컬 임베딩 모델 로딩: {model} (device={device})")
        self._model = SentenceTransformer(model, device=device)
        self.model = model
        self._is_e5 = "e5" in model.lower()
        self.name = f"local:{model}"

    def _embed(self, texts: list, is_query: bool) -> np.ndarray:
        if self._is_e5:
            prefix = "query: " if is_query else "passage: "
            texts = [prefix + t for t in texts]
        vecs = self._model.encode(
            texts, batch_size=64, convert_to_numpy=True,
            normalize_embeddings=False, show_progress_bar=False,
        )
        return np.asarray(vecs, dtype="float32")


def get_embedder(backend: str = None, model: str = None, out_dim: int = None) -> BaseEmbedder:
    backend = (backend or EMBED_BACKEND).lower()
    if backend == "local":
        return LocalEmbedder(model or LOCAL_EMBED_MODEL)
    return GeminiEmbedder(model=model, out_dim=out_dim if out_dim else GEMINI_EMBED_DIM)


# ════════════════════════════════════════════════════════════════
# 데이터 로딩 + 품질 필터
# ════════════════════════════════════════════════════════════════
def _iter_jsonl(path: str) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_and_filter(jsonl: str, limit: Optional[int] = None) -> list:
    """스냅샷 → 정제된 메타 리스트(인덱스 순서). 중복·빈 행 제거."""
    seen = set()
    rows = []
    dropped_empty = dropped_dup = 0
    for row in _iter_jsonl(jsonl):
        title = _s(row.get("제목"))
        core  = _s(row.get("핵심 문장"))
        summ  = _s(row.get("기사 요약"))
        kw    = _s(row.get("키워드"))
        # 완전 공허한 행 제거(제목·핵심문장·요약·키워드 모두 없음)
        if not (title or core or summ or kw):
            dropped_empty += 1
            continue
        key = (title, core, _s(row.get("리스크")))
        if key in seen:
            dropped_dup += 1
            continue
        seen.add(key)

        meta = {f: _s(row.get(f)) for f in META_FIELDS}
        meta["_doc"] = build_doc(row)
        meta["_kw"]  = keyword_set(row)
        rows.append(meta)
        if limit and len(rows) >= limit:
            break

    print(f"   🧹 필터: 유효 {len(rows):,} · 빈행제거 {dropped_empty:,} · 중복제거 {dropped_dup:,}")
    return rows


def _sha1_head(path: str, n_bytes: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(n_bytes))
    return h.hexdigest()[:12]


# ════════════════════════════════════════════════════════════════
# 빌드 (재개 가능한 임베딩 → FAISS)
# ════════════════════════════════════════════════════════════════
def build(limit: Optional[int] = None, tag: str = "", backend: str = None, chunk: int = 1000) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    P = _paths(tag)
    if not os.path.exists(P["jsonl"]):
        raise FileNotFoundError(f"스냅샷 없음: {P['jsonl']} — 먼저 tools/casedb_sync.py 실행")

    t0 = time.time()
    print(f"⏳ 유사사례 인덱스 빌드 시작 (backend={backend or EMBED_BACKEND}, limit={limit})")
    rows = load_and_filter(P["jsonl"], limit=limit)
    n = len(rows)
    if n == 0:
        raise RuntimeError("유효 사례가 0건입니다.")

    # 메타 저장(인덱스 정렬)
    with open(P["meta"], "w", encoding="utf-8") as f:
        for m in rows:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    embedder = get_embedder(backend)
    dim = embedder.dim
    print(f"   🧠 임베딩 백엔드: {embedder.name}  (dim={dim})")

    docs = [m["_doc"] for m in rows]

    # ── 재개 상태 ──
    state = {}
    if os.path.exists(P["state"]) and os.path.exists(P["emb"]):
        try:
            state = json.load(open(P["state"], encoding="utf-8"))
        except Exception:
            state = {}
    resume_ok = (
        state.get("n") == n and state.get("dim") == dim
        and state.get("embedder") == embedder.name
    )

    if resume_ok:
        arr = np.lib.format.open_memmap(P["emb"], mode="r+")
        start = int(state.get("done", 0))
        print(f"   ♻️  재개: {start:,}/{n:,} 부터 이어서 임베딩")
    else:
        arr = np.lib.format.open_memmap(P["emb"], mode="w+", dtype="float32", shape=(n, dim))
        start = 0

    for i in range(start, n, chunk):
        vecs = embedder.embed_passages(docs[i:i + chunk])
        arr[i:i + len(vecs)] = vecs
        arr.flush()
        done = i + len(vecs)
        json.dump({"n": n, "dim": dim, "embedder": embedder.name, "done": done},
                  open(P["state"], "w", encoding="utf-8"))
        print(f"   📈 임베딩 {done:,}/{n:,}  ({time.time() - t0:6.1f}s)")

    # FAISS 인덱스 파일은 저장하지 않는다.
    #   - faiss.write_index 는 Windows에서 한글 경로('바탕 화면')를 못 열어 실패.
    #   - Flat 인덱스라 로드 시 .npy 에서 인메모리로 재구성해도 100k에 <1s.
    arr.flush()

    manifest = {
        "built_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
        "embedder":   embedder.name,
        "backend":    (backend or EMBED_BACKEND),
        "dim":        dim,
        "count":      n,
        "doc_fields": DOC_FIELDS,
        "source":     os.path.basename(P["jsonl"]),
        "source_hash": _sha1_head(P["jsonl"]),
        "partial":    bool(limit),
    }
    json.dump(manifest, open(P["manifest"], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if os.path.exists(P["state"]):
        os.remove(P["state"])

    print(f"✅ 빌드 완료: {n:,}건 · dim={dim} · {time.time() - t0:.1f}s")
    print(f"   → {P['emb']}")
    print(f"   → {P['meta']}")
    print(f"   → {P['manifest']}")


def finalize_partial(tag: str = "") -> None:
    """중단된 빌드를 '현재까지 임베딩된 만큼'으로 마감해 사용 가능한 부분 인덱스로 만든다.
    embed_state 는 지우지 않으므로 나중에 `build` 로 나머지를 이어서 완성할 수 있다."""
    P = _paths(tag)
    if not (os.path.exists(P["state"]) and os.path.exists(P["emb"])):
        raise FileNotFoundError("진행 중 상태(embed_state)나 임베딩(.npy)이 없어 부분 마감 불가")
    state = json.load(open(P["state"], encoding="utf-8"))
    done  = int(state.get("done", 0))
    if done <= 0:
        raise RuntimeError("아직 완성된 임베딩이 없습니다.")
    embedder_name = state.get("embedder", "")
    manifest = {
        "built_at":    time.strftime("%Y-%m-%d %H:%M:%S"),
        "embedder":    embedder_name,
        "backend":     embedder_name.split(":", 1)[0] or EMBED_BACKEND,
        "dim":         int(state.get("dim", 0)),
        "count":       done,                       # 유효 임베딩 구간만 사용
        "full_n":      int(state.get("n", done)),  # 전체 목표(미완성 표시용)
        "doc_fields":  DOC_FIELDS,
        "source":      os.path.basename(P["jsonl"]),
        "source_hash": _sha1_head(P["jsonl"]) if os.path.exists(P["jsonl"]) else "",
        "partial":     True,
    }
    json.dump(manifest, open(P["manifest"], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"✅ 부분 인덱스 마감: {done:,}/{manifest['full_n']:,}건 사용 가능")
    print(f"   나중에 `python similar_case_engine.py build` 로 나머지를 이어서 완성할 수 있습니다.")


# ════════════════════════════════════════════════════════════════
# 검색 엔진
# ════════════════════════════════════════════════════════════════
class SimilarCaseEngine:
    """빌드된 인덱스를 로드해 하이브리드 유사 사례 검색을 제공."""

    def __init__(self, tag: str = "", backend: str = None, verbose: bool = True):
        import faiss
        self.P = _paths(tag)
        for key in ("emb", "meta", "manifest"):
            if not os.path.exists(self.P[key]):
                raise FileNotFoundError(
                    f"인덱스 파일 없음: {self.P[key]} — 먼저 `python similar_case_engine.py build` 실행"
                )
        self.manifest = json.load(open(self.P["manifest"], encoding="utf-8"))
        # 임베딩(.npy, 정규화됨) → 인메모리 IndexFlatIP 재구성 (한글 경로 FAISS 버그 회피)
        emb = np.load(self.P["emb"]).astype("float32")
        # 부분 빌드 지원: 매니페스트 count 만큼만(유효 임베딩 구간) 사용, 나머지 0벡터 제외
        count = int(self.manifest.get("count", emb.shape[0]))
        count = max(0, min(count, emb.shape[0]))
        emb = emb[:count]
        self.index = faiss.IndexFlatIP(emb.shape[1])
        self.index.add(np.ascontiguousarray(emb))
        all_meta  = [json.loads(l) for l in open(self.P["meta"], encoding="utf-8") if l.strip()]
        self.meta = all_meta[:count]
        self._blobs   = [_row_text_blob(m) for m in self.meta]
        # 임베더는 빌드 매니페스트의 백엔드/모델/차원으로 구성 → 로드 시 API 프로브 없음
        parts   = str(self.manifest.get("embedder", "")).split(":")
        m_back  = parts[0] if parts and parts[0] else self.manifest.get("backend")
        m_model = parts[1] if len(parts) > 1 else None
        m_dim   = int(self.manifest.get("dim", 0)) or None
        self.embedder = get_embedder(backend or m_back, model=m_model, out_dim=m_dim)
        if verbose:
            print(f"✅ 유사사례 엔진 로드: {self.manifest['count']:,}건 · {self.manifest['embedder']}")

    # ── 하이브리드 검색 ──
    def search(self, query_text: str, keywords=None, risk_categories=None,
               k: int = 5, pool: int = DENSE_POOL) -> list:
        qv = self.embedder.embed_query(query_text)
        pool = max(pool, k)
        sims, idxs = self.index.search(np.ascontiguousarray(qv), min(pool, len(self.meta)))

        qkw = set()
        for t in (keywords or []):
            t = _s(t).lower()
            if len(t) >= 2:
                qkw.add(t)
        cats = set(_s(c) for c in (risk_categories or []) if _s(c))

        scored = []
        for sim, idx in zip(sims[0], idxs[0]):
            if idx < 0:
                continue
            m = self.meta[idx]
            dense = float(sim)
            # 키워드 겹침: 쿼리 키워드가 사례 텍스트에 등장하는 비율
            kw_score = 0.0
            if qkw:
                blob_tokens = keyword_tokens(self._blobs[idx])
                hits = sum(1 for t in qkw if t in blob_tokens)
                kw_score = hits / len(qkw)
            cat_score = 1.0 if (cats and _s(m.get("리스크")) in cats) else 0.0
            final = W_DENSE * dense + W_KW * kw_score + W_CAT * cat_score
            scored.append((final, dense, kw_score, cat_score, idx, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for rank, (final, dense, kw_s, cat_s, idx, m) in enumerate(scored[:k], 1):
            results.append({
                "rank":          rank,
                "score":         round(final, 4),
                "dense":         round(dense, 4),
                "keyword_score": round(kw_s, 4),
                "category_hit":  bool(cat_s),
                "News ID":       m.get("News ID"),
                "제목":           m.get("제목"),
                "리스크":         m.get("리스크"),
                "세부 태그":      m.get("세부 태그"),
                "핵심 문장":      m.get("핵심 문장"),
                "기사 요약":      m.get("기사 요약"),
                "리스크 포인트":  m.get("리스크 포인트"),
                "관련 법 및 정책": m.get("관련 법 및 정책"),
                "뉴스 링크":      m.get("뉴스 링크"),
                "언론사":         m.get("언론사"),
                "기사 작성일":    m.get("기사 작성일"),
            })
        return results


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════
def _cli():
    ap = argparse.ArgumentParser(description="NATAM 유사 사례 엔진")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="인덱스 빌드")
    b.add_argument("--limit", type=int, default=None)
    b.add_argument("--backend", choices=["gemini", "local"], default=None)
    b.add_argument("--test", action="store_true", help="_test 태그로 빌드(실 산출물 보존)")

    s = sub.add_parser("search", help="검색 테스트")
    s.add_argument("query")
    s.add_argument("--k", type=int, default=5)
    s.add_argument("--test", action="store_true")
    s.add_argument("--cat", default=None, help="리스크 카테고리(콤마 구분)")
    s.add_argument("--kw", default=None, help="키워드(콤마 구분)")

    sub.add_parser("info", help="매니페스트 출력").add_argument("--test", action="store_true")
    sub.add_parser("finalize", help="중단된 빌드를 현재까지로 부분 마감").add_argument("--test", action="store_true")

    args = ap.parse_args()
    tag = "_test" if getattr(args, "test", False) else ""

    if args.cmd == "build":
        build(limit=args.limit, tag=tag, backend=args.backend)
    elif args.cmd == "finalize":
        finalize_partial(tag=tag)
    elif args.cmd == "search":
        eng = SimilarCaseEngine(tag=tag)
        cats = args.cat.split(",") if args.cat else None
        kws  = args.kw.split(",") if args.kw else None
        res = eng.search(args.query, keywords=kws, risk_categories=cats, k=args.k)
        print(f"\n🔎 '{args.query}'  →  상위 {len(res)}건\n" + "=" * 60)
        for r in res:
            flag = " 🏷️같은리스크군" if r["category_hit"] else ""
            print(f"[{r['rank']}] score={r['score']} (dense={r['dense']}, kw={r['keyword_score']}){flag}")
            print(f"    제목: {r['제목']}")
            print(f"    리스크: {r['리스크']}  |  세부: {r['세부 태그']}  |  {r['언론사']} {r['기사 작성일']}")
            print(f"    리스크포인트: {r['리스크 포인트']}")
            print(f"    링크: {r['뉴스 링크']}\n")
    elif args.cmd == "info":
        P = _paths(tag)
        if os.path.exists(P["manifest"]):
            print(json.dumps(json.load(open(P["manifest"], encoding="utf-8")),
                             ensure_ascii=False, indent=2))
        else:
            print(f"매니페스트 없음: {P['manifest']}")


if __name__ == "__main__":
    _cli()
