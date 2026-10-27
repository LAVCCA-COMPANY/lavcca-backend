"""
utils/google_vision_searcher.py - Google Vision API 역이미지 검색
비용: 무료 1,000회/월 → 이후 $1.50/1,000회

사용 조건:
  .env에 GOOGLE_API_KEY=<your_key> 설정
  Google Cloud Console → Vision API 활성화 필요

동작 전략:
  CLIP/FFT로 의심스러운 프레임만 선별 → Google Vision Web Detection으로 정확한 출처 확인
  Getty/Shutterstock → HIGH 위험도 + 원본 URL 반환
  Unsplash/Pixabay   → SAFE (무료 이미지)
  완전 일치 이미지 발견 시 → 미식별이어도 MEDIUM 위험도 상향

개선사항 (v2):
  1. 도메인 가중 매칭: URL 도메인(호스트명)에서 키워드 발견 = 가장 신뢰도 높음
  2. 단어 경계 매칭: 짧은 키워드(≤5자)는 \b 단어경계 적용 → 오탐 방지
     ex) "sbs" 가 "subscribe" URL에 매칭되지 않음
  3. 출처별 가중치: full_match_domain > full_match_path > partial_domain > page_domain > text
  4. TEXT_DETECTION 병행: Vision API OCR로 이미지 내 저작권 텍스트 직접 추출
  5. phash 캐시: 동일/유사 프레임 재검색 방지 (API 비용 절감 + 일관성)
"""
import re
import base64
import threading
import cv2
import numpy as np
import structlog
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from config import config

logger = structlog.get_logger()

VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"

