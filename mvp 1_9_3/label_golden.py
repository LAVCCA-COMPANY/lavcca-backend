# -*- coding: utf-8 -*-
"""
label_golden.py  —  골든셋 라벨링 도구 (로컬 브라우저, 표준 라이브러리만)

golden_set_candidates.json 의 후보를 한 문장씩 보여 주고, 사람이 고른 라벨을
golden_set.json 에 바로 저장한다 (라벨을 채운 항목만 저장 → eval 이 전부 gold 로 평가).
다시 실행하면 golden_set.json 의 라벨을 불러와 이어서 한다.

사용:
  python label_golden.py            # http://127.0.0.1:8765 이 열린다
  (라벨링 후) python eval_harness.py golden_set.json --save-baseline

키:
  1~9, 0   라벨 토글 (화면 버튼 순서)     Enter  저장 후 다음
  ← / →    이전 / 다음 (저장 안 함)        Del    이 문장 라벨 지우기
  H        모델·Tier2 참고 정보 보기/숨기기
"""

import glob
import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR        = Path(__file__).parent
CANDIDATES_PATH = BASE_DIR / "golden_set_candidates.json"
GOLDEN_PATH     = BASE_DIR / "golden_set.json"
LABELS_PATH     = BASE_DIR / "controversy_labels.yaml"
REPORT_GLOB     = str(BASE_DIR / "reports" / "report_*.json")
HOST, PORT      = "127.0.0.1", 8765
HIGH_SEVERITY   = {"L03", "L06", "L09", "L11"}
CONTEXT_N       = 2   # 앞뒤로 보여 줄 자막 줄 수


def _key(item):
    s = item["source"]
    return f'{s["video_id"]}|{s["segment_id"]}|{item["text"]}'


def _load_label_defs():
    """controversy_labels.yaml 에서 id/name/description 만 읽는다 (yaml 의존성 없이)."""
    defs, cur = [], None
    for line in LABELS_PATH.read_text(encoding="utf-8").splitlines():
        m = re.match(r'\s*-?\s*(id|name|description):\s*"([^"]*)"', line)
        if not m:
            continue
        if m.group(1) == "id":
            cur = {"id": m.group(2)}
            defs.append(cur)
        elif cur is not None:
            cur[m.group(1)] = m.group(2)
    return defs


def _video_id(path):
    m = re.search(r"_([\w-]{11})\.json$", path)
    return m.group(1) if m else Path(path).stem


def _load_transcripts():
    """video_id → 교정 자막 목록. 같은 영상 리포트가 여러 개면 처음 것만 쓴다."""
    out = {}
    for path in sorted(glob.glob(REPORT_GLOB)):
        vid = _video_id(path)
        if vid in out:
            continue
        rep = json.loads(Path(path).read_text(encoding="utf-8"))
        out[vid] = [{"segment_id": s.get("segment_id", i),
                     "timestamp": s.get("timestamp"),
                     "text": (s.get("corrected_text") or "").strip()}
                    for i, s in enumerate(rep.get("transcript_analysis") or [])]
    return out


def _priority(item):
    """검수 순서: Tier2 논란 → 고심각도 의심 → 기타 의심 → 정상 샘플."""
    t2 = item.get("tier2") or {}
    sus = {l for l, p in item["model_suggested"].items() if l != "L12" and p >= 0.15}
    if t2.get("is_controversy"):
        return 0
    if sus & HIGH_SEVERITY:
        return 1
    if sus:
        return 2
    return 3


def build_items():
    items = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8-sig"))
    saved = {}
    if GOLDEN_PATH.exists():
        saved = {_key(g): g.get("labels") or []
                 for g in json.loads(GOLDEN_PATH.read_text(encoding="utf-8-sig"))}
    transcripts = _load_transcripts()
    for item in items:
        item["labels"] = saved.get(_key(item), item.get("labels") or [])
        segs = transcripts.get(item["source"]["video_id"], [])
        pos = next((i for i, s in enumerate(segs)
                    if s["segment_id"] == item["source"]["segment_id"]), None)
        item["_before"] = [s["text"] for s in segs[max(0, pos - CONTEXT_N):pos]] if pos is not None else []
        item["_after"] = [s["text"] for s in segs[pos + 1:pos + 1 + CONTEXT_N]] if pos is not None else []
    items.sort(key=_priority)
    return items


ITEMS = build_items()


