#!/usr/bin/env python3
"""
Gugus competitive monitor - feed builder.

Queries gugus.co.kr's public search API for the watchlist models (Chanel + Hermes),
parses each product's embedded `wishProduct({...})` JSON, maps it to the schema that
index.html's loadFeed() expects, and writes feed.json.

No browser needed: gugus serves product data through a JSON-in / HTML-fragment-out
endpoint, and every field we need is in the wishProduct payload on each card.

Runs in GitHub Actions on a schedule. See .github/workflows/update-feed.yml.
"""

import json
import re
import sys
import time
import datetime as dt
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.stderr.write("Missing dependency: pip install beautifulsoup4\n")
    raise

BASE = "https://www.gugus.co.kr"
SEARCH_URL = BASE + "/search/selectOpenSearchGoods"
IMG_BASE = "https://image.gugus.co.kr"

# Each search term is fetched once; results are then classified into the
# specific watchlist model keys below. Fewer searches, full coverage.
SEARCH_TERMS = [
    "에르메스 벌킨",        # Hermes Birkin     -> birkin25, birkin30
    "에르메스 켈리 25",     # Hermes Kelly 25   -> kelly25
    "에르메스 미니켈리",    # Hermes Mini Kelly -> mini_kelly
    "샤넬 클래식",          # Chanel Classic    -> cdf_small/medium/jumbo
    "샤넬 보이",            # Chanel Boy        -> boy_med
    "샤넬 WOC",             # Chanel WOC        -> woc
    "샤넬 지갑 온 체인",    # wallet on chain   -> woc
]

# Human-readable label for each watchlist key (must match index.html's defs).
MODEL_LABEL = {
    "cdf_small": "Classic Flap · Small",
    "cdf_medium": "Classic Flap · Medium",
    "cdf_jumbo": "Classic Flap · Jumbo",
    "woc": "Wallet on Chain",
    "boy_med": "Boy · Medium",
    "birkin25": "Birkin 25",
    "birkin30": "Birkin 30",
    "kelly25": "Kelly 25",
    "mini_kelly": "Mini Kelly",
}

# Gugus condition grade -> dashboard condition wording.
GRADE_MAP = {"N": "New", "S": "Like New", "A": "Excellent", "B": "Very Good", "C": "Good"}

# Common color romanisation (cosmetic; falls back to the Korean term).
COLOR_MAP = {
    "블랙": "Black", "화이트": "White", "베이지": "Beige",
    "브라운": "Brown", "네이비": "Navy", "그레이": "Grey",
    "버건디": "Burgundy", "레드": "Red", "핑크": "Pink",
    "블루": "Blue", "그린": "Green", "골드": "Gold",
    "실버": "Silver", "옐로우": "Yellow", "오렌지": "Orange",
    "퍼플": "Purple", "카멜": "Camel", "크림": "Cream", "카키": "Khaki",
}

# Common store romanisation (cosmetic; falls back to the Korean name).
STORE_MAP = {
    "청담블랙점": "Cheongdam Black", "압구정점": "Apgujeong",
    "한남점": "Hannam", "반포신세계점": "Banpo Shinsegae",
    "대치점": "Daechi", "선릉점": "Seolleung", "명동점": "Myeongdong",
    "잠실석촌호수점": "Jamsil", "일산점": "Ilsan",
    "분당정자점": "Bundang Jeongja", "판교역점": "Pangyo",
    "인천송도점": "Songdo", "부산센텀점": "Busan Centum",
    "해운대마린점": "Haeundae Marine", "부산서면점": "Busan Seomyeon",
    "동래점": "Dongnae", "대구점": "Daegu", "대구수성점": "Daegu Suseong",
    "대전타임월드점": "Daejeon Time World", "울산점": "Ulsan",
    "광주상무점": "Gwangju Sangmu",
}


# Leather/material keywords -> display name. Hermès leather appears in the
# product description (often English: TOGO, EPSOM...); Chanel leather is usually
# in the title (캐비어 / 램스킨).
LEATHER_MAP = {
    "TOGO": "Togo", "토고": "Togo",
    "EPSOM": "Epsom", "엡송": "Epsom",
    "SWIFT": "Swift", "스위프트": "Swift",
    "CLEMENCE": "Clémence", "클레망스": "Clémence", "클레멘스": "Clémence",
    "CHEVRE": "Chèvre", "샤브르": "Chèvre", "셰브르": "Chèvre",
    "BARENIA": "Barénia", "바레니아": "Barénia",
    "EVERCOLOR": "Evercolor", "에버컬러": "Evercolor",
    "NOVILLO": "Novillo", "노빌로": "Novillo",
    "FJORD": "Fjord", "피요르드": "Fjord",
    "TADELAKT": "Tadelakt", "타델락": "Tadelakt",
    "MYSORE": "Mysore", "마이소르": "Mysore",
    "OSTRICH": "Ostrich", "오스트리치": "Ostrich",
    "ALLIGATOR": "Alligator", "알리게이터": "Alligator",
    "CAVIAR": "Caviar", "캐비어": "Caviar", "캐비아": "Caviar",
    "LAMBSKIN": "Lambskin", "램스킨": "Lambskin",
    "CALFSKIN": "Calfskin", "카프스킨": "Calfskin", "카프": "Calfskin",
    "TWEED": "Tweed", "트위드": "Tweed",
    "PATENT": "Patent", "파텐트": "Patent", "에나멜": "Patent",
}