# ─────────────────────────────────────────────
# 저작권 보유자 키워드 매핑
# format: "검색 키워드" → (rights_holder, risk_level, risk_score, is_free)
# ─────────────────────────────────────────────
_RIGHTS_MAP: Dict[str, tuple] = {
    # ── 스톡 이미지/영상 서비스 (HIGH 위험) ──
    "gettyimages":      ("Getty Images",           "HIGH", 0.93, False),
    "getty images":     ("Getty Images",           "HIGH", 0.93, False),
    "shutterstock":     ("Shutterstock",           "HIGH", 0.92, False),
    "istockphoto":      ("iStock (Getty Images)",  "HIGH", 0.91, False),
    "istock":           ("iStock (Getty Images)",  "HIGH", 0.91, False),
    "stock.adobe":      ("Adobe Stock",            "HIGH", 0.90, False),
    "adobestock":       ("Adobe Stock",            "HIGH", 0.90, False),
    "adobe stock":      ("Adobe Stock",            "HIGH", 0.90, False),
    "alamy":            ("Alamy",                  "HIGH", 0.89, False),
    "123rf":            ("123RF",                  "HIGH", 0.86, False),
    "depositphotos":    ("Depositphotos",          "HIGH", 0.85, False),
    "dreamstime":       ("Dreamstime",             "HIGH", 0.84, False),
    "bigstockphoto":    ("Bigstock",               "HIGH", 0.82, False),
    "pond5":            ("Pond5",                  "HIGH", 0.82, False),
    "storyblocks":      ("Storyblocks",            "HIGH", 0.82, False),
    "videoblocks":      ("Storyblocks",            "HIGH", 0.82, False),
    "motionarray":      ("Motion Array",           "HIGH", 0.80, False),
    "envato":           ("Envato Elements",        "HIGH", 0.80, False),
    "elements.envato":  ("Envato Elements",        "HIGH", 0.80, False),
    # ── 뉴스 통신사 ──
    "apimages":         ("Associated Press",       "HIGH", 0.91, False),
    "ap photo":         ("Associated Press",       "HIGH", 0.90, False),
    "associated press": ("Associated Press",       "HIGH", 0.90, False),
    "reuters":          ("Reuters",                "HIGH", 0.90, False),
    "afp":              ("AFP (Agence France)",    "HIGH", 0.89, False),
    "yonhapnews":       ("연합뉴스",                "HIGH", 0.88, False),
    "yonhap":           ("연합뉴스",                "HIGH", 0.88, False),
    "newsis":           ("뉴시스",                  "HIGH", 0.85, False),
    "news1":            ("뉴스1",                   "HIGH", 0.85, False),
    # ── 방송사 / OTT ──
    "netflix":          ("Netflix",                "HIGH", 0.93, False),
    "disney":           ("Disney",                 "HIGH", 0.93, False),
    "disneyplus":       ("Disney+",                "HIGH", 0.93, False),
    "hbo":              ("HBO/Max",                "HIGH", 0.91, False),
    "hulu":             ("Hulu",                   "HIGH", 0.88, False),
    "primevideo":       ("Amazon Prime Video",     "HIGH", 0.88, False),
    "amazon prime":     ("Amazon Prime Video",     "HIGH", 0.88, False),
    "apple.tv":         ("Apple TV+",              "HIGH", 0.88, False),
    "kbs.co.kr":        ("KBS",                    "HIGH", 0.88, False),
    "kbs":              ("KBS",                    "HIGH", 0.87, False),
    "imbc":             ("MBC",                    "HIGH", 0.88, False),
    "mbc":              ("MBC",                    "HIGH", 0.87, False),
    "sbs.co.kr":        ("SBS",                    "HIGH", 0.88, False),
    "sbs":              ("SBS",                    "HIGH", 0.87, False),
    "jtbc":             ("JTBC",                   "HIGH", 0.88, False),
    "tvn":              ("tvN",                    "HIGH", 0.88, False),
    "cjenm":            ("CJ ENM",                 "HIGH", 0.86, False),
    "tving":            ("TVING",                  "HIGH", 0.86, False),
    "wavve":            ("Wavve",                  "HIGH", 0.86, False),
    "seezn":            ("Seezn (KT)",             "HIGH", 0.83, False),
    "cnn":              ("CNN",                    "HIGH", 0.87, False),
    "bbc":              ("BBC",                    "HIGH", 0.87, False),
    # ── 영화/드라마 제작사 ──
    "warnerbros":       ("Warner Bros.",            "HIGH", 0.88, False),
    "warner bros":      ("Warner Bros.",            "HIGH", 0.88, False),
    "universalpictures":("Universal Pictures",      "HIGH", 0.87, False),
    "universal pictures":("Universal Pictures",    "HIGH", 0.87, False),
    "paramount":        ("Paramount Pictures",      "HIGH", 0.87, False),
    "sonypictures":     ("Sony Pictures",           "HIGH", 0.87, False),
    "sony pictures":    ("Sony Pictures",           "HIGH", 0.87, False),
    "20thcentury":      ("20th Century Studios",    "HIGH", 0.86, False),
    "pixar":            ("Pixar/Disney",            "HIGH", 0.92, False),
    "marvel":           ("Marvel/Disney",           "HIGH", 0.92, False),
    "dccomics":         ("DC/Warner Bros.",         "HIGH", 0.90, False),
    "dc comics":        ("DC/Warner Bros.",         "HIGH", 0.90, False),
    "dreamworks":       ("DreamWorks",              "HIGH", 0.86, False),
    "lucasfilm":        ("Lucasfilm/Disney",        "HIGH", 0.91, False),
    "mgm":              ("MGM/Amazon",              "HIGH", 0.86, False),
    "lionsgate":        ("Lionsgate",               "HIGH", 0.85, False),
    "a24":              ("A24",                     "HIGH", 0.84, False),
    # ── 영화/작품 정보 사이트 (출처 확인용) ──
    "imdb":             ("IMDb 등재 작품",           "MEDIUM", 0.62, False),
    "rottentomatoes":   ("Rotten Tomatoes 등재",    "MEDIUM", 0.58, False),
    "letterboxd":       ("Letterboxd 등재",         "MEDIUM", 0.55, False),
    "themoviedb":       ("TMDB 등재 작품",           "MEDIUM", 0.55, False),
    "kobis":            ("영화진흥위원회(KOBIS)",    "HIGH", 0.80, False),
    "hancinema":        ("HanCinema 등재",           "MEDIUM", 0.60, False),
    # ── 한국 음악 레이블 ──
    "hybe":             ("HYBE",                   "HIGH", 0.88, False),
    "big hit":          ("HYBE (Big Hit)",          "HIGH", 0.88, False),
    "smtown":           ("SM Entertainment",        "HIGH", 0.88, False),
    "sm entertainment": ("SM Entertainment",        "HIGH", 0.88, False),
    "jyp":              ("JYP Entertainment",       "HIGH", 0.87, False),
    "jypentertainment": ("JYP Entertainment",       "HIGH", 0.87, False),
    "ygentertainment":  ("YG Entertainment",        "HIGH", 0.87, False),
    "yg entertainment": ("YG Entertainment",        "HIGH", 0.87, False),
    "starship":         ("Starship Entertainment",  "HIGH", 0.85, False),
    "kakao entertainment":("Kakao Entertainment",  "HIGH", 0.86, False),
    # ── 글로벌 음악 레이블 ──
    "universalmusic":   ("Universal Music Group",   "HIGH", 0.87, False),
    "universal music":  ("Universal Music Group",   "HIGH", 0.87, False),
    "sonymusic":        ("Sony Music",              "HIGH", 0.87, False),
    "sony music":       ("Sony Music",              "HIGH", 0.87, False),
    "warnermusic":      ("Warner Music Group",      "HIGH", 0.87, False),
    "warner music":     ("Warner Music Group",      "HIGH", 0.87, False),
    "republicrecords":  ("Republic Records/UMG",    "HIGH", 0.86, False),
    "interscope":       ("Interscope/UMG",          "HIGH", 0.85, False),
    # ── 스포츠 ──
    "nfl":              ("NFL",                    "HIGH", 0.89, False),
    "nba":              ("NBA",                    "HIGH", 0.89, False),
    "mlb":              ("MLB",                    "HIGH", 0.88, False),
    "nhl":              ("NHL",                    "HIGH", 0.88, False),
    "fifa":             ("FIFA",                   "HIGH", 0.88, False),
    "espn":             ("ESPN/Disney",             "HIGH", 0.88, False),
    "kbo":              ("KBO (한국야구위원회)",     "HIGH", 0.86, False),
    "kleague":          ("K리그",                   "HIGH", 0.85, False),
    "k league":         ("K리그",                   "HIGH", 0.85, False),
    "pga tour":         ("PGA Tour",               "HIGH", 0.85, False),
    # (의도적 제외) youtube / spotify / melon / bugs 등 범용 플랫폼 도메인:
    #   페이지 URL 매칭은 "그 플랫폼 어딘가에 존재한다"는 뜻일 뿐 권리자 식별이 아님.
    #   특히 본인이 업로드한 영상도 YouTube 페이지에서 발견되므로 오탐의 주원인이었음.
    # ── 게임 회사 / 플랫폼 ──
    "steampowered":     ("Valve/Steam",                    "HIGH", 0.85, False),
    "nintendo":         ("Nintendo",                        "HIGH", 0.92, False),
    "playstation":      ("PlayStation/Sony",                "HIGH", 0.90, False),
    "riotgames":        ("Riot Games",                      "HIGH", 0.87, False),
    "leagueoflegends":  ("Riot Games (League of Legends)",  "HIGH", 0.87, False),
    "valorant":         ("Riot Games (Valorant)",           "HIGH", 0.86, False),
    "blizzard":         ("Blizzard Entertainment",          "HIGH", 0.88, False),
    "battle.net":       ("Blizzard Entertainment",          "HIGH", 0.88, False),
    "ea.com":           ("Electronic Arts",                 "HIGH", 0.87, False),
    "epicgames":        ("Epic Games",                      "HIGH", 0.86, False),
    "fortnite":         ("Epic Games (Fortnite)",           "HIGH", 0.86, False),
    "rockstargames":    ("Rockstar Games",                  "HIGH", 0.89, False),
    "rockstar games":   ("Rockstar Games",                  "HIGH", 0.89, False),
    "activision":       ("Activision Blizzard",             "HIGH", 0.86, False),
    "ubisoft":          ("Ubisoft",                         "HIGH", 0.85, False),
    "capcom":           ("Capcom",                          "HIGH", 0.86, False),
    "bandainamco":      ("Bandai Namco",                    "HIGH", 0.85, False),
    "bandai namco":     ("Bandai Namco",                    "HIGH", 0.85, False),
    "fromsoftware":     ("FromSoftware",                    "HIGH", 0.85, False),
    "squareenix":       ("Square Enix",                     "HIGH", 0.86, False),
    "square enix":      ("Square Enix",                     "HIGH", 0.86, False),
    "konami":           ("Konami",                          "HIGH", 0.84, False),
    "bethesda":         ("Bethesda/Microsoft",              "HIGH", 0.85, False),
    "nexon.com":        ("Nexon",                           "HIGH", 0.85, False),
    "krafton":          ("KRAFTON",                         "HIGH", 0.84, False),
    "pubg":             ("KRAFTON (PUBG)",                  "HIGH", 0.86, False),
    "smilegate":        ("Smilegate",                       "HIGH", 0.83, False),
    "mihoyo":           ("miHoYo/HoYoverse",                "HIGH", 0.86, False),
    "hoyoverse":        ("HoYoverse",                       "HIGH", 0.86, False),
    "genshinimpact":    ("miHoYo (Genshin Impact)",         "HIGH", 0.87, False),
    "supercell":        ("Supercell",                       "HIGH", 0.84, False),
    "mojang":           ("Mojang/Microsoft",                "HIGH", 0.88, False),
    "minecraft":        ("Mojang/Microsoft (Minecraft)",    "HIGH", 0.88, False),
    "xbox":             ("Xbox/Microsoft",                  "HIGH", 0.88, False),
    # ── 애니메이션 / 만화 / 웹툰 ──
    "crunchyroll":      ("Crunchyroll/Sony",                "HIGH", 0.90, False),
    "funimation":       ("Funimation/Sony",                 "HIGH", 0.88, False),
    "aniplex":          ("Aniplex",                         "HIGH", 0.88, False),
    "toei":             ("Toei Animation",                  "HIGH", 0.89, False),
    "shueisha":         ("Shueisha",                        "HIGH", 0.90, False),
    "viz.com":          ("VIZ Media",                       "HIGH", 0.88, False),
    "viz media":        ("VIZ Media",                       "HIGH", 0.88, False),
    "kodansha":         ("Kodansha",                        "HIGH", 0.88, False),
    "kadokawa":         ("KADOKAWA",                        "HIGH", 0.87, False),
    "mangaplus":        ("Shueisha/MANGA Plus",             "HIGH", 0.88, False),
    "manga plus":       ("Shueisha/MANGA Plus",             "HIGH", 0.88, False),
    "webtoon":          ("WEBTOON/NAVER",                   "HIGH", 0.86, False),
    "webtoons.com":     ("WEBTOON/NAVER",                   "HIGH", 0.86, False),
    "lezhin":           ("Lezhin Comics",                   "HIGH", 0.85, False),
    "kakaopage":        ("Kakao Page",                      "HIGH", 0.85, False),
    "ghibli":           ("Studio Ghibli",                   "HIGH", 0.91, False),
    "kyoto animation":  ("Kyoto Animation",                 "HIGH", 0.89, False),
    "mappa":            ("MAPPA",                           "HIGH", 0.87, False),
    "wit studio":       ("WIT Studio",                      "HIGH", 0.86, False),
    "ufotable":         ("ufotable",                        "HIGH", 0.86, False),
    "comixology":       ("ComiXology/Amazon",               "HIGH", 0.84, False),
    "tapas.io":         ("Tapas Media",                     "HIGH", 0.82, False),
    "fandom.com":       ("Fandom 등재 작품",                "MEDIUM", 0.55, False),
    # ── 무료 이미지 (SAFE/LOW) ──
    "wikimedia":        ("Wikimedia Commons",      "SAFE",  0.05, True),
    "wikipedia":        ("Wikipedia",              "SAFE",  0.05, True),
    "pixabay":          ("Pixabay",                "SAFE",  0.05, True),
    "unsplash":         ("Unsplash",               "SAFE",  0.05, True),
    "pexels":           ("Pexels",                 "SAFE",  0.05, True),
    "rawpixel":         ("Rawpixel",               "SAFE",  0.08, True),
    "libreshot":        ("Libreshot",              "SAFE",  0.05, True),
    "freepik":          ("Freepik (확인 필요)",     "LOW",   0.25, False),
    "creative commons": ("Creative Commons",        "LOW",   0.15, False),
    "cc0":              ("CC0 (공개 도메인)",        "SAFE",  0.03, True),
}

