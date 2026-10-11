"""
온비드 공매 상가·주택 수집기 (차세대 API)
=========================================

서울 + 고양시의 압류재산 공매 물건 중 상가(상가용및업무용)와 주택(아파트·연립·다세대·단독·다가구)을
수집하고, 목표수익률을 맞추려면 월세가 얼마여야 하는지를 역산해서 구분별 CSV로 저장한다.
API는 용도를 요청 조건으로 받지 못하므로 한 번에 받아서 CATEGORIES 기준으로 나눈다.

준비
----
    pip install -r requirements.txt

    인증키는 코드에 넣지 않고 환경변수 ONBID_KEY 로 읽는다.
        - 로컬: 프로젝트 루트에 .env 파일을 만들고 ONBID_KEY=발급받은Decoding키 한 줄을 적는다.
                (.env.example 참고. .env 는 .gitignore 로 커밋되지 않는다)
        - GitHub Actions: 저장소 Settings → Secrets and variables → Actions 에
                ONBID_KEY 라는 이름으로 Decoding 키를 등록한다.

실행
----
    python onbid_collect2.py

산출물
------
    data/onbid_commercial_YYYY-MM-DD.csv    상가 (onbid_detail.py 이후 단계의 입력)
    data/onbid_residential_YYYY-MM-DD.csv   주택
    data/latest.json                        상가 신규 판별용
    data/onbid_residential_latest.json      주택 신규 판별용
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# 로컬 실행 시 .env 파일에서 환경변수를 읽어온다.
# GitHub Actions에서는 .env 파일이 없고 Secrets가 이미 환경변수로 주입되므로
# python-dotenv가 없거나 .env가 없어도 조용히 넘어간다.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

ENDPOINT = "https://apis.data.go.kr/B010003/OnbidRlstListSrvc2/getRlstCltrList2"

# 공공데이터포털 Decoding 키. 환경변수 ONBID_KEY 로 주입한다 (로컬: .env, Actions: Secrets).
SERVICE_KEY = (os.environ.get("ONBID_KEY") or "").strip()

DEBUG = True     # 첫 요청 URL을 화면에 보여준다 (키는 가림)
PAGE_ROWS = 100  # 한 번에 가져올 건수

# 이 API는 기본 python-requests User-Agent를 거부한다. 브라우저처럼 보내야 한다.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)

# 조회 대상 지역. 시도는 한 번에 하나만 받으므로 나눠서 호출한다.
TARGETS = [
    {"lctnSdnm": "서울특별시"},
    {"lctnSdnm": "경기도", "lctnSggnm": "고양시 덕양구"},
    {"lctnSdnm": "경기도", "lctnSggnm": "고양시 일산동구"},
    {"lctnSdnm": "경기도", "lctnSggnm": "고양시 일산서구"},
]

PRPT_DIV_CD = "0007"        # 압류재산
PVCT_TRGT_YN = "N"          # 수의계약가능여부. 필수 파라미터다
DISPOSAL_SALE = "0001"      # 매각 (임대 제외)

# 구분별 조건. 용도코드는 요청에는 못 쓰고 수신 후 필터로 사용한다.
# 공매는 최저가 필드를 쓸 수 없어 가격 조건은 감정가 기준이다.
CATEGORIES = [
    {
        "구분": "상가",
        "slug": "commercial",
        "mcls": "10300",            # 상가용및업무용건물
        "scls": None,               # 소분류 전체
        "min_appraisal": 200_000_000,
        "max_appraisal": 500_000_000,
        "acq_tax_rate": 0.046,      # 상가 취득세 4.6%
        "floor_coef": True,
        "latest": "latest.json",    # 기존 파일명 유지
    },
    {
        "구분": "주택",
        "slug": "residential",
        "mcls": "10200",            # 주거용건물
        # 아파트, 단독주택, 다가구주택, 다세대주택, 연립주택
        # (도시형생활주택 10208, 기타주거용건물 10219, 오피스텔 10503 은 제외)
        "scls": {"10201", "10204", "10205", "10206", "10209"},
        "min_appraisal": 0,
        "max_appraisal": 100_000_000,
        "acq_tax_rate": 0.011,      # 주택 취득세 1.1%
        "floor_coef": False,        # 주택은 층별 임대료 차이가 작아 층별계수를 쓰지 않는다
        "latest": "onbid_residential_latest.json",
    },
]

MIN_PY = 10.0                  # 평수 하한 10평. 쇼핑몰 초소형 점포 제외
EXCLUDE_SHARE = True            # 지분물건 제외
FAILED_BID_WARN = 5             # 유찰횟수 경고 기준 (이상이면 경고 표시)
FAILED_BID_STRONG_WARN = 10     # 유찰횟수 강한 경고 기준

# 층별 임대료 계수. 1층 대비 저층/고층의 실질 임대 시세 비율을 가정한다.
FLOOR_COEF = {1: 1.0, 2: 0.55, 3: 0.45}
FLOOR_COEF_HIGH = 0.4   # 4층 이상
FLOOR_COEF_BASEMENT = 0.45

# --- 수익률 역산 가정 ---------------------------------------------------------
TARGET_YIELD = 0.06          # 목표 수익률
ASSUMED_WIN_RATE = 0.70      # 낙찰가 / 감정가 가정치
MIN_PRICE_MARKUP = 1.05      # 경매: 최저매각가 대비 낙찰가 가정치 (최저가보다 조금 높게 낙찰)
ACQ_TAX_RATE = 0.046         # 기본 취득세 (상가 4.6%). 주택은 CATEGORIES 에서 1.1%
LEGAL_FEE = 1_500_000        # 법무비·등기비
EVICTION_COST = 10_000_000   # 명도비. 공매는 인도명령이 없어 넉넉히 잡는다
VACANCY_RATE = 0.10          # 공실률
ANNUAL_OPEX = 1_000_000      # 연간 경비 (재산세·수선충당 등)


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------

def fetch_page(params, page, rows=None):
    rows = rows or PAGE_ROWS
    query = {
        "serviceKey": SERVICE_KEY,
        "pageNo": page,
        "numOfRows": rows,
        "resultType": "json",
        "prptDivCd": PRPT_DIV_CD,
        "pvctTrgtYn": PVCT_TRGT_YN,
        **params,
    }
    global DEBUG
    response = requests.get(ENDPOINT, params=query, headers=HEADERS, timeout=30)

    if DEBUG:
        shown = response.url
        if SERVICE_KEY:
            shown = shown.replace(requests.utils.quote(SERVICE_KEY, safe=""), "<KEY>")
        print(f"    요청 URL: {shown}")
        DEBUG = False

    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError:
        print("    ! JSON이 아닌 응답을 받았습니다. 앞부분을 표시합니다:")
        print("      " + response.text[:400].replace("\n", " "))
        return [], 0

    header = payload.get("header") or {}
    code = header.get("resultCode")

    if code != "00":
        msg = header.get("resultMsg")
        if msg == "NODATA_ERROR":
            return [], 0
        if msg:
            print(f"    ! {msg}")
        else:
            print("    ! 예상과 다른 응답입니다. 앞부분을 표시합니다:")
            print("      " + json.dumps(payload, ensure_ascii=False)[:400])
        return [], 0

    body = payload.get("body", {})
    items = (body.get("items") or {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    return items, int(body.get("totalCount", 0))


def collect_region(params):
    label = " ".join(params.values())
    print(f"\n[{label}]")

    rows, page = [], 1
    while True:
        items, total = fetch_page(params, page)
        if not items:
            break
        rows.extend(items)
        print(f"  {len(rows)} / {total}")
        if len(rows) >= total:
            break
        page += 1
        time.sleep(0.3)      # 서버 배려

    return rows


# ---------------------------------------------------------------------------
# 필터 및 계산
# ---------------------------------------------------------------------------

# "제3층", "제지하2층", "제지1층" 처럼 물건명 안에 붙어 나오는 층수 표기.
# "지" 또는 "지하" 접두사가 붙으면 지하층이다.
FLOOR_RE = re.compile(r"제(지하?)?(\d+)층")


def extract_floor(name):
    """물건명에서 층수를 뽑는다. 지하층은 지하여부를 'Y'로 별도 표시한다."""
    if not isinstance(name, str):
        return None, ""
    m = FLOOR_RE.search(name)
    if not m:
        return None, ""
    floor = int(m.group(2))
    is_basement = "Y" if m.group(1) else ""
    return floor, is_basement


def floor_coefficient(floor, is_basement):
    """층수를 1층 임대료 대비 비율로 환산하는 계수를 돌려준다."""
    if is_basement == "Y":
        return FLOOR_COEF_BASEMENT
    if pd.isna(floor):
        return None
    floor = int(floor)
    return FLOOR_COEF.get(floor, FLOOR_COEF_HIGH)


def assumed_bid_price(appraisal, min_price=None):
    """가정낙찰가. 기본은 감정가 × 가정낙찰률.

    최저매각가를 알 수 있는 경매는 유찰이 거듭되면 감정가 × 70% 보다 훨씬 낮게 낙찰되므로
    max(최저매각가 × 1.05, 감정가 × 70%) 로 잡는다. 실제 낙찰가는 최저가보다 조금 높게 형성된다.
    """
    bid = appraisal * ASSUMED_WIN_RATE
    if min_price and not pd.isna(min_price):
        bid = max(min_price * MIN_PRICE_MARKUP, bid)
    return bid


def required_monthly_rent(bid_price, acq_tax_rate=ACQ_TAX_RATE):
    """가정낙찰가에서 목표수익률을 맞추기 위해 필요한 월세를 역산한다."""
    if not bid_price:
        return None

    total_cost = bid_price * (1 + acq_tax_rate) + LEGAL_FEE + EVICTION_COST

    # 보증금은 물건마다 달라 알 수 없으므로 0으로 둔다. 보수적인 방향이다.
    required_net = total_cost * TARGET_YIELD
    required_gross = (required_net + ANNUAL_OPEX) / (1 - VACANCY_RATE)

    return round(required_gross / 12)


def add_rent_columns(df, appraisal_col, category, min_price_col=None):
    """가정낙찰가·필요월세·평당필요월세와 (상가만) 층별 환산 컬럼을 채운다. 법원경매 수집기도 같이 쓴다.

    min_price_col 을 주면 (경매) 가정낙찰가에 최저매각가를 반영한다.
    """
    min_prices = df[min_price_col] if min_price_col else [None] * len(df)
    bids = pd.Series(
        [assumed_bid_price(a, m) for a, m in zip(df[appraisal_col], min_prices)],
        index=df.index,
    )
    df["가정낙찰가"] = bids.round(-4)
    df["필요월세"] = bids.map(lambda b: required_monthly_rent(b, category["acq_tax_rate"]))
    df["평당필요월세"] = (df["필요월세"] / df["평수"]).round(-2)

    if category["floor_coef"]:
        df["층별계수"] = [
            floor_coefficient(f, b) for f, b in zip(df["층"], df["지하여부"])
        ]
        df["1층환산 평당필요월세"] = (df["평당필요월세"] / df["층별계수"]).round(-2)
    else:
        # 컬럼은 남겨 두어 상가 CSV와 구조를 맞춘다.
        df["층별계수"] = None
        df["1층환산 평당필요월세"] = None


def sort_column(category):
    return "1층환산 평당필요월세" if category["floor_coef"] else "평당필요월세"


def prepare_dataframe(raw_rows):
    """구분과 상관없는 공통 정리: 숫자 변환, 회차 중복 제거, 매각만, 지분 제외."""
    df = pd.DataFrame(raw_rows)
    if df.empty:
        return df

    numeric = ["apslEvlAmt", "landSqms", "bldSqms", "usbdNft"]
    for col in numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 같은 물건이 주 단위 재입찰 회차별로 여러 행 나온다.
    # 물건관리번호로 묶어서 입찰시작일이 가장 빠른 회차 한 건만 남긴다.
    before = len(df)
    if "cltrMngNo" in df.columns and "cltrBidBgngDt" in df.columns:
        df["_bidBgngDt"] = pd.to_datetime(df["cltrBidBgngDt"], errors="coerce")
        df = (
            df.sort_values("_bidBgngDt")
            .drop_duplicates(subset="cltrMngNo", keep="first")
            .drop(columns="_bidBgngDt")
        )
        print(f"  회차 중복 제거 (물건관리번호별 최빠른 입찰시작일만): {before} → {len(df)}")

    before = len(df)
    if "dspsMthodNm" in df.columns:
        df = df[df["dspsMthodNm"] == "매각"]
        print(f"  매각 물건만 (임대 제외): {before} → {len(df)}")

    before = len(df)
    if EXCLUDE_SHARE and "alcYn" in df.columns:
        df = df[df["alcYn"] != "Y"]
        print(f"  지분물건 제외: {before} → {len(df)}")

    return df


def build_dataframe(df, category):
    """공통 정리를 마친 df 에서 한 구분(상가/주택)을 골라 필요월세 등을 계산한다."""
    label = category["구분"]
    print(f"\n[{label}]")

    before = len(df)
    df = df[df["cltrUsgMclsCtgrId"].astype(str) == category["mcls"]]
    if category["scls"]:
        df = df[df["cltrUsgSclsCtgrId"].astype(str).isin(category["scls"])]
    print(f"  {label} 용도만: {before} → {len(df)}")

    lo, hi = category["min_appraisal"], category["max_appraisal"]
    before = len(df)
    df = df[df["apslEvlAmt"].fillna(0).between(lo, hi)]
    print(f"  감정가 {lo:,}~{hi:,}원: {before} → {len(df)}")

    if df.empty:
        return df

    df = df.assign(구분=label, 평수=(df["bldSqms"] / 3.3058).round(1))

    before = len(df)
    df = df[df["평수"].fillna(0) >= MIN_PY].copy()
    print(f"  평수 {MIN_PY}평 이상: {before} → {len(df)}")

    if df.empty:
        return df

    if "onbidCltrNm" in df.columns:
        floor_info = df["onbidCltrNm"].map(extract_floor)
        df["층"] = floor_info.map(lambda t: t[0])
        df["지하여부"] = floor_info.map(lambda t: t[1])

    add_rent_columns(df, "apslEvlAmt", category)

    def warn_label(n):
        if n >= FAILED_BID_STRONG_WARN:
            return f"🔴 유찰{int(n)}회"
        if n >= FAILED_BID_WARN:
            return f"⚠ 유찰{int(n)}회"
        return ""

    df["경고"] = df["usbdNft"].fillna(0).map(warn_label)
    df["온비드링크"] = (
        "https://www.onbid.co.kr/op/cta/cltrdtl/collateralRealEstateDetail.do"
        "?cltrHstrNo=" + df["onbidCltrno"].astype(str)
    )

    return df


KEEP_COLUMNS = [
    "구분",
    "cltrMngNo", "onbidCltrNm", "층", "지하여부", "cltrUsgSclsCtgrNm",
    "lctnSdnm", "lctnSggnm", "lctnEmdNm",
    "apslEvlAmt", "가정낙찰가", "필요월세", "평당필요월세",
    "층별계수", "1층환산 평당필요월세",
    "bldSqms", "평수", "landSqms",
    "usbdNft", "경고", "alcYn", "evcRsbyTrgtCont",
    "cltrBidBgngDt", "cltrBidEndDt", "dtbtRqrEdtmCont",
    "pbctStatNm", "ltnoPnu", "mdfcnDt", "온비드링크",
]

RENAMES = {
    "cltrMngNo": "물건관리번호",
    "onbidCltrNm": "물건명",
    "cltrUsgSclsCtgrNm": "용도",
    "lctnSdnm": "시도", "lctnSggnm": "시군구", "lctnEmdNm": "읍면동",
    "apslEvlAmt": "감정가",
    "bldSqms": "건물면적", "landSqms": "토지면적",
    "usbdNft": "유찰횟수", "alcYn": "지분여부",
    "evcRsbyTrgtCont": "명도책임",
    "cltrBidBgngDt": "입찰시작", "cltrBidEndDt": "입찰마감",
    "dtbtRqrEdtmCont": "배분요구종기",
    "pbctStatNm": "상태", "ltnoPnu": "PNU", "mdfcnDt": "최종수정",
}

# 저장되는 CSV의 공통 컬럼 (한글 이름). 법원경매 수집기도 이 순서를 따른다.
OUTPUT_COLUMNS = [RENAMES.get(c, c) for c in KEEP_COLUMNS] + ["신규"]


def _fmt(value):
    return "" if pd.isna(value) else f"{value:,.0f}"


def output_path(category, prefix):
    stamp = datetime.now().strftime("%Y-%m-%d")
    return OUT_DIR / f"{prefix}_{category['slug']}_{stamp}.csv"


def save_empty(category, prefix):
    """조건을 통과한 물건이 없어도 헤더만 있는 CSV를 남긴다.

    파일이 없으면 다음 단계(상세조회·텔레그램)가 며칠 전 파일을 오늘 것으로 오인할 수 있다.
    신규 판별용 latest 파일은 건드리지 않는다.
    """
    path = output_path(category, prefix)
    pd.DataFrame(columns=OUTPUT_COLUMNS).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[{category['구분']}] 조건을 통과한 물건이 없습니다. 빈 CSV를 {path} 에 저장했습니다.")


def save_output(out, category, prefix, latest_name, preview_cols):
    """신규 표시를 붙이고 정렬해서 data/{prefix}_{slug}_YYYY-MM-DD.csv 로 저장한다.

    out 은 OUTPUT_COLUMNS 중 '신규'를 뺀 컬럼(+출처별 추가 컬럼)을 가진 DataFrame 이다.
    """
    prev_path = OUT_DIR / latest_name
    previous = set()
    if prev_path.exists():
        previous = set(json.loads(prev_path.read_text(encoding="utf-8")))

    ids = out["물건관리번호"].astype(str)
    out = out.copy()
    out["신규"] = ids.map(lambda x: "Y" if previous and x not in previous else "")
    prev_path.write_text(json.dumps(sorted(set(ids)), ensure_ascii=False), encoding="utf-8")

    extras = [c for c in out.columns if c not in OUTPUT_COLUMNS]
    out = out[OUTPUT_COLUMNS + extras].sort_values(sort_column(category), ascending=True)

    path = output_path(category, prefix)
    out.to_csv(path, index=False, encoding="utf-8-sig")

    print(f"\n[{category['구분']}] {len(out)}건을 {path} 에 저장했습니다.")
    if previous:
        print(f"이 중 신규 {(out['신규'] == 'Y').sum()}건")

    cols = [c for c in preview_cols if category["floor_coef"] or c not in ("층별계수", "1층환산 평당필요월세")]
    preview = out[cols].head(15).copy()
    for col in ("감정가", "최저매각가", "필요월세", "평당필요월세", "1층환산 평당필요월세"):
        if col in preview.columns:
            preview[col] = preview[col].map(_fmt)
    print("--- 미리보기 ---")
    print(preview.to_string(index=False))


# ---------------------------------------------------------------------------

def main():
    if not SERVICE_KEY:
        sys.exit(
            "인증키가 없습니다. 환경변수 ONBID_KEY 를 설정하세요.\n"
            "  로컬: 프로젝트 루트에 .env 파일을 만들고 ONBID_KEY=발급받은Decoding키 를 적으세요 (.env.example 참고).\n"
            "  GitHub Actions: 저장소 Secrets에 ONBID_KEY 를 등록하세요."
        )
    print(f"인증키 길이: {len(SERVICE_KEY)}자 (끝 4자리: ...{SERVICE_KEY[-4:]})")


    raw = []
    for target in TARGETS:
        try:
            raw.extend(collect_region(target))
        except requests.RequestException as exc:
            print(f"  ! 요청 실패: {exc}")

    print(f"\n총 {len(raw)}건 수집. 필터를 적용합니다.")
    if not raw:
        return

    base = prepare_dataframe(raw)

    for category in CATEGORIES:
        df = build_dataframe(base, category)
        if df.empty:
            save_empty(category, "onbid")
            continue

        cols = [c for c in KEEP_COLUMNS if c in df.columns]
        out = df[cols].rename(columns=RENAMES)
        save_output(
            out, category, "onbid", category["latest"],
            ["물건명", "층", "지하여부", "용도", "감정가", "필요월세", "평수",
             "평당필요월세", "층별계수", "1층환산 평당필요월세", "경고"],
        )


if __name__ == "__main__":
    main()
