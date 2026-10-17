"""
Gemini 오디오 조각 전사(transcribe_with_gemini) 회귀 테스트.

엔진 import 는 무거워서(Whisper·torch) 필요한 정의만 소스에서 뽑아 가짜 클라이언트로 돌린다.
ffmpeg·ffprobe·네트워크는 쓰지 않는다.
"""
import ast
import json
import re
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

ENGINE = Path(__file__).resolve().parents[1] / "mvp_ver_1_9_2.py"
_FUNCS = {"_parse_ts", "_salvage_segments", "_safe_json_parse", "_parse_429_error",
          "_api_is_transient", "_transcribe_items_to_segments"}
_CLASSES = {"TransientAPIError"}
_ASSIGNS = {"_TRANSIENT_HTTP", "_TRANSIENT_WORDS"}
_METHODS = {"_TRANSCRIBE_PROMPT", "_gemini_generate_retry", "_transcribe_audio_chunk",
            "transcribe_with_gemini", "_transcribe_with_aws"}


def _load(**overrides):
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in _FUNCS)
            or (isinstance(n, ast.ClassDef) and n.name in _CLASSES)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in _ASSIGNS for t in n.targets))]
    engine_cls = next(n for n in tree.body
                      if isinstance(n, ast.ClassDef) and n.name == "CrisisConsultantSystem")
    body = [n for n in engine_cls.body
            if (isinstance(n, ast.FunctionDef) and n.name in _METHODS)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in _METHODS for t in n.targets))]
    keep.append(ast.ClassDef(name="Engine", bases=[], keywords=[], body=body, decorator_list=[]))
    ns = {
        "re": re, "json": json, "ThreadPoolExecutor": ThreadPoolExecutor,
        "time": types.SimpleNamespace(sleep=lambda s: None, time=__import__("time").time),
        "TRANSCRIBE_CHUNK_SEC": 120, "TRANSCRIBE_OVERLAP_SEC": 5, "TRANSCRIBE_WORKERS": 4,
        "WHISPER_FALLBACK": False, "GEN_MODEL": "fake-model", "_GEMINI_SEED": 42,
        "TRANSCRIBE_FALLBACK": "", "AWS_TRANSCRIBE_S3": "", "AWS_TRANSCRIBE_TIMEOUT_SEC": 60,
        "_thinking_config": lambda j=False: {"thinking_level": "low"},
        "_repair_truncated_json": lambda s: None,
        # 오디오 대신 "시작:길이" 를 바이트로 넘겨 가짜 클라이언트가 어느 조각인지 알게 한다
        "_extract_audio": lambda src, start=0.0, length=None: f"{start}:{length}".encode(),
        "_probe_duration": lambda p: 300.0,
    }
    ns.update(overrides)
    exec(compile(ast.fix_missing_locations(ast.Module(body=keep, type_ignores=[])), str(ENGINE), "exec"), ns)
    return ns


def mmss(sec):
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


class FakeModels:
    """조각 길이 안에서 10초마다 세그먼트를 돌려준다. text 에는 절대 시각을 적어 검증에 쓴다."""

    def __init__(self, fail=None, fail_code=None, broken_first=False):
        self.chunks = []
        self.calls = 0
        self.seeds = []
        self.fail = fail
        self.fail_code = fail_code
        self.broken_first = broken_first

    def generate_content(self, model, contents, config):
        self.calls += 1
        self.seeds.append(config.get("seed"))
        if isinstance(self.fail, BaseException):
            raise self.fail
        if self.fail:
            err = Exception(self.fail)
            if self.fail_code is not None:
                err.code = self.fail_code
            raise err
        if self.broken_first and self.calls == 1:
            # 10/16 실제로 받은 형태: 키 앞 따옴표 누락
            return types.SimpleNamespace(
                text='{"segments":[{"start":"00:00","end":"00:01","text":"가"},{start":"00:07","end":"00:09"}]}',
                usage_metadata=None)
        start, length = contents[0].inline_data.data.decode().split(":")
        start = float(start)
        length = 300.0 - start if length == "None" else float(length)
        self.chunks.append((start, length))
        segs = [{"start": mmss(t), "end": mmss(t + 4), "text": f"t{int(start + t)}"}
                for t in range(0, int(length) - 4, 10)]
        return types.SimpleNamespace(text=json.dumps({"segments": segs}), usage_metadata=None)


def _engine(ns, models):
    eng = ns["Engine"]()
    eng.client = types.SimpleNamespace(models=models)
    eng.cost = types.SimpleNamespace(add=lambda resp, label="": None)
    return eng


def test_chunks_merge_without_overlap_duplicates():
    models = FakeModels()
    out = _engine(_load(), models).transcribe_with_gemini("v.mp4")

    # 0~120 / 120~240 / 240~300 조각, 앞뒤 5초 겹침
    assert sorted(models.chunks) == [(0, 125.0), (115, 130.0), (235, 65.0)]
    starts = [s["start"] for s in out["segments"]]
    assert starts == sorted(starts)
    # 겹친 구간(115~125, 235~245)의 세그먼트가 두 번 들어가지 않는다
    texts = [s["text"] for s in out["segments"]]
    assert len(texts) == len(set(texts))
    # 조각 시작 시각을 더해 절대 시각이 된다(text 에 적힌 시각과 일치)
    assert all(s["text"] == f"t{int(s['start'])}" for s in out["segments"])
    assert out["video_info"]["duration"] == 300.0
    assert [s["id"] for s in out["segments"]] == list(range(len(out["segments"])))