# 이미지 내 저작권 텍스트 패턴 (Vision OCR에서 발견 시 확실한 근거)
# Pattern → (rights_holder, extra_score_boost)
_COPYRIGHT_TEXT_PATTERNS = [
    (r'\©\s*\d{4}\s*(getty|shutterstock|reuters|ap\s+photo|afp)',  0.10),
    (r'getty\s+images',     0.08),
    (r'shutterstock\.com',  0.08),
    (r'istockphoto\.com',   0.07),
    (r'©\s*(kbs|mbc|sbs|jtbc|tvn)',  0.07),
    (r'all\s+rights\s+reserved',     0.03),
    (r'copyright\s+\d{4}',           0.02),
]

# 저작권자 이름 집합 (detected_entity 추출 시 제외 대상)
_RIGHTS_NAMES_LOWER = {v[0].lower() for v in _RIGHTS_MAP.values()}

# ─────────────────────────────────────────────
# 자기 채널 필터 (본인 유튜브 영상 오탐 방지)
# ─────────────────────────────────────────────
_OWN_CHANNELS: set = set()
_OWN_DOMAINS: set = set()


def set_own_domains(domains) -> None:
    """
    본인 소유 웹사이트/블로그/포트폴리오 도메인 등록.

    크리에이터가 자기 이미지를 자기 사이트에도 올린 경우, Vision이 그걸
    찾아 "인터넷에서 발견됨 → 저작권 위반"으로 오탐하는 것을 방지한다.
    (기존 set_own_channels는 YouTube만 커버 → 블로그/인스타 등은 못 막았음)

    Args:
        domains: 도메인 목록 (예: ["myblog.com", "myname.tistory.com"])
    """
    global _OWN_DOMAINS
    out = set()
    for d in domains or []:
        d = d.strip().lower()
        if not d:
            continue
        # URL이 들어와도 도메인만 추출
        if "://" in d or "/" in d:
            d = _domain_of(d) or d.split("/")[0]
        if d.startswith("www."):
            d = d[4:]
        if d:
            out.add(d)
    _OWN_DOMAINS = out
    if _OWN_DOMAINS:
        logger.info("own_domains_registered",
                    count=len(_OWN_DOMAINS), domains=sorted(_OWN_DOMAINS)[:3])