DETAIL_URL = BASE + "/goods/viewGoods?goodsNo="
CACHE_FILE = "detail_cache.json"

HEADERS = {
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "text/html, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Origin": BASE,
    "Referer": BASE + "/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}


def fetch_search(term, per_page=40, page=1, retries=3):
    payload = {
        "perPage": per_page, "page": page,
        "uperCategoryList": [], "categoryList": [], "brandList": [],
        "modelList": [], "gradeList": [], "propertyList": [], "shopList": [],
        "excludeTradingYn": "N", "purcvPsbYn": "N", "orginSearchTermYn": "N",
        "sortOrder": "REG_DESC", "searchTerm": term,
    }
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(retries):
        try:
            req = Request(SEARCH_URL, data=body, headers=HEADERS, method="POST")
            with urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except (URLError, HTTPError) as e:
            sys.stderr.write("  search '%s' attempt %d failed: %s\n" % (term, attempt + 1, e))
            time.sleep(2 * (attempt + 1))
    return ""


def extract_wish_objects(html):
    objs = []
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select('[onclick*="wishProduct"]'):
        oc = el.get("onclick", "")
        s, e = oc.find("{"), oc.rfind("}")
        if s < 0 or e <= s:
            continue
        try:
            objs.append(json.loads(oc[s:e + 1]))
        except json.JSONDecodeError:
            continue
    return objs


def classify(o):
    name = ((o.get("gdsNm") or "") + " " + (o.get("mdlKorNm") or "") + " " +
            (o.get("mdlEngNm") or "")).lower()

    def has(*words):
        return any(w in name for w in words)

    size_m = re.search(r"(?<!\\d)(25|28|30|32|35|40)(?!\\d)", name)
    size = size_m.group(1) if size_m else None

    # Wallet on Chain - check first: "클래식"/"보이" also appear in WOC names.
    # gugus spells it 월렛 (not 월릿).
    if has("woc", "월렛 온 체인", "월릿 온 체인", "지갑 온 체인", "wallet on chain"):
        return "woc"

    # Small leather goods (wallets, coin purses, card holders) often carry the
    # model word but are not bags we track - drop them.
    if has("지갑", "동전", "카드", "wallet", "card holder",
           "뮬", "벨트", "샌들", "슬리퍼", "스카프", "트윌리",
           "참", "스트랩", "키링", "브로치", "팔찌", "귀걸이"):
        return None

    if has("벌킨", "birkin"):
        if size == "25":
            return "birkin25"
        if size == "30":
            return "birkin30"
        return None
    if has("켈리", "kelly"):
        if has("미니", "mini"):
            return "mini_kelly"
        if size == "25":
            return "kelly25"
        return None
    if has("보이", "boy"):
        return "boy_med"
    if has("클래식", "classic"):
        if has("스몰", "small"):
            return "cdf_small"
        if has("점보", "jumbo", "맥시", "maxi", "라지", "large"):
            return "cdf_jumbo"
        if has("미디움", "미듐", "medium", "미디엄"):
            return "cdf_medium"
        return None  # unknown size - do not guess
    return None


def map_color(kor):
    if not kor:
        return "—"
    for k, v in COLOR_MAP.items():
        if k in kor:
            return v
    return kor


def map_store(kor):
    if not kor:
        return "—"
    return STORE_MAP.get(kor, kor)


def to_listing(o, model_key):
    krw = o.get("prstSalePrc") or o.get("dcSalePrc") or o.get("frstSalePrc") or 0
    year_m = re.search(r"\b(19|20)\d{2}\b", o.get("gdsNm") or "")
    img = o.get("gdsImgUrl") or ""
    if img and not img.startswith("http"):
        img = IMG_BASE + img
    return {
        "goodsNo": str(o.get("gdsNo") or ""),
        "brand": "Hermès" if (o.get("brndEngNm") == "Hermes") else (o.get("brndEngNm") or "—"),
        "model": MODEL_LABEL[model_key],
        "modelKey": model_key,
        "krw": int(krw) if isinstance(krw, (int, float)) else 0,
        "condition": GRADE_MAP.get((o.get("gdsGrdNm") or "").strip().upper(), "Used"),
        "year": year_m.group(0) if year_m else "—",
        "color": map_color(o.get("prptValColorComplex") or o.get("prptValColor")),
        "inclusions": [False, False, False, False],
        "postedAt": o.get("regDtm") or o.get("ltlyRegDtm") or None,
        "store": map_store(o.get("invtPssnShpNm") or o.get("shpRgnSprtNm")),
        "resalePHP": None,
        "image": img,
        "stamp": extract_stamp(
            "Hermès" if (o.get("brndEngNm") == "Hermes") else (o.get("brndEngNm") or ""),
            o.get("gdsNm")) or "—",
        "leather": extract_leather(o.get("gdsNm") or ""),  # may be None -> enriched later
    }


def extract_leather(*texts):
    blob = " ".join(t for t in texts if t)
    up = blob.upper()
    for k, v in LEATHER_MAP.items():
        if k.isascii():
            if k in up:
                return v
        elif k in blob:
            return v
    return None


def extract_stamp(brand, name):
    """Hermès engraved stamp letter, or Chanel series number, from the title."""
    if brand == "Hermès":
        m = re.search(r"\(([A-Z])\)", name or "")
        if m:
            return "Stamp " + m.group(1)
    else:
        m = re.search(r"(\d+)\s*번대", name or "")
        if m:
            return "Series " + m.group(1)
    return None


def fetch_detail_desc(goods_no, retries=2):
    """Return the free-text product description (holds Hermès leather)."""
    for attempt in range(retries):
        try:
            req = Request(DETAIL_URL + str(goods_no),
                          headers={"User-Agent": HEADERS["User-Agent"],
                                   "Accept-Language": "ko-KR,ko;q=0.9"})
            with urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", "replace")
            m = re.search(r'gdsDtlDsct\s*=\s*"((?:[^"\\]|\\.)*)"', html)
            if m:
                try:
                    return json.loads('"' + m.group(1) + '"')
                except Exception:
                    return m.group(1)
        except (URLError, HTTPError):
            time.sleep(1.5 * (attempt + 1))
    return ""


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, sort_keys=True, indent=0)