def test_unknown_duration_uses_one_chunk():
    models = FakeModels()
    out = _engine(_load(_probe_duration=lambda p: None), models).transcribe_with_gemini("v.mp4")
    assert models.chunks == [(0, 300.0)]
    assert out["video_info"]["duration"] == out["segments"][-1]["end"]


def test_transient_error_raises_transient_api_error():
    ns = _load()
    models = FakeModels(fail="503 UNAVAILABLE")
    with pytest.raises(ns["TransientAPIError"]):
        _engine(ns, models).transcribe_with_gemini("v.mp4")


# 같은 모델 재시도·대체 모델은 클라이언트 래퍼 몫(test_gemini_resilience.py). 여기 가짜 클라이언트는
# 래퍼가 없으므로 전사 쪽은 한 번만 부르고 일시/영구 판정만 한다.
def test_gateway_codes_are_transient():
    ns = _load(_probe_duration=lambda p: 60.0)
    models = FakeModels(fail="502 BAD_GATEWAY", fail_code=502)
    with pytest.raises(ns["TransientAPIError"]):
        _engine(ns, models).transcribe_with_gemini("v.mp4")
    assert models.calls == 1

    models = FakeModels(fail="504 Gateway Timeout")
    with pytest.raises(ns["TransientAPIError"]):
        _engine(ns, models).transcribe_with_gemini("v.mp4")
    assert models.calls == 1


@pytest.mark.parametrize("exc", [
    TimeoutError("The read operation timed out"),
    ConnectionResetError("connection reset"),
    httpx.ReadTimeout("The read operation timed out"),
    httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
])
def test_network_errors_are_transient(exc):
    ns = _load(_probe_duration=lambda p: 60.0)
    models = FakeModels(fail=exc)
    with pytest.raises(ns["TransientAPIError"]):
        _engine(ns, models).transcribe_with_gemini("v.mp4")
    assert models.calls == 1


def test_embedded_500_is_not_a_transient_code():
    ns = _load(_probe_duration=lambda p: 60.0)
    models = FakeModels(fail="400 INVALID_ARGUMENT see 1500", fail_code=400)
    with pytest.raises(RuntimeError) as ei:
        _engine(ns, models).transcribe_with_gemini("v.mp4")
    assert not isinstance(ei.value, ns["TransientAPIError"])
    assert models.calls == 1

    models = FakeModels(fail="failed after 1500ms")
    with pytest.raises(RuntimeError):
        _engine(ns, models).transcribe_with_gemini("v.mp4")
    assert models.calls == 1


def test_permanent_error_is_not_retried():
    ns = _load(_probe_duration=lambda p: 60.0)
    models = FakeModels(fail="400 INVALID_ARGUMENT")
    with pytest.raises(RuntimeError) as ei:
        _engine(ns, models).transcribe_with_gemini("v.mp4")
    assert not isinstance(ei.value, ns["TransientAPIError"])
    assert models.calls == 1


def test_whisper_fallback_when_enabled():
    ns = _load(WHISPER_FALLBACK=True)
    eng = _engine(ns, FakeModels(fail="503 UNAVAILABLE"))
    eng._transcribe_with_whisper = lambda p: {"video_info": {"path": p}, "segments": [], "whisper": True}
    out = eng.transcribe_with_gemini("v.mp4")
    assert out["whisper"] is True and out["video_info"]["asr_engine"] == "whisper"


def _aws_on(**kw):
    return _load(TRANSCRIBE_FALLBACK="aws", AWS_TRANSCRIBE_S3="s3://b/transcribe-tmp/", **kw)


def test_gemini_failure_goes_to_aws_transcribe():
    eng = _engine(_aws_on(), FakeModels(fail="503 UNAVAILABLE"))
    eng._transcribe_with_aws = lambda p: {"video_info": {"asr_engine": "aws_transcribe"}, "segments": []}
    assert eng.transcribe_with_gemini("v.mp4")["video_info"]["asr_engine"] == "aws_transcribe"


def test_aws_failure_then_whisper_locally():
    eng = _engine(_aws_on(WHISPER_FALLBACK=True), FakeModels(fail="503 UNAVAILABLE"))
    order = []

    def aws(p):
        order.append("aws")
        raise RuntimeError("AccessDenied")

    def whisper(p):
        order.append("whisper")
        return {"video_info": {"path": p}, "segments": []}

    eng._transcribe_with_aws, eng._transcribe_with_whisper = aws, whisper
    assert eng.transcribe_with_gemini("v.mp4")["video_info"]["asr_engine"] == "whisper"
    assert order == ["aws", "whisper"]