def clear_own_domains() -> None:
    global _OWN_DOMAINS
    _OWN_DOMAINS = set()


def _all_matches_are_own_domains(full_urls: List[str], page_urls: List[str]) -> bool:
    """
    이미지가 발견된 모든 출처가 본인 도메인뿐이면 True.
    → 본인이 자기 콘텐츠를 자기 사이트에만 올린 것 = 저작권 위반 아님.
    타 사이트에도 하나라도 있으면 False (남이 퍼간 것일 수 있으므로 정상 판정).
    """
    if not _OWN_DOMAINS:
        return False
    urls = [u for u in (list(full_urls) + list(page_urls)) if u]
    if not urls:
        return False
    for u in urls:
        dom = _domain_of(u)
        # 본인 도메인 또는 그 서브도메인이 아닌 출처가 하나라도 있으면 → 본인것 아님
        if not any(dom == od or dom.endswith("." + od) for od in _OWN_DOMAINS):
            return False
    return True


def set_own_channels(channels) -> None:
    """
    본인 YouTube 채널명/핸들 등록.
    Vision API / YouTube API 결과에서 본인 채널이 저작권 위반으로
    오탐되는 것을 방지한다.

    Args:
        channels: 채널명 목록 (예: ["MyChannel", "@myhandle", "채널이름"])
    """
    global _OWN_CHANNELS
    _OWN_CHANNELS = {ch.strip().lower().lstrip('@') for ch in channels if ch.strip()}
    if _OWN_CHANNELS:
        logger.info("own_channels_registered",
                    count=len(_OWN_CHANNELS),
                    channels=sorted(_OWN_CHANNELS)[:3])


def clear_own_channels() -> None:
    """등록된 자기 채널 초기화"""
    global _OWN_CHANNELS
    _OWN_CHANNELS = set()