def save():
    """라벨이 있는 항목만 golden_set.json 으로. 임시 파일에 쓰고 바꿔 끼운다."""
    done = [{k: v for k, v in it.items() if not k.startswith("_")}
            for it in ITEMS if it["labels"]]
    tmp = GOLDEN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GOLDEN_PATH)
    return len(done)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/items":
            self._send(200, json.dumps({"items": ITEMS, "labels": _load_label_defs(),
                                        "high": sorted(HIGH_SEVERITY)}, ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if self.path != "/api/label":
            return self._send(404, "{}")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        labels = [l for l in body.get("labels", []) if isinstance(l, str)]
        if "L12" in labels and len(labels) > 1:   # 해당 없음은 다른 라벨과 겹칠 수 없다
            return self._send(400, json.dumps({"error": "L12 는 단독으로만"}))
        ITEMS[int(body["idx"])]["labels"] = labels
        self._send(200, json.dumps({"saved": save()}))

    def log_message(self, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>골든셋 라벨링</title>
<style>
 body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:860px;margin:24px auto;padding:0 16px;background:#fafafa;color:#222}
 .bar{height:6px;background:#ddd;border-radius:3px;overflow:hidden}.bar>div{height:100%;background:#2b7a4b}
 .meta{color:#666;font-size:13px;margin:10px 0}
 .ctx{color:#888;font-size:15px;line-height:1.6}
 .text{font-size:22px;line-height:1.5;margin:10px 0;padding:14px 16px;background:#fff;border-left:4px solid #2b7a4b;border-radius:4px}
 .labels{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px;margin:16px 0}
 button.l{text-align:left;padding:8px 10px;border:1px solid #ccc;border-radius:6px;background:#fff;cursor:pointer;font-size:14px}
 button.l small{display:block;color:#777;font-size:12px}
 button.l.on{background:#2b7a4b;color:#fff;border-color:#2b7a4b}button.l.on small{color:#dfe}
 button.l.hs{border-left:4px solid #c0392b}
 .hint{font-size:13px;background:#fff8e1;padding:8px 10px;border-radius:6px;margin:8px 0}
 .nav button{padding:8px 14px;margin-right:6px;font-size:14px}
 .keys{color:#888;font-size:12px;margin-top:14px}
</style></head><body>
<div class="bar"><div id="prog"></div></div>
<div class="meta" id="meta"></div>
<div class="ctx" id="before"></div>
<div class="text" id="text"></div>
<div class="ctx" id="after"></div>
<div class="hint" id="hint" hidden></div>
<div class="labels" id="labels"></div>
<div class="nav">
 <button onclick="go(-1)">← 이전</button>
 <button onclick="commit()"><b>저장 후 다음 (Enter)</b></button>
 <button onclick="go(1)">다음 →</button>
 <button onclick="nextUnlabeled()">다음 미검수로</button>
 <span id="status"></span>
</div>
<div class="keys">1~9·0 라벨 토글 · Enter 저장 후 다음 · ←/→ 이동 · Del 라벨 지우기 · H 참고 정보 · 애매하면 비워 두고 넘어가세요(평가에서 빠짐). 빨간 줄 = 고심각도</div>
<script>
let items=[], defs=[], high=[], i=0, sel=new Set(), showHint=false;
const KEYS="1234567890";
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
fetch("/api/items").then(r=>r.json()).then(d=>{items=d.items;defs=d.labels;high=d.high;
 i=Math.max(0,items.findIndex(x=>!x.labels.length));render();});
function render(){
 const it=items[i]; sel=new Set(it.labels);
 const done=items.filter(x=>x.labels.length).length;
 document.getElementById("prog").style.width=(100*done/items.length)+"%";
 const s=it.source;
 document.getElementById("meta").textContent=`${i+1} / ${items.length} · 검수 ${done}개 · ${s.video_id} #${s.segment_id} ${s.timestamp||""}`;
 document.getElementById("before").innerHTML=it._before.map(esc).join("<br>");
 document.getElementById("after").innerHTML=it._after.map(esc).join("<br>");
 document.getElementById("text").textContent=it.text;
 const ms=Object.entries(it.model_suggested).sort((a,b)=>b[1]-a[1]).map(([l,p])=>`${l} ${p}`).join(", ");
 const t2=it.tier2?`${it.tier2.is_controversy?"논란":"정상"} ${it.tier2.category||""} ${it.tier2.severity||""}`:"없음";
 const h=document.getElementById("hint"); h.hidden=!showHint; h.textContent=`모델: ${ms||"-"}   |   Tier2: ${t2}`;
 document.getElementById("labels").innerHTML=defs.map((d,k)=>
  `<button class="l ${sel.has(d.id)?"on":""} ${high.includes(d.id)?"hs":""}" onclick="toggle('${d.id}')">`+
  `[${KEYS[k]||""}] ${d.id} ${esc(d.name)}<small>${esc(d.description||"")}</small></button>`).join("");
}
function toggle(id){
 if(sel.has(id)) sel.delete(id);
 else { if(id==="L12") sel.clear(); else sel.delete("L12"); sel.add(id); }
 items[i].labels=[...sel]; render();
}
async function send(labels){
 const r=await fetch("/api/label",{method:"POST",body:JSON.stringify({idx:i,labels})});
 const d=await r.json(); document.getElementById("status").textContent=r.ok?`저장됨 (golden_set.json ${d.saved}개)`:d.error;
 return r.ok;
}
async function commit(){ if(await send([...sel])) go(1); }
function go(n){ i=Math.min(items.length-1,Math.max(0,i+n)); render(); }
function nextUnlabeled(){ const k=items.findIndex((x,j)=>j>i&&!x.labels.length); if(k>=0){i=k;render();} }
document.addEventListener("keydown",e=>{
 const k=KEYS.indexOf(e.key);
 if(k>=0&&defs[k]) toggle(defs[k].id);
 else if(e.key==="Enter") commit();
 else if(e.key==="ArrowLeft") go(-1);
 else if(e.key==="ArrowRight") go(1);
 else if(e.key==="Delete"){ sel.clear(); items[i].labels=[]; send([]); render(); }
 else if(e.key==="h"||e.key==="H"){ showHint=!showHint; render(); }
 else return;
 e.preventDefault();   // 포커스된 버튼이 Enter 로 한 번 더 눌리지 않게
});
</script></body></html>"""


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"🏷️  라벨링 도구: {url}  (후보 {len(ITEMS)}개, 저장 → {GOLDEN_PATH.name})  종료: Ctrl+C")
    webbrowser.open(url)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