def test_aws_failure_in_container_keeps_gemini_error_for_retry_later():
    ns = _aws_on()
    eng = _engine(ns, FakeModels(fail="503 UNAVAILABLE"))
    eng._transcribe_with_aws = lambda p: (_ for _ in ()).throw(RuntimeError("AccessDenied"))
    with pytest.raises(ns["TransientAPIError"]):   # AWS 오류가 아니라 원래 Gemini 일시 장애
        eng.transcribe_with_gemini("v.mp4")


def test_aws_skipped_without_bucket():
    ns = _load(TRANSCRIBE_FALLBACK="aws", AWS_TRANSCRIBE_S3="")
    eng = _engine(ns, FakeModels(fail="503 UNAVAILABLE"))
    eng._transcribe_with_aws = lambda p: pytest.fail("버킷 없이 AWS 를 부르면 안 됨")
    with pytest.raises(ns["TransientAPIError"]):
        eng.transcribe_with_gemini("v.mp4")


def _item(w, st, en, conf="0.9"):
    return {"type": "pronunciation", "start_time": str(st), "end_time": str(en),
            "alternatives": [{"content": w, "confidence": conf}]}


def _punct(p):
    return {"type": "punctuation", "alternatives": [{"content": p}]}


def test_transcribe_items_split_on_punct_pause_and_length():
    ns = _load()
    items = [_item("안녕", 0.0, 0.5), _item("하세요", 0.6, 1.0, "0.5"), _punct("."),
             _item("오늘은", 1.2, 1.6), _item("쉬고", 3.0, 3.4),            # 1초 넘는 쉼
             *[_item("말", 4.0 + i, 4.5 + i) for i in range(10)]]          # 8초 넘으면 끊음
    segs = ns["_transcribe_items_to_segments"](items)
    assert segs[0]["text"] == "안녕 하세요." and segs[0]["confidence"] == pytest.approx(0.7)
    assert segs[1]["text"] == "오늘은" and segs[2]["start"] == 3.0
    assert all(s["end"] - s["start"] <= 8.0 for s in segs)
    assert sum(s["text"].count("말") for s in segs) == 10


class _FakeAWS:
    def __init__(self, status="COMPLETED"):
        self.status, self.calls = status, []

    def client(self, name):
        return self

    def put_object(self, **kw):
        self.calls.append(("put", kw["Bucket"], kw["Key"]))

    def delete_object(self, **kw):
        self.calls.append(("delete_object", kw["Key"]))

    def start_transcription_job(self, **kw):
        self.calls.append(("start", kw["LanguageCode"], kw["Media"]["MediaFileUri"]))

    def get_transcription_job(self, **kw):
        return {"TranscriptionJob": {"TranscriptionJobStatus": self.status, "FailureReason": "bad audio",
                                     "Transcript": {"TranscriptFileUri": "https://example/t.json"}}}

    def delete_transcription_job(self, **kw):
        self.calls.append(("delete_job",))


def _urlopen_ok(url, timeout=None):
    body = json.dumps({"results": {"items": [_item("가", 0.0, 0.4), _punct("?")]}}).encode()

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body
    return R()


def test_aws_transcribe_uploads_transcribes_and_cleans_up(monkeypatch):
    import sys
    import urllib.request
    fake = _FakeAWS()
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_ok)
    eng = _engine(_aws_on(_probe_duration=lambda p: 12.0), FakeModels())
    out = eng._transcribe_with_aws("v.mp4")

    put, start = fake.calls[0], fake.calls[1]
    assert put[1] == "b" and put[2].startswith("transcribe-tmp/lavcca-asr-") and put[2].endswith(".mp3")
    assert start == ("start", "ko-KR", f"s3://b/{put[2]}")
    assert ("delete_object", put[2]) in fake.calls and ("delete_job",) in fake.calls
    assert out["video_info"] == {"path": "v.mp4", "duration": 12.0, "asr_engine": "aws_transcribe"}
    assert out["segments"][0]["text"] == "가?" and out["segments"][0]["avg_logprob"] < 0


def test_aws_transcribe_failure_still_cleans_up(monkeypatch):
    import sys
    fake = _FakeAWS(status="FAILED")
    monkeypatch.setitem(sys.modules, "boto3", fake)
    eng = _engine(_aws_on(), FakeModels())
    with pytest.raises(RuntimeError, match="bad audio"):
        eng._transcribe_with_aws("v.mp4")
    assert [c[0] for c in fake.calls] == ["put", "start", "delete_object", "delete_job"]


def test_broken_json_chunk_is_requested_again_with_other_seed():
    models = FakeModels(broken_first=True)
    out = _engine(_load(_probe_duration=lambda p: 60.0), models).transcribe_with_gemini("v.mp4")
    assert models.calls == 2 and models.seeds == [None, 43]
    assert len(out["segments"]) == 6   # 깨진 응답의 1개가 아니라 다시 받은 전체


def test_salvage_accepts_mmss():
    ns = _load()
    cut = '{"segments":[{"start":"01:05","end":"01:07.5","text":"가"},{"start":"01:0'
    assert ns["_salvage_segments"](cut) == [{"start": 65.0, "end": 67.5, "text": "가"}]
    assert ns["_parse_ts"]("1:02:03") == 3723.0