def _is_own_youtube_content(page_urls: List[str], entities: List[str]) -> bool:
    """
    Vision 결과가 본인 채널 콘텐츠를 가리키는지 확인.

    판단 기준:
    1. YouTube 페이지 URL에 채널 핸들이 포함됨
       예: youtube.com/@mychannel → ch='mychannel' → True
    2. Vision 엔티티 텍스트에 채널명이 포함됨
       예: entity='MyChannel Gaming' → ch='mychannel' → True
    """
    if not _OWN_CHANNELS:
        return False

    # YouTube 관련 URL만 추출
    yt_pages = [
        u for u in page_urls
        if "youtube.com" in u.lower() or "youtu.be" in u.lower()
    ]
    yt_text  = " ".join(yt_pages).lower()
    ent_text = " ".join(e.lower() for e in entities)

    for ch in _OWN_CHANNELS:
        ch_esc = re.escape(ch)
        # URL에서 @채널핸들 또는 /채널명 패턴 매칭
        if yt_text and re.search(r"[@/]" + ch_esc + r"(?:[/?&\s]|$)", yt_text):
            return True
        # 엔티티 텍스트 단어 경계 매칭
        if ent_text and re.search(
            r"(?<![a-z0-9가-힣])" + ch_esc + r"(?![a-z0-9가-힣])", ent_text
        ):
            return True
    return False


# ─────────────────────────────────────────────
# 매칭 헬퍼 함수
# ─────────────────────────────────────────────

def _domain_of(url: str) -> str:
    """URL에서 등록 도메인(호스트명) 추출 (www. 제거)"""
    try:
        netloc = urlparse(url.strip()).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _kw_in_text(kw: str, text: str) -> bool:
    """
    키워드를 텍스트에서 검색.

    핵심 규칙:
    - 5자 이하 단어: \b 단어 경계 필요
      → "sbs" 가 "subscribe"에 매칭되지 않음
      → "mbc" 가 "combat" URL에 매칭되지 않음
      → 단, URL에서 "-sbs-" / ".sbs." 형태는 정상 매칭
    - 6자 이상: 부분 문자열 허용 (shutterstock, netflix 등은 오탐 없음)
    """
    if not kw or not text:
        return False
    kw_escaped = re.escape(kw)
    if len(kw.replace(" ", "")) <= 5:
        return bool(re.search(r'(?<![a-z0-9])' + kw_escaped + r'(?![a-z0-9])', text, re.IGNORECASE))
    return kw in text


def _kw_match_weight(
    keyword: str,
    full_urls: List[str],
    partial_urls: List[str],
    page_urls: List[str],
    entity_text: str,
    ocr_text: str = "",
) -> Tuple[float, Optional[str]]:
    """
    키워드 ↔ Vision 결과 매칭 가중치 계산.

    매칭 신뢰도 계층 (높을수록 확실):
      1.00  full_match URL 도메인에 키워드 포함 (이미지 출처 도메인 일치)
      0.90  full_match URL 경로에 키워드 포함  (단어 경계)
      0.85  partial_match URL 도메인
      0.75  page URL 도메인  (이미지를 담은 페이지의 도메인)
      0.70  partial_match URL 경로 (단어 경계)
      0.55  page URL 경로    (단어 경계, 가장 낮음: 페이지≠이미지 소유자)
      0.60  Vision 엔티티 텍스트 (단어 경계)
      0.85  Vision OCR 이미지 내 텍스트 (이미지 안에 직접 쓰여 있음 → 높은 신뢰)

    Returns:
        (weight: float, matched_url: str|None)
    """
    kw = keyword.lower()
    best_w = 0.0
    matched_url: Optional[str] = None

    # ── OCR 텍스트 (이미지 내 텍스트): 신뢰도 높음 ──
    if ocr_text and _kw_in_text(kw, ocr_text):
        best_w = max(best_w, 0.85)

    # ── Full match URLs (동일 이미지) ──
    for url in full_urls:
        domain = _domain_of(url)
        if domain and kw in domain:
            return 1.00, url   # 즉시 반환: 가장 확실한 증거
        if _kw_in_text(kw, url.lower()):
            if best_w < 0.90:
                best_w = 0.90
                matched_url = url

    # ── Partial match URLs (유사 이미지) ──
    for url in partial_urls:
        domain = _domain_of(url)
        if domain and kw in domain:
            w = 0.85
        elif _kw_in_text(kw, url.lower()):
            w = 0.70
        else:
            continue
        if w > best_w:
            best_w = w
            matched_url = url

    # ── Page URLs (이미지를 포함한 페이지) ──
    for url in page_urls:
        domain = _domain_of(url)
        if domain and kw in domain:
            w = 0.75
        elif _kw_in_text(kw, url.lower()):
            w = 0.55   # 페이지 경로 매칭: 이미지 소유자 ≠ 페이지 운영자 가능
        else:
            continue
        if w > best_w:
            best_w = w
            matched_url = url

    # ── 엔티티 텍스트 (Vision이 인식한 개체명) ──
    if entity_text and _kw_in_text(kw, entity_text):
        best_w = max(best_w, 0.60)

    return best_w, matched_url


