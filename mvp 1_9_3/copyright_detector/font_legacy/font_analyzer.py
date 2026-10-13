"""
analyzers/font_analyzer.py - 자막/폰트 저작권 분석
OCR로 텍스트 추출 → 한글/영문 폰트 시각 분류 → 상업용 폰트 검출

한글 폰트 분석 전략:
  1. OCR 텍스트에서 폰트명 직접 감지 (가장 정확)
  2. 획 굵기 변동계수(CV) + 수평수직 비율로 스타일 분류
  3. 스타일 → 상업용 위험도 매핑
"""
import asyncio
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
import structlog
import numpy as np
import cv2

from config import config
from database.db_manager import get_db_manager
from utils.ocr_engine import safe_ocr
from utils.font_classifier import get_font_classifier

logger = structlog.get_logger()


# ─────────────────────────────────────────────
# 상업용 폰트 DB (한국어 중심 대폭 확장)
# ─────────────────────────────────────────────
COMMERCIAL_FONTS_SEED = {
    # ────── 영문 상업 폰트 ──────
    "helvetica":         {"foundry": "Linotype",             "risk": 0.85},
    "futura":            {"foundry": "Bauer",                "risk": 0.80},
    "gotham":            {"foundry": "Hoefler & Co.",        "risk": 0.85},
    "proxima nova":      {"foundry": "Mark Simonson Studio", "risk": 0.80},
    "brandon grotesque": {"foundry": "HVD Fonts",            "risk": 0.75},
    "avenir":            {"foundry": "Linotype",             "risk": 0.80},
    "gill sans":         {"foundry": "Monotype",             "risk": 0.75},
    "times new roman":   {"foundry": "Monotype",             "risk": 0.70},
    "bodoni":            {"foundry": "Berthold",             "risk": 0.70},
    "ff din":            {"foundry": "FontFont",             "risk": 0.80},
    "myriad":            {"foundry": "Adobe",                "risk": 0.75},
    "frutiger":          {"foundry": "Linotype",             "risk": 0.80},

    # ────── 한국 무료 폰트 ──────
    "나눔고딕":               {"foundry": "Naver",        "risk": 0.05, "is_free": True},
    "나눔명조":               {"foundry": "Naver",        "risk": 0.05, "is_free": True},
    "나눔바른고딕":           {"foundry": "Naver",        "risk": 0.05, "is_free": True},
    "나눔스퀘어":             {"foundry": "Naver",        "risk": 0.05, "is_free": True},
    "나눔스퀘어라운드":       {"foundry": "Naver",        "risk": 0.05, "is_free": True},
    "나눔펜스크립트":         {"foundry": "Naver",        "risk": 0.05, "is_free": True},
    "나눔브러시스크립트":     {"foundry": "Naver",        "risk": 0.05, "is_free": True},
    "마루부리":               {"foundry": "Naver",        "risk": 0.05, "is_free": True},
    "spoqa han sans":        {"foundry": "Spoqa",         "risk": 0.05, "is_free": True},
    "s-core dream":          {"foundry": "S-Core",        "risk": 0.05, "is_free": True},
    "gmarket sans":          {"foundry": "Gmarket",       "risk": 0.05, "is_free": True},
    "g마켓산스":              {"foundry": "Gmarket",       "risk": 0.05, "is_free": True},
    "bm euljiro":            {"foundry": "Woowa Bros",    "risk": 0.05, "is_free": True},
    "bm hanna":              {"foundry": "Woowa Bros",    "risk": 0.05, "is_free": True},
    "bm dohyeon":            {"foundry": "Woowa Bros",    "risk": 0.05, "is_free": True},
    "bm jua":                {"foundry": "Woowa Bros",    "risk": 0.05, "is_free": True},
    "bm kirang":             {"foundry": "Woowa Bros",    "risk": 0.05, "is_free": True},
    "서울서체":               {"foundry": "서울시",        "risk": 0.05, "is_free": True},
    "서울남산":               {"foundry": "서울시",        "risk": 0.05, "is_free": True},
    "서울한강":               {"foundry": "서울시",        "risk": 0.05, "is_free": True},
    "제주고딕":               {"foundry": "제주도",        "risk": 0.05, "is_free": True},
    "제주명조":               {"foundry": "제주도",        "risk": 0.05, "is_free": True},
    "제주한라산":             {"foundry": "제주도",        "risk": 0.05, "is_free": True},
    "본고딕":                 {"foundry": "Adobe/Google", "risk": 0.05, "is_free": True},
    "본명조":                 {"foundry": "Adobe/Google", "risk": 0.05, "is_free": True},
    "source han sans":       {"foundry": "Adobe",         "risk": 0.05, "is_free": True},
    "source han serif":      {"foundry": "Adobe",         "risk": 0.05, "is_free": True},
    "noto sans kr":          {"foundry": "Google",        "risk": 0.05, "is_free": True},
    "noto serif kr":         {"foundry": "Google",        "risk": 0.05, "is_free": True},
    "ibm plex sans kr":      {"foundry": "IBM",           "risk": 0.05, "is_free": True},
    "kopub돋움체":            {"foundry": "한국출판인회의", "risk": 0.05, "is_free": True},
    "kopub바탕체":            {"foundry": "한국출판인회의", "risk": 0.05, "is_free": True},
    "이롭게바탕체":           {"foundry": "이롭게",        "risk": 0.05, "is_free": True},
    "한컴말랑말랑":           {"foundry": "Hancom",        "risk": 0.05, "is_free": True},
    "조선일보명조":           {"foundry": "조선일보",      "risk": 0.05, "is_free": True},
    "경기천년체":             {"foundry": "경기도",        "risk": 0.05, "is_free": True},
    "야놀자야체":             {"foundry": "야놀자",        "risk": 0.05, "is_free": True},

    # ────── 한국 상업 폰트 — 윤디자인 ──────
    "윤고딕":       {"foundry": "윤디자인", "risk": 0.87},
    "윤고딕100":    {"foundry": "윤디자인", "risk": 0.87},
    "윤고딕200":    {"foundry": "윤디자인", "risk": 0.87},
    "윤고딕300":    {"foundry": "윤디자인", "risk": 0.87},
    "윤고딕500":    {"foundry": "윤디자인", "risk": 0.87},
    "윤고딕700":    {"foundry": "윤디자인", "risk": 0.87},
    "윤명조":       {"foundry": "윤디자인", "risk": 0.87},
    "윤명조100":    {"foundry": "윤디자인", "risk": 0.87},
    "윤명조300":    {"foundry": "윤디자인", "risk": 0.87},
    "윤체":         {"foundry": "윤디자인", "risk": 0.85},
    "윤디자인":     {"foundry": "윤디자인", "risk": 0.80},

    # ────── 한국 상업 폰트 — SM Corp ──────
    "sm신명조":     {"foundry": "SM Corp", "risk": 0.87},
    "sm고딕":       {"foundry": "SM Corp", "risk": 0.87},
    "sm중명조":     {"foundry": "SM Corp", "risk": 0.85},
    "sm중고딕":     {"foundry": "SM Corp", "risk": 0.85},
    "sm신고딕":     {"foundry": "SM Corp", "risk": 0.82},
    "sm3신명조":    {"foundry": "SM Corp", "risk": 0.87},
    "sm3고딕":      {"foundry": "SM Corp", "risk": 0.87},
    "sm그래픽":     {"foundry": "SM Corp", "risk": 0.80},
    "sm세명조":     {"foundry": "SM Corp", "risk": 0.85},
    "sm세고딕":     {"foundry": "SM Corp", "risk": 0.85},

    # ────── 한국 상업 폰트 — HY (한양정보통신) ──────
    "hy신명조":     {"foundry": "HY", "risk": 0.82},
    "hy중고딕":     {"foundry": "HY", "risk": 0.82},
    "hy엽서":       {"foundry": "HY", "risk": 0.77},
    "hy헤드라인":   {"foundry": "HY", "risk": 0.82},
    "hy목각파임":   {"foundry": "HY", "risk": 0.82},
    "hy그래픽":     {"foundry": "HY", "risk": 0.77},
    "hy크리스탈":   {"foundry": "HY", "risk": 0.77},
    "hy견고딕":     {"foundry": "HY", "risk": 0.77},
    "hy울릉도":     {"foundry": "HY", "risk": 0.77},
    "hy수평선":     {"foundry": "HY", "risk": 0.72},
    "hy강b":        {"foundry": "HY", "risk": 0.80},
    "hy펀":         {"foundry": "HY", "risk": 0.77},
    "hy바다":       {"foundry": "HY", "risk": 0.77},
    "hy나무":       {"foundry": "HY", "risk": 0.77},

    # ────── 한국 상업 폰트 — Sandoll (산돌) ──────
    "sandoll":           {"foundry": "Sandoll", "risk": 0.88},
    "산돌":              {"foundry": "Sandoll", "risk": 0.85},
    "sd삼립":            {"foundry": "Sandoll", "risk": 0.88},
    # 미생체·빙그레체: 산돌 제작이지만 각 사가 무료 배포 → is_free (오탐 방지)
    "sd미생":            {"foundry": "Sandoll/윤태호", "risk": 0.05, "is_free": True},
    "미생체":            {"foundry": "Sandoll/윤태호", "risk": 0.05, "is_free": True},
    "sd빙그레":          {"foundry": "빙그레",  "risk": 0.05, "is_free": True},
    "sd빙그레ii":        {"foundry": "빙그레",  "risk": 0.05, "is_free": True},
    "sd코어":            {"foundry": "Sandoll", "risk": 0.88},
    "sd아이원":          {"foundry": "Sandoll", "risk": 0.85},
    "sd독수리오남매":    {"foundry": "Sandoll", "risk": 0.85},
    "sd평화":            {"foundry": "Sandoll", "risk": 0.85},
    "sd해결사":          {"foundry": "Sandoll", "risk": 0.85},

    # ────── 한국 상업 폰트 — 방송사 전용 ──────
    "tvn":               {"foundry": "CJ ENM", "risk": 0.92},
    # tvN 즐거운이야기체는 무료 배포 (상업 사용 허용) → 오탐 방지
    "tvn즐거운이야기":   {"foundry": "CJ ENM", "risk": 0.05, "is_free": True},
    "tvn드라마":         {"foundry": "CJ ENM", "risk": 0.92},
    "sbs고딕":           {"foundry": "SBS",    "risk": 0.87},
    "sbsg":              {"foundry": "SBS",    "risk": 0.87},
    "sbs드라마":         {"foundry": "SBS",    "risk": 0.87},
    "mbc미니":           {"foundry": "MBC",    "risk": 0.82},
    "mbc드라마":         {"foundry": "MBC",    "risk": 0.82},
    "kbs2고딕":          {"foundry": "KBS",    "risk": 0.82},
    "jtbc돋움":          {"foundry": "JTBC",   "risk": 0.87},

    # ────── 한국 상업 폰트 — 기업 전용 ──────
    "아리따":            {"foundry": "Amorepacific", "risk": 0.87},
    "아리따돋움":        {"foundry": "Amorepacific", "risk": 0.87},
    "아리따부리":        {"foundry": "Amorepacific", "risk": 0.87},
    "이니스프리":        {"foundry": "Amorepacific", "risk": 0.85},
    "카카오":            {"foundry": "Kakao",        "risk": 0.85},
    "카카오big":         {"foundry": "Kakao",        "risk": 0.85},
    "카카오small":       {"foundry": "Kakao",        "risk": 0.85},
    "네이버나눔체":      {"foundry": "Naver",        "risk": 0.05, "is_free": True},
    "현대":              {"foundry": "Hyundai",      "risk": 0.85},
    "기아":              {"foundry": "Kia",          "risk": 0.85},
    # 롯데리아 폰트(딱붙어체·촵땡겨체)는 무료 배포 → 오탐 방지
    "롯데리아":          {"foundry": "Lotteria",     "risk": 0.05, "is_free": True},

    # ────── 한국 상업 폰트 — AG ──────
    "ag최정호":          {"foundry": "AG",  "risk": 0.87},
    "ag타이포":          {"foundry": "AG",  "risk": 0.82},
    "ag새벽하늘":        {"foundry": "AG",  "risk": 0.82},
    "ag올드페이스":      {"foundry": "AG",  "risk": 0.80},

    # ────── 한국 상업 폰트 — 온글잎/기타 ──────
    "온글잎":            {"foundry": "Ongeurip", "risk": 0.77},
    "평창평화체":        {"foundry": "Pyeongchang", "risk": 0.77},
    "에스코어드림":      {"foundry": "S-Core", "risk": 0.05, "is_free": True},
    "넥슨":              {"foundry": "Nexon", "risk": 0.72},
    "넥슨lv1고딕":       {"foundry": "Nexon", "risk": 0.05, "is_free": True},
    "마포":              {"foundry": "마포구", "risk": 0.05, "is_free": True},

    # ══════════════════════════════════════════════
    # 유튜브 인기 한글 폰트 시드 (썸네일·자막 사용 빈도 상위)
    # 라이선스 정보는 2026-06 기준 배포처 공지 기반 — 변경될 수 있으므로
    # 유료→무료 전환 등 갱신 시 이 목록을 수정할 것.
    # ══════════════════════════════════════════════

    # ── 유료 (유튜브 썸네일·자막 단골 상업 폰트 → 감지 대상) ──
    "격동고딕":          {"foundry": "Sandoll",  "risk": 0.88},   # 썸네일 1위급 유료
    "격동명조":          {"foundry": "Sandoll",  "risk": 0.87},
    "격동굴림":          {"foundry": "Sandoll",  "risk": 0.85},
    "산돌고딕네오":      {"foundry": "Sandoll",  "risk": 0.87},
    "고딕네오":          {"foundry": "Sandoll",  "risk": 0.85},
    "산돌명조네오":      {"foundry": "Sandoll",  "risk": 0.87},
    "산돌광수":          {"foundry": "Sandoll",  "risk": 0.85},
    "광수체":            {"foundry": "Sandoll",  "risk": 0.85},
    "공병각":            {"foundry": "Sandoll",  "risk": 0.85},
    "rix고딕":           {"foundry": "Fontrix",  "risk": 0.85},
    "rix모던고딕":       {"foundry": "Fontrix",  "risk": 0.85},
    "rix락앤롤":         {"foundry": "Fontrix",  "risk": 0.82},
    "머리정체":          {"foundry": "윤디자인", "risk": 0.87},
    "윤굴림":            {"foundry": "윤디자인", "risk": 0.85},

    # ── 무료 — 썸네일 인기 임팩트체 ──
    "프리텐다드":        {"foundry": "길형진(orioncactus)", "risk": 0.05, "is_free": True},
    "pretendard":        {"foundry": "길형진(orioncactus)", "risk": 0.05, "is_free": True},
    "어그로체":          {"foundry": "SB(샌드박스)",  "risk": 0.05, "is_free": True},
    "sb어그로":          {"foundry": "SB(샌드박스)",  "risk": 0.05, "is_free": True},
    "여기어때잘난체":    {"foundry": "여기어때",      "risk": 0.05, "is_free": True},
    "잘난체":            {"foundry": "여기어때",      "risk": 0.05, "is_free": True},
    "검은고딕":          {"foundry": "ZessType",      "risk": 0.05, "is_free": True},
    "black han sans":    {"foundry": "ZessType",      "risk": 0.05, "is_free": True},
    "쿠키런":            {"foundry": "데브시스터즈",  "risk": 0.05, "is_free": True},
    "cookierun":         {"foundry": "데브시스터즈",  "risk": 0.05, "is_free": True},
    "메이플스토리체":    {"foundry": "Nexon",         "risk": 0.05, "is_free": True},
    "maplestory":        {"foundry": "Nexon",         "risk": 0.05, "is_free": True},
    "넷마블체":          {"foundry": "Netmarble",     "risk": 0.05, "is_free": True},
    "넥슨lv2고딕":       {"foundry": "Nexon",         "risk": 0.05, "is_free": True},
    "몬소리체":          {"foundry": "티몬",          "risk": 0.05, "is_free": True},
    "티몬몬소리":        {"foundry": "티몬",          "risk": 0.05, "is_free": True},
    "토스페이스":        {"foundry": "Toss",          "risk": 0.05, "is_free": True},
    "지마켓산스":        {"foundry": "Gmarket",       "risk": 0.05, "is_free": True},
    "파셜산스":          {"foundry": "OFL(Google Fonts)", "risk": 0.05, "is_free": True},

    # ── 무료 — 배달의민족 한글명 (영문 bm 시리즈와 병행) ──
    "배민도현":          {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "도현체":            {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "배민주아":          {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "주아체":            {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "배민한나":          {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "한나체":            {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "한나는열한살":      {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "배민연성":          {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "연성체":            {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "배민기랑해랑":      {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "을지로체":          {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "을지로10년후":      {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},
    "글림체":            {"foundry": "Woowa Bros", "risk": 0.05, "is_free": True},

    # ── 무료 — 카페24 시리즈 ──
    "카페24써라운드":    {"foundry": "Cafe24", "risk": 0.05, "is_free": True},
    "카페24아네모네":    {"foundry": "Cafe24", "risk": 0.05, "is_free": True},
    "카페24빛나는별":    {"foundry": "Cafe24", "risk": 0.05, "is_free": True},
    "카페24단정해":      {"foundry": "Cafe24", "risk": 0.05, "is_free": True},
    "카페24고운밤":      {"foundry": "Cafe24", "risk": 0.05, "is_free": True},
    "카페24동동":        {"foundry": "Cafe24", "risk": 0.05, "is_free": True},
    "카페24쑥쑥":        {"foundry": "Cafe24", "risk": 0.05, "is_free": True},
    "카페24심플해":      {"foundry": "Cafe24", "risk": 0.05, "is_free": True},
    "카페24클래식타입":  {"foundry": "Cafe24", "risk": 0.05, "is_free": True},

    # ── 무료 — 기업 배포 ──
    "빙그레체":          {"foundry": "빙그레",   "risk": 0.05, "is_free": True},
    "빙그레따옴":        {"foundry": "빙그레",   "risk": 0.05, "is_free": True},
    "빙그레메로나":      {"foundry": "빙그레",   "risk": 0.05, "is_free": True},
    "빙그레싸만코":      {"foundry": "빙그레",   "risk": 0.05, "is_free": True},
    "롯데리아딱붙어":    {"foundry": "Lotteria", "risk": 0.05, "is_free": True},
    "롯데리아촵땡겨":    {"foundry": "Lotteria", "risk": 0.05, "is_free": True},
    "이사만루체":        {"foundry": "공게임즈", "risk": 0.05, "is_free": True},
    "kbo다이아고딕":     {"foundry": "KBO",      "risk": 0.05, "is_free": True},
    "티웨이항공체":      {"foundry": "티웨이항공", "risk": 0.05, "is_free": True},
    "티웨이하늘체":      {"foundry": "티웨이항공", "risk": 0.05, "is_free": True},
    "비트로코어체":      {"foundry": "VITRO",    "risk": 0.05, "is_free": True},
    "비트로프라이드체":  {"foundry": "VITRO",    "risk": 0.05, "is_free": True},
    "원스토어모바일고딕": {"foundry": "ONE store", "risk": 0.05, "is_free": True},
    "수트체":            {"foundry": "SUIT/sunn.us", "risk": 0.05, "is_free": True},
    "페이퍼로지":        {"foundry": "Paperlogy",   "risk": 0.05, "is_free": True},
    "교보손글씨":        {"foundry": "교보문고",   "risk": 0.05, "is_free": True},
    "부크크명조":        {"foundry": "부크크",     "risk": 0.05, "is_free": True},
    "부크크고딕":        {"foundry": "부크크",     "risk": 0.05, "is_free": True},

    # ── 무료 — 공공기관·지자체 배포 ──
    "강원교육모두":      {"foundry": "강원도교육청", "risk": 0.05, "is_free": True},
    "강원교육튼튼":      {"foundry": "강원도교육청", "risk": 0.05, "is_free": True},
    "강원교육새음":      {"foundry": "강원도교육청", "risk": 0.05, "is_free": True},
    "전주완판본체":      {"foundry": "전주시",       "risk": 0.05, "is_free": True},
    "부산체":            {"foundry": "부산시",       "risk": 0.05, "is_free": True},
    "고양체":            {"foundry": "고양시",       "risk": 0.05, "is_free": True},
    "김포평화바탕":      {"foundry": "김포시",       "risk": 0.05, "is_free": True},
    "포천막걸리체":      {"foundry": "포천시",       "risk": 0.05, "is_free": True},
    "순바탕":            {"foundry": "한국출판문화산업진흥원", "risk": 0.05, "is_free": True},
    "동그라미재단":      {"foundry": "동그라미재단", "risk": 0.05, "is_free": True},

    # ── 무료 — 손글씨·기타 인기 ──
    "이서윤체":          {"foundry": "이서윤×넷마블", "risk": 0.05, "is_free": True},
    "양진체":            {"foundry": "양진",          "risk": 0.05, "is_free": True},
    "잉크립퀴드체":      {"foundry": "잉크립퀴드",    "risk": 0.05, "is_free": True},
    "나눔손글씨":        {"foundry": "Naver",         "risk": 0.05, "is_free": True},
}

# 한글 스타일별 위험도
KOREAN_STYLE_RISK = {
    "명조":   0.72,   # 세리프 → 대부분 상업용
    "고딕":   0.48,   # 산세리프 → 무료/상업용 혼재
    "손글씨": 0.25,   # 대부분 무료 또는 개인용
    "장식체": 0.65,   # 특수 디자인 → 상업용 많음
}

# 스타일별 표시명
KOREAN_STYLE_LABEL = {
    "명조":   "한글 명조체",
    "고딕":   "한글 고딕체",
    "손글씨": "한글 손글씨체",
    "장식체": "한글 장식체",
}


# ─────────────────────────────────────────────
# 전처리
# ─────────────────────────────────────────────
def detect_text_regions(frame: np.ndarray) -> List[Dict]:
    h, w = frame.shape[:2]
    return [
        {"type": "subtitle", "roi": frame[int(h * 0.75):, :], "zone": "bottom"},
        {"type": "mid",      "roi": frame[int(h * 0.35):int(h * 0.65), :], "zone": "middle"},
    ]


def preprocess_for_ocr(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    enlarged = cv2.resize(gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(enlarged, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


# ─────────────────────────────────────────────
# 폰트 분석기
# ─────────────────────────────────────────────
class FontAnalyzer:

    def __init__(self):
        self.db = get_db_manager()
        self.executor = ThreadPoolExecutor(max_workers=3)
        self._known_commercial_fonts = self._load_known_fonts()

    def _load_known_fonts(self) -> Dict:
        fonts = dict(COMMERCIAL_FONTS_SEED)
        try:
            db_fonts = self.db.get_all_known_fonts()
            for f in db_fonts:
                key = f["name"].lower()
                if key not in fonts:
                    fonts[key] = {"foundry": f.get("foundry", ""), "risk": 0.75}
        except Exception:
            pass
        return fonts

    async def analyze(self, frames: List[Tuple[float, np.ndarray]],
                      job_id: str) -> List[Dict]:
        # 폰트 분석 보류 — config.pipeline.enable_font_analysis 로 재개
        # (시각 폰트 분류 신뢰 불가 + 휴리스틱 오탐으로 전체 비활성)
        if not getattr(config.pipeline, "enable_font_analysis", False):
            logger.info("font_analysis_skipped_disabled", job_id=job_id)
            return []

        logger.info("font_analysis_start", job_id=job_id)

        # 25초 간격 샘플링 (OCR 호출 수 최소화: 10분 영상 → ~24프레임)
        sampled = []
        last_time = -25
        for ts, frame in frames:
            if ts - last_time >= 25.0:
                sampled.append((ts, frame))
                last_time = ts

        loop = asyncio.get_event_loop()
        semaphore = asyncio.Semaphore(2)

        async def analyze_frame(ts, frame):
            async with semaphore:
                return await loop.run_in_executor(
                    self.executor, self._analyze_single_frame, ts, frame, job_id
                )

        results = await asyncio.gather(
            *[analyze_frame(ts, frame) for ts, frame in sampled],
            return_exceptions=True
        )

        findings = []
        seen_fonts = set()
        for result in results:
            if isinstance(result, Exception) or not result:
                continue
            for finding in result:
                font_key = finding.get("title", "").lower()
                if font_key not in seen_fonts:
                    seen_fonts.add(font_key)
                    findings.append(finding)

        logger.info("font_analysis_done", findings=len(findings), job_id=job_id)
        return findings

    def _analyze_single_frame(self, timestamp: float, frame: np.ndarray,
                               job_id: str) -> List[Dict]:
        findings = []
        text_regions = detect_text_regions(frame)

        for region in text_regions:
            roi = region["roi"]
            if roi is None or roi.size == 0:
                continue

            # OCR 사전 필터
            if not self._has_text_pixels(roi):
                continue

            text_data = self._run_ocr(roi)
            if not text_data:
                continue

            # 방법 1: OCR 텍스트에서 폰트명 직접 검색 (가장 정확)
            explicit_font = self._detect_explicit_font_name(text_data)
            if explicit_font:
                matched = self._match_commercial_font(explicit_font)
                if matched and not matched.get("is_free"):
                    risk_score = matched["risk"]
                    findings.append(self._build_finding(
                        job_id, timestamp, explicit_font,
                        matched.get("foundry", ""), risk_score,
                        confidence=0.92,
                        description=f"폰트명 직접 감지: {explicit_font} ({matched.get('foundry', '')})",
                        font_info={"method": "explicit_name", "is_korean": True}
                    ))
                    continue

            # 방법 2: CNN 폰트 분류기 (학습된 모델 있을 때 — 폰트 단위 정밀 식별)
            cnn_finding = self._classify_font_cnn(roi, text_data, timestamp, job_id)
            if cnn_finding is not None:
                findings.append(cnn_finding)
                continue
            if get_font_classifier().available:
                # 분류기가 있고 "상업 폰트 아님/불확실"로 판정 → 휴리스틱 생략
                # (4종 스타일 추정은 분류기보다 거칠어 오탐만 추가)
                continue

            # 방법 3 (폴백): 시각적 스타일 분석 — 분류기 미학습 환경 전용
            font_info = self._identify_font_from_image(roi, text_data)
            if not font_info:
                continue

            font_name = font_info.get("font_name", "").lower()
            if not font_name:
                continue

            matched = self._match_commercial_font(font_name)
            if matched:
                if matched.get("is_free"):
                    continue

                risk_score = matched["risk"]
                if font_info.get("is_korean") and font_info.get("style") == "고딕":
                    risk_score *= 0.85

                # 시각적 분석은 신뢰도 낮음 → 임계값 높임
                if risk_score < config.risk.MEDIUM_THRESHOLD:
                    continue

                display_name = font_info.get("font_name", font_name)
                style_label = font_info.get("style", "")
                findings.append(self._build_finding(
                    job_id, timestamp, display_name,
                    matched.get("foundry", ""), risk_score,
                    confidence=font_info.get("confidence", 0.50),
                    description=(
                        f"상업용 폰트 감지: {display_name}"
                        f"{' (' + style_label + ')' if style_label else ''} "
                        f"({matched.get('foundry', '')})"
                    ),
                    font_info=font_info
                ))

                self.db.learn_from_finding("font", {
                    "font_name": font_name,
                    "foundry": matched.get("foundry", ""),
                    "license_type": "commercial",
                    "requires_license": True,
                })

        return findings

    # 식별 확신 게이트 — 237클래스 모델 실측 보정값 (2026-07 학습본 기준)
    #   측정: 유료 OneShinhan conf 0.41/margin 0.31, Youandi 0.35/0.28
    #         무료 배민주아 0.73/0.60, 미학습 굴림/맑은고딕 저신뢰 또는 무료클래스 오예측
    # 유료: 클래스가 2/237뿐이라 미학습 폰트가 유료로 예측될 확률 극히 낮음(검증 오탐 0)
    #       → 낮은 신뢰도라도 유료 클래스 예측이면 채택 (탐지율 확보)
    _PAID_CONF_MIN   = 0.32
    _PAID_MARGIN_MIN = 0.20
    # 무료 폰트명 단정: 미학습 폰트가 무료 클래스로 그럴듯하게 오예측될 수 있어
    #       (예: 굴림→ChosunGu 0.52) 훨씬 높은 확신 요구 → 아니면 "모름"
    _FREE_CONF_MIN   = 0.62
    _FREE_MARGIN_MIN = 0.38

    def _classify_font_cnn(self, roi: np.ndarray, text_data: List[Dict],
                            timestamp: float, job_id: str) -> Optional[Dict]:
        """
        CNN 폰트 분류기로 자막 폰트 식별.

        출력 정책 (사용자 요구: 학습된 폰트면 정확히, 아니면 '모름'):
          · 확신(conf·margin 통과) + 유료  → 저작권 finding (HIGH), 정확한 폰트명
          · 확신 + 무료                    → 정보 finding (SAFE), "무료 폰트 확인: {명}"
          · 확신 부족 (한글 텍스트는 있음)  → "모름(미학습 폰트)" 정보 finding
          · 텍스트 크롭 없음/분류 불가       → None
        → 확신 없으면 절대 특정 폰트명을 단정하지 않음.
        """
        engine = get_font_classifier()
        if not engine.available:
            return None

        # 한글 텍스트 라인만 (영문 전용 크롭은 한글 폰트 분류 의미 없음)
        crops = []
        for t in text_data:
            if not any('가' <= c <= '힣' for c in t.get("text", "")):
                continue
            bbox = t.get("bbox")
            if not bbox:
                continue
            try:
                # OCR은 preprocess_for_ocr로 1.5배 확대된 이미지에서 수행됨
                # → bbox를 원본 roi 좌표계로 환산
                _s = 1.5
                xs = [int(p[0] / _s) for p in bbox]
                ys = [int(p[1] / _s) for p in bbox]
                pad = 4
                x0, x1 = max(min(xs) - pad, 0), min(max(xs) + pad, roi.shape[1])
                y0, y1 = max(min(ys) - pad, 0), min(max(ys) + pad, roi.shape[0])
                if x1 - x0 >= 24 and y1 - y0 >= 12:
                    crops.append(roi[y0:y1, x0:x1])
            except Exception:
                continue
        if not crops:
            return None

        result = engine.classify_crops(crops[:6])
        if not result:
            return None

        name = result["font_name"]
        conf = result["confidence"]
        margin = result["margin"]
        is_comm = result["is_commercial"]
        paid_ok = is_comm and conf >= self._PAID_CONF_MIN and margin >= self._PAID_MARGIN_MIN
        free_ok = (not is_comm) and conf >= self._FREE_CONF_MIN and margin >= self._FREE_MARGIN_MIN
        logger.debug("font_cnn_result", font=name, conf=conf, margin=margin,
                     commercial=is_comm, paid_ok=paid_ok, free_ok=free_ok)

        # ── 유료 클래스 예측 + 게이트 통과 → 저작권 finding ──
        if paid_ok:
            return self._paid_finding(job_id, timestamp, result)

        # ── 무료 폰트 고신뢰 식별 → 정보 finding ──
        if free_ok:
            return self._build_finding(
                job_id, timestamp, f"무료 폰트: {name}", result["foundry"], 0.03,
                confidence=round(conf, 3),
                description=(
                    f"AI 폰트 식별: {name} (무료 폰트, 신뢰도 {conf:.0%}) "
                    f"— 저작권 위험 없음"
                ),
                font_info={"method": "cnn_classifier", "identified": True, **result},
            )

        # ── 그 외 전부 → "모름" (특정 폰트 단정 금지) ──
        return self._build_finding(
            job_id, timestamp, "모름 (미학습 폰트)", "", 0.0,
            confidence=round(conf, 3),
            description=(
                f"폰트 식별 불가 — 학습된 폰트로 확신할 수 없음 "
                f"(최고 후보 '{name}' 신뢰도 {conf:.0%}). 미학습 폰트로 추정."
            ),
            font_info={"method": "cnn_classifier", "identified": False, **result},
        )

    def _paid_finding(self, job_id, timestamp, result) -> Dict:
        name = result["font_name"]
        conf = result["confidence"]
        self.db.learn_from_finding("font", {
            "font_name": name,
            "foundry": result["foundry"],
            "license_type": "commercial",
            "requires_license": True,
        })
        return self._build_finding(
            job_id, timestamp, f"유료 폰트: {name}", result["foundry"], result["risk"],
            confidence=round(conf, 3),
            description=(
                f"AI 폰트 식별: {name} ({result['foundry']}) — 유료 폰트, "
                f"신뢰도 {conf:.0%}, 텍스트 라인 {result['n_crops']}개 분석. 라이선스 확인 필요."
            ),
            font_info={"method": "cnn_classifier", "identified": True, **result},
        )

    def _build_finding(self, job_id, timestamp, font_name, foundry,
                        risk_score, confidence, description, font_info=None) -> Dict:
        return {
            "job_id": job_id,
            "finding_type": "font",
            "timestamp_start": timestamp,
            "timestamp_end": timestamp,
            "timestamp_display": self._format_time(timestamp),
            "title": font_name,
            "author": foundry,
            "rights_holder": foundry,
            "source": "font_detection",
            "confidence_score": confidence,
            "risk_score": risk_score,
            "risk_level": self._get_risk_level(risk_score),
            "description": description,
            "raw_response": font_info or {},
        }

    # ─────────────────────────────────────────────
    # 폰트 식별
    # ─────────────────────────────────────────────
    def _detect_explicit_font_name(self, text_data: List[Dict]) -> Optional[str]:
        """
        OCR 텍스트에서 폰트명 직접 감지
        예: "Font: 윤고딕", "사용 폰트: 나눔고딕", "tvn즐거운이야기체"
        """
        all_text = " ".join(t.get("text", "") for t in text_data).lower()
        for font_name in self._known_commercial_fonts:
            if font_name in all_text and len(font_name) >= 3:
                return font_name
        return None

    def _identify_font_from_image(self, image: np.ndarray,
                                   text_data: List[Dict]) -> Optional[Dict]:
        if not text_data:
            return None
        all_text = " ".join(t.get("text", "") for t in text_data)
        has_korean = any('가' <= c <= '힣' for c in all_text)
        if has_korean:
            return self._identify_korean_font(image, all_text, text_data)
        else:
            return self._identify_latin_font(image, text_data)

    def _identify_korean_font(self, image: np.ndarray,
                               text: str, text_data: List[Dict]) -> Optional[Dict]:
        """
        획 굵기 변동계수(CV) + 수평수직 투영 비율 + 엣지 밀도로 스타일 분류
        → 스타일 카테고리로 상업용 여부 추정
        """
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
        h, w = gray.shape[:2]
        if h * w == 0:
            return None

        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        h_profile = np.sum(binary > 0, axis=1).astype(float)
        v_profile = np.sum(binary > 0, axis=0).astype(float)

        h_nz = h_profile[h_profile > 0]
        v_nz = v_profile[v_profile > 0]

        if len(h_nz) < 5 or len(v_nz) < 5:
            # 데이터 부족 → 고딕으로 기본 처리
            style = "고딕"
            risk = KOREAN_STYLE_RISK[style]
            label = KOREAN_STYLE_LABEL[style]
            return {
                "font_name": label,
                "is_korean": True, "style": style,
                "confidence": 0.30, "method": "insufficient_data",
                "text_sample": text[:20],
            }

        h_cv = float(np.std(h_nz) / np.mean(h_nz)) if np.mean(h_nz) > 0 else 0
        v_cv = float(np.std(v_nz) / np.mean(v_nz)) if np.mean(v_nz) > 0 else 0
        stroke_cv = (h_cv + v_cv) / 2
        hv_ratio  = float(np.mean(h_nz)) / float(np.mean(v_nz)) if np.mean(v_nz) > 0 else 1.0

        edges = cv2.Canny(gray, 30, 100)
        edge_density = float(np.sum(edges > 0)) / (h * w)

        # 스타일 분류
        if stroke_cv > 0.45 or (hv_ratio < 0.65 and edge_density > 0.10):
            style = "명조"
            confidence = 0.65
        elif stroke_cv > 0.40 and edge_density > 0.13:
            style = "장식체"
            confidence = 0.60
        elif stroke_cv > 0.35:
            style = "손글씨"
            confidence = 0.55
        else:
            style = "고딕"
            confidence = 0.50

        risk = KOREAN_STYLE_RISK[style]
        label = KOREAN_STYLE_LABEL[style]

        return {
            "font_name": label,
            "is_korean": True,
            "style": style,
            "stroke_cv": round(stroke_cv, 3),
            "edge_density": round(edge_density, 3),
            "hv_ratio": round(hv_ratio, 3),
            "text_sample": text[:20],
            "confidence": confidence,
            "method": "korean_stroke_analysis",
        }

    def _identify_latin_font(self, image: np.ndarray,
                              text_data: List[Dict]) -> Optional[Dict]:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
        h, w = gray.shape[:2]
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / (h * w) if h * w > 0 else 0
        binary = (gray < 128).astype(np.uint8)
        stroke_ratio = np.sum(binary) / (h * w) if h * w > 0 else 0

        if edge_density > 0.20:
            font_name = "Bodoni" if stroke_ratio > 0.35 else "Times New Roman"
        else:
            font_name = "Helvetica" if stroke_ratio > 0.35 else "Futura"

        return {
            "font_name": font_name,
            "is_korean": False,
            "is_serif": edge_density > 0.15,
            "is_bold": stroke_ratio > 0.35,
            "text_sample": text_data[0]["text"][:20] if text_data else "",
            "confidence": 0.50,
            "method": "latin_feature_analysis",
        }

    # ─────────────────────────────────────────────
    # OCR
    # ─────────────────────────────────────────────
    def _has_text_pixels(self, image: np.ndarray) -> bool:
        """텍스트 가능성 사전 체크"""
        if image is None or image.size == 0:
            return False
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
        if float(gray.std()) < 12.0:
            return False
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        dark_ratio = float(np.sum(binary == 0)) / max(binary.size, 1)
        return 0.02 <= dark_ratio <= 0.65

    def _run_ocr(self, image: np.ndarray) -> List[Dict]:
        """공유 OCR 싱글톤 사용"""
        try:
            processed = preprocess_for_ocr(image)
            results = safe_ocr(processed, detail=1, min_confidence=0.4)
            return [{"text": text, "confidence": conf, "bbox": bbox}
                    for bbox, text, conf in results]
        except Exception as e:
            logger.debug("ocr_error", error=str(e))
            return []

    # ─────────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────────
    def _match_commercial_font(self, font_name: str) -> Optional[Dict]:
        font_lower = font_name.lower()
        for key, info in self._known_commercial_fonts.items():
            if key in font_lower or font_lower in key:
                return info
        return None

    def _get_risk_level(self, score: float) -> str:
        if score >= config.risk.HIGH_THRESHOLD:
            return "HIGH"
        elif score >= config.risk.MEDIUM_THRESHOLD:
            return "MEDIUM"
        elif score >= config.risk.LOW_THRESHOLD:
            return "LOW"
        return "SAFE"

    def _format_time(self, seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
