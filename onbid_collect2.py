"""
온비드 공매 상가 수집기 (차세대 API)
====================================

서울 + 고양시의 상가용/업무용 압류재산 공매 물건을 수집하고,
목표수익률을 맞추려면 월세가 얼마여야 하는지를 역산해서 CSV로 저장한다.

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
    data/onbid_commercial_YYYY-MM-DD.csv   전체 수집분
    data/latest.json                       다음 실행 때 신규 판별용
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
USAGE_MCLS = "10300"        # 상가용및업무용건물 (요청에는 못 쓰고 수신 후 필터로 사용)
PVCT_TRGT_YN = "N"          # 수의계약가능여부. 필수 파라미터다
DISPOSAL_SALE = "0001"      # 매각 (임대 제외)

MIN_APPRAISAL = 200_000_000    # 감정가 하한 2억
MAX_APPRAISAL = 500_000_000    # 감정가 상한 5억
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
ACQ_TAX_RATE = 0.046         # 상가 취득세 4.6%
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


def required_monthly_rent(appraisal):
    """목표수익률을 맞추기 위해 필요한 월세를 역산한다."""
    if not appraisal:
        return None

    bid_price = appraisal * ASSUMED_WIN_RATE
    total_cost = bid_price * (1 + ACQ_TAX_RATE) + LEGAL_FEE + EVICTION_COST

    # 보증금은 물건마다 달라 알 수 없으므로 0으로 둔다. 보수적인 방향이다.
    required_net = total_cost * TARGET_YIELD
    required_gross = (required_net + ANNUAL_OPEX) / (1 - VACANCY_RATE)

    return round(required_gross / 12)


def build_dataframe(raw_rows):
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

    if "cltrUsgMclsCtgrId" in df.columns:
        df = df[df["cltrUsgMclsCtgrId"].astype(str) == USAGE_MCLS]
        print(f"  상가용및업무용건물만: {before} → {len(df)}")

    before = len(df)
    if "dspsMthodNm" in df.columns:
        df = df[df["dspsMthodNm"] == "매각"]
        print(f"  매각 물건만 (임대 제외): {before} → {len(df)}")

    before = len(df)
    if EXCLUDE_SHARE and "alcYn" in df.columns:
        df = df[df["alcYn"] != "Y"]
        print(f"  지분물건 제외: {before} → {len(df)}")

    before = len(df)
    df = df[df["apslEvlAmt"].fillna(0).between(MIN_APPRAISAL, MAX_APPRAISAL)]
    print(f"  감정가 {MIN_APPRAISAL:,}~{MAX_APPRAISAL:,}원: {before} → {len(df)}")

    if df.empty:
        return df

    df["평수"] = (df["bldSqms"] / 3.3058).round(1)

    before = len(df)
    df = df[df["평수"].fillna(0) >= MIN_PY]
    print(f"  평수 {MIN_PY}평 이상: {before} → {len(df)}")

    if df.empty:
        return df

    if "onbidCltrNm" in df.columns:
        floor_info = df["onbidCltrNm"].map(extract_floor)
        df["층"] = floor_info.map(lambda t: t[0])
        df["지하여부"] = floor_info.map(lambda t: t[1])

    df["가정낙찰가"] = (df["apslEvlAmt"] * ASSUMED_WIN_RATE).round(-4)
    df["필요월세"] = df["apslEvlAmt"].map(required_monthly_rent)
    df["평당필요월세"] = (df["필요월세"] / df["평수"]).round(-2)

    df["층별계수"] = [
        floor_coefficient(f, b) for f, b in zip(df["층"], df["지하여부"])
    ]
    df["1층환산 평당필요월세"] = (df["평당필요월세"] / df["층별계수"]).round(-2)

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

    df = build_dataframe(raw)
    if df.empty:
        print("조건을 통과한 물건이 없습니다.")
        return

    # 신규 물건 판별
    prev_path = OUT_DIR / "latest.json"
    previous = set()
    if prev_path.exists():
        previous = set(json.loads(prev_path.read_text(encoding="utf-8")))

    current = set(df["cltrMngNo"].astype(str))
    df["신규"] = df["cltrMngNo"].astype(str).map(lambda x: "Y" if x not in previous and previous else "")
    prev_path.write_text(json.dumps(sorted(current), ensure_ascii=False), encoding="utf-8")

    cols = [c for c in KEEP_COLUMNS if c in df.columns] + ["신규"]
    out = df[cols].rename(columns=RENAMES).sort_values("1층환산 평당필요월세", ascending=True)

    stamp = datetime.now().strftime("%Y-%m-%d")
    path = OUT_DIR / f"onbid_commercial_{stamp}.csv"
    out.to_csv(path, index=False, encoding="utf-8-sig")

    print(f"\n{len(out)}건을 {path} 에 저장했습니다.")
    if previous:
        print(f"이 중 신규 {(out['신규'] == 'Y').sum()}건")

    preview = out[
        ["물건명", "층", "지하여부", "용도", "감정가", "필요월세", "평수",
         "평당필요월세", "층별계수", "1층환산 평당필요월세", "경고"]
    ].head(15).copy()
    preview["감정가"] = preview["감정가"].map("{:,.0f}".format)
    preview["필요월세"] = preview["필요월세"].map("{:,.0f}".format)
    preview["평당필요월세"] = preview["평당필요월세"].map("{:,.0f}".format)
    preview["1층환산 평당필요월세"] = preview["1층환산 평당필요월세"].map("{:,.0f}".format)
    print("\n--- 미리보기 ---")
    print(preview.to_string(index=False))


if __name__ == "__main__":
    main()