# ─────────────────────────────────────────────
# 역검색 엔진
# ─────────────────────────────────────────────
class GoogleVisionSearcher:
    """
    Google Vision API Web Detection + Text Detection 역이미지 검색기.
    API key 방식 (서비스 계정 불필요).

    v2 개선:
    - WEB_DETECTION + TEXT_DETECTION 동시 요청 (비용 동일)
    - phash 기반 결과 캐시 (유사 프레임 재사용)
    """

    def __init__(self, api_key: str = ""):
        self._api_key = api_key.strip()
        self._enabled = bool(self._api_key)
        self._cache: Dict[str, Optional[Dict]] = {}
        self._cache_lock = threading.Lock()
        self._call_count = 0          # 작업당 실제 HTTP 호출 수 (캐시 히트 제외)
        self._budget_warned = False
        if not self._enabled:
            logger.debug("google_vision_searcher_disabled",
                         reason="GOOGLE_API_KEY not set in .env")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── 캐시 관리 ──────────────────────────────
    def clear_cache(self) -> None:
        """새 분석 작업 시작 전 캐시 + 호출 예산 초기화"""
        with self._cache_lock:
            self._cache.clear()
            self._call_count = 0
            self._budget_warned = False
        logger.debug("vision_cache_cleared")

    def _cache_get(self, frame: np.ndarray) -> Tuple[bool, Optional[Dict]]:
        """phash 유사도 기반 캐시 조회"""
        ph = self._phash(frame)
        if not ph:
            return False, None
        with self._cache_lock:
            if ph in self._cache:
                logger.debug("vision_cache_hit_exact", ph=ph[:8])
                return True, self._cache[ph]
            # 유사 프레임 검색 (Hamming distance ≤ 8)
            ph_int = int(ph, 16)
            for cached_ph, result in self._cache.items():
                try:
                    dist = bin(ph_int ^ int(cached_ph, 16)).count("1")
                    if dist <= 8:
                        logger.debug("vision_cache_hit_similar", dist=dist)
                        self._cache[ph] = result   # 동일 결과로 등록
                        return True, result
                except Exception:
                    pass
        return False, None

    def _cache_set(self, frame: np.ndarray, result: Optional[Dict]) -> None:
        ph = self._phash(frame)
        if not ph:
            return
        with self._cache_lock:
            if len(self._cache) >= 400:
                # FIFO 방식: 가장 오래된 항목 50개 제거
                for key in list(self._cache.keys())[:50]:
                    del self._cache[key]
            self._cache[ph] = result

    @staticmethod
    def _phash(frame: np.ndarray) -> str:
        """프레임의 perceptual hash (16진수 문자열)"""
        try:
            from utils.video_utils import compute_phash
            return compute_phash(frame)
        except Exception:
            try:
                import hashlib
                small = cv2.resize(frame, (16, 16))
                return hashlib.md5(small.tobytes()).hexdigest()
            except Exception:
                return ""

    # ── 공개 API ─────────────────────────────
    def search(self, frame: np.ndarray, max_size: int = 800) -> Optional[Dict]:
        """
        이미지 역검색 → 저작권 출처 식별.

        v2: WEB_DETECTION + TEXT_DETECTION 동시 요청, phash 캐시 적용.

        Returns:
            None  — API key 없거나 요청 실패
            Dict  — {rights_holder, risk_level, risk_score, source_url,
                     web_entities, full_matches, partial_matches, pages,
                     is_free, detected_entity, entity_conf, ocr_copyright}
        """
        if not self._enabled or frame is None or frame.size == 0:
            return None

        # 캐시 확인
        hit, cached = self._cache_get(frame)
        if hit:
            return cached

        # 작업당 호출 예산 확인 (비용 상한 — 캐시 히트는 무제한)
        with self._cache_lock:
            if self._call_count >= config.pipeline.vision_max_calls_per_job:
                if not self._budget_warned:
                    self._budget_warned = True
                    logger.warning("vision_budget_exhausted",
                                   limit=config.pipeline.vision_max_calls_per_job)
                return None
            self._call_count += 1

        b64 = _encode_frame(frame, max_size)
        payload = {
            "requests": [{
                "image": {"content": b64},
                "features": [
                    {"type": "WEB_DETECTION",  "maxResults": 10},
                    {"type": "TEXT_DETECTION",  "maxResults": 1},   # 이미지 내 저작권 텍스트
                ],
            }]
        }

        try:
            import requests as req_lib
            # 키는 헤더로 보낸다: URL 에 있으면 requests 예외 메시지(=로그)에 그대로 찍힌다
            resp = req_lib.post(
                VISION_API_URL,
                headers={"x-goog-api-key": self._api_key},
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if status == 429:
                logger.warning("google_vision_quota_exceeded")
            elif status == 400:
                logger.warning("google_vision_bad_request", detail=str(e)[:120])
            else:
                logger.error("google_vision_request_failed", error=str(e)[:120])
            return None

        result = _parse_web_detection(data)
        self._cache_set(frame, result)
        return result

    def detect_logos(self, image_bytes: bytes, max_results: int = 5) -> List[Dict]:
        """
        LOGO_DETECTION — 브랜드 로고 감지 (search()와 동일한 images:annotate 엔드포인트).
        서비스계정(google-credentials.json) 대신 GOOGLE_API_KEY(REST)로 인증하여
        Vision 인증 방식을 하나로 일원화한다.
        반환: [{"description": 브랜드명, "score": 신뢰도(0~1)}, ...]
              (위험도 매핑·임계값은 호출자 정책에 위임)
        """
        if not self._enabled or not image_bytes:
            return []

        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "requests": [{
                "image":    {"content": b64},
                "features": [{"type": "LOGO_DETECTION", "maxResults": max_results}],
            }]
        }

        try:
            import requests as req_lib
            resp = req_lib.post(
                VISION_API_URL,
                headers={"x-goog-api-key": self._api_key},
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if status == 429:
                logger.warning("google_vision_quota_exceeded")
            elif status == 400:
                logger.warning("google_vision_bad_request", detail=str(e)[:120])
            else:
                logger.error("google_vision_logo_request_failed", error=str(e)[:120])
            return []

        try:
            annotations = (data.get("responses") or [{}])[0].get("logoAnnotations") or []
        except (AttributeError, IndexError, TypeError):
            return []
        return [
            {"description": a.get("description", ""), "score": float(a.get("score", 0.0) or 0.0)}
            for a in annotations
            if a.get("description")
        ]


# ─────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────
def _encode_frame(frame: np.ndarray, max_size: int) -> str:
    h, w = frame.shape[:2]
    if max(h, w) > max_size:
        scale = max_size / max(h, w)
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def _parse_web_detection(data: dict) -> Optional[Dict]:
    """
    Vision API 응답 파싱 → 저작권 정보 추출 (v2)

    매칭 전략:
    1. URL 도메인 우선 (full_match > partial > page)
    2. 단어 경계 적용 (짧은 키워드 오탐 방지)
    3. TEXT_DETECTION OCR 텍스트에서 저작권 직접 추출
    4. 증거 가중치 누적 (도메인 1.0 > 경로 0.9 > 페이지 0.75 > 텍스트 0.6)
    """
    try:
        response = data["responses"][0]
        web = response.get("webDetection", {})
    except (KeyError, IndexError):
        return None

    # ── 웹 감지 결과 파싱 ──
    entity_list = [
        (e.get("description", ""), e.get("score", 0.0))
        for e in web.get("webEntities", [])
    ]
    entities: List[str] = [name for name, _ in entity_list]

    full_urls    = [m.get("url", "") for m in web.get("fullMatchingImages", [])]
    partial_urls = [m.get("url", "") for m in web.get("partialMatchingImages", [])]
    page_urls    = [p.get("url", "") for p in web.get("pagesWithMatchingImages", [])]

    n_full    = len(full_urls)
    n_partial = len(partial_urls)

    entity_text = " ".join(entities).lower()

    # ── TEXT_DETECTION OCR 텍스트 파싱 ──
    ocr_text = ""
    text_annotations = response.get("textAnnotations", [])
    if text_annotations:
        ocr_text = text_annotations[0].get("description", "").lower()

    # ── 1단계: 저작권자 키워드 매칭 (가중치 누적) ──
    best_holder: Optional[str] = None
    best_level  = "SAFE"
    best_score  = 0.0
    best_free   = False
    best_weight = 0.0   # 매칭 품질 추적 (권리자 특정 신뢰도 판단용)
    source_url: Optional[str] = None

    for keyword, (name, level, score, is_free) in _RIGHTS_MAP.items():
        w, url_hit = _kw_match_weight(
            keyword, full_urls, partial_urls, page_urls, entity_text, ocr_text
        )
        if w <= 0:
            continue

        effective = score * w

        # 가중치에 따라 위험 레벨 하향 조정
        # 낮은 신뢰도 매칭 (페이지 경로, 엔티티 텍스트)이 HIGH를 유지하지 않도록
        if w >= 0.85:
            adjusted_level = level              # 도메인 매칭 등 고신뢰 → 원래 레벨 유지
        elif w >= 0.70:
            adjusted_level = "MEDIUM" if level == "HIGH" else level   # HIGH → MEDIUM
        else:
            _level_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "SAFE": 0}
            _max_low = min(_level_order.get(level, 3), 1)   # LOW가 최대
            adjusted_level = ["SAFE", "LOW", "MEDIUM", "HIGH"][_max_low]

        if effective > best_score:
            best_holder = name
            best_level  = adjusted_level
            best_score  = effective
            best_free   = is_free
            best_weight = w   # 해당 매칭의 실제 가중치 기록
            source_url  = url_hit

    # ── 2단계: OCR 텍스트에서 저작권 패턴 직접 추출 ──
    # 이미지 안에 "@GettyImages", "© Reuters" 등이 직접 쓰여 있으면 가장 확실
    ocr_copyright: Optional[str] = None
    if ocr_text:
        for pattern, boost in _COPYRIGHT_TEXT_PATTERNS:
            m = re.search(pattern, ocr_text, re.IGNORECASE)
            if m:
                ocr_copyright = m.group(0).strip()
                # 패턴 매칭 시 점수 상향 (명시적 저작권 텍스트)
                best_score = min(best_score + boost, 0.98)
                logger.debug("ocr_copyright_found", text=repr(ocr_copyright[:40]))
                break

    # ── 2-b단계: 저신뢰 매칭 필터 (OCR 확인 이후) ──
    #
    # 문제: 엔티티 텍스트나 페이지 경로만으로 권리자를 특정하면 오탐률이 높다.
    #   예) Cesky Terrier 이미지가 Wikipedia 페이지에 등장
    #       → page_url 도메인 'wikipedia' 매칭(w=0.75) → 엉뚱하게 "Wikipedia" 반환
    #   예) 일반 이미지가 어떤 뉴스 페이지에 등장, URL에 우연히 'kbs' 포함
    #       → 엉뚱하게 "KBS" 반환
    #
    # 규칙:
    #   • 비무료(저작권 있는) 이미지: URL 도메인에서 직접 확인 (weight ≥ 0.70)
    #   • 무료(is_free) 이미지 소스: 이미지 파일 URL에서 직접 확인 (weight ≥ 0.85)
    #     → 페이지 URL에만 등장하면 "그 페이지가 참조했을 뿐" = 원본 출처 불명
    #   • OCR로 이미지 내 저작권 텍스트 발견 시 → 가장 확실, 필터 예외 처리
    _MIN_WEIGHT_FOR_HOLDER = 0.70   # 비무료: page URL 도메인 이상 필요
    _MIN_WEIGHT_FOR_FREE   = 0.85   # 무료:   full/partial URL에서 직접 확인 필요

    if not ocr_copyright:
        if best_free and best_weight < _MIN_WEIGHT_FOR_FREE:
            # 무료 이미지 출처 미확인 → 이미지 파일 URL에서 확인 안 됨
            logger.debug(
                "vision_free_source_unconfirmed",
                holder=best_holder,
                weight=round(best_weight, 2),
                reason="free_source_only_in_page_url_not_image_url",
            )
            best_holder = None
            best_free   = False
            if n_full > 0:
                best_level = "MEDIUM"
                best_score = 0.38
            else:
                best_level = "SAFE"
                best_score = 0.0

        elif not best_free and best_weight < _MIN_WEIGHT_FOR_HOLDER:
            # 비무료: URL 수준 증거 없이 권리자 특정 불가
            if best_holder:
                logger.debug(
                    "vision_holder_confidence_too_low",
                    holder=best_holder,
                    weight=round(best_weight, 2),
                    reason="no_url_level_match__entity_text_or_page_path_only",
                )
            best_holder = None
            if n_full > 0:
                best_level = "MEDIUM"   # 동일 이미지가 인터넷에 있음 → 주의 수준
                best_score = 0.38
            else:
                best_level = "SAFE"
                best_score = 0.0

    # ── 3단계: detected_entity 추출 (작품명/아티스트명, 저작권자명 제외) ──
    detected_entity: Optional[str] = None
    entity_conf = 0.0

    for ent_name, ent_score in entity_list:
        if ent_score < 0.78:
            continue
        ent_lower = ent_name.lower()
        # 저작권자명 자체는 제외 (ex. "Netflix" → 저작권자, "Squid Game" → 콘텐츠명)
        is_holder = (
            any(_kw_in_text(kw, ent_lower) for kw in _RIGHTS_MAP)
            or any(rn in ent_lower for rn in _RIGHTS_NAMES_LOWER)
        )
        if not is_holder and ent_score > entity_conf:
            detected_entity = ent_name
            entity_conf = ent_score

    # ── 4단계: 완전 일치 있음 + 출처 미확인 → MEDIUM 상향 ──
    # (무료 이미지 / 이미 높은 점수로 식별된 경우 제외)
    if n_full > 0 and best_score < 0.40 and not best_free:
        best_level = "MEDIUM"
        best_score = 0.45
        logger.debug("vision_medium_uplift", full_matches=n_full)

    # ── 5단계: 본인 콘텐츠 오탐 방지 (채널 + 도메인) ──
    # 사용자가 자신의 콘텐츠를 분석할 때, Vision이 그걸 인터넷(본인 유튜브/
    # 본인 사이트)에서 찾아 "저작권 위반"으로 잘못 판단하는 것을 방지
    _own_chan = _OWN_CHANNELS and _is_own_youtube_content(page_urls, entities)
    _own_dom  = _all_matches_are_own_domains(full_urls, page_urls)
    if _own_chan or _own_dom:
        logger.info("own_content_filtered",
                    reason="channel" if _own_chan else "domain",
                    original_holder=best_holder)
        return {
            "rights_holder":   None,
            "risk_level":      "SAFE",
            "risk_score":      0.0,
            "source_url":      None,
            "web_entities":    entities[:5],
            "full_matches":    n_full,
            "partial_matches": n_partial,
            "pages":           page_urls[:3],
            "is_free":         True,
            "detected_entity": detected_entity,
            "entity_conf":     round(entity_conf, 3),
            "ocr_copyright":   None,
            "own_channel_match": True,   # 본인 콘텐츠 필터 적용됨을 표시
        }

    return {
        "rights_holder":   best_holder,
        "risk_level":      best_level,
        "risk_score":      round(best_score, 3),
        "source_url":      source_url,
        "web_entities":    entities[:5],
        "full_matches":    n_full,
        "partial_matches": n_partial,
        "pages":           page_urls[:3],
        "is_free":         best_free,
        "detected_entity": detected_entity,     # Vision이 인식한 구체적 콘텐츠명
        "entity_conf":     round(entity_conf, 3),
        "ocr_copyright":   ocr_copyright,       # 이미지 내 저작권 텍스트 (있을 시)
    }


# ─────────────────────────────────────────────
# 모듈-레벨 싱글톤
# ─────────────────────────────────────────────
_searcher: Optional[GoogleVisionSearcher] = None


def _build_searcher() -> GoogleVisionSearcher:
    try:
        from config import config
        key = config.api.google_api_key
    except Exception:
        key = ""
    return GoogleVisionSearcher(api_key=key)


def get_searcher() -> GoogleVisionSearcher:
    global _searcher
    if _searcher is None:
        _searcher = _build_searcher()
    return _searcher


def search_image_for_copyright(frame: np.ndarray) -> Optional[Dict]:
    """
    공개 API.
    Google Vision Web Detection + OCR로 이미지 역검색 → 저작권 출처 반환.
    GOOGLE_API_KEY 없으면 None 반환 (예외 없음).
    """
    return get_searcher().search(frame)


def clear_vision_cache() -> None:
    """새 분석 작업 시작 시 호출: Vision API 결과 캐시 초기화."""
    get_searcher().clear_cache()