def fetch_fx_rate(default=0.0398):
    """PHP per 1 KRW. index.html multiplies krw * rate to get the PHP price."""
    url = "https://open.er-api.com/v6/latest/KRW"
    try:
        req = Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
        with urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        rate = data.get("rates", {}).get("PHP")
        if isinstance(rate, (int, float)) and 0 < rate < 1:
            return round(rate, 5)
    except Exception as e:
        sys.stderr.write("FX fetch failed, using default %s: %s\n" % (default, e))
    return default


def main():
    seen = {}
    for term in SEARCH_TERMS:
        html = fetch_search(term)
        if not html:
            continue
        for o in extract_wish_objects(html):
            key = classify(o)
            if not key:
                continue
            gid = str(o.get("gdsNo") or "")
            if not gid or gid in seen:
                continue
            seen[gid] = to_listing(o, key)
        time.sleep(1)

    # Enrich leather: Chanel usually has it in the title already; Hermès needs
    # the detail page. Cache by goodsNo so only new items are ever fetched.
    cache = load_cache()
    fetched = 0
    for gid, listing in seen.items():
        if listing.get("leather"):
            continue
        if gid in cache:
            listing["leather"] = cache[gid] or "—"
            continue
        lea = extract_leather(fetch_detail_desc(gid)) or ""
        cache[gid] = lea
        listing["leather"] = lea or "—"
        fetched += 1
        time.sleep(0.4)
    # normalise any remaining None
    for listing in seen.values():
        if not listing.get("leather"):
            listing["leather"] = "—"
    # prune cache to goodsNo we still see (keep it from growing forever)
    cache = {k: v for k, v in cache.items() if k in seen}
    save_cache(cache)
    print("Detail pages fetched this run: %d" % fetched)

    listings = list(seen.values())
    listings.sort(key=lambda x: x.get("postedAt") or "", reverse=True)

    feed = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fxRate": fetch_fx_rate(),
        "listings": listings,
    }
    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    print("Wrote feed.json: %d listings, fxRate=%s" % (len(listings), feed["fxRate"]))
    by_model = {}
    for l in listings:
        by_model[l["modelKey"]] = by_model.get(l["modelKey"], 0) + 1
    print("By model:", json.dumps(by_model, ensure_ascii=False))


if __name__ == "__main__":
    main()
