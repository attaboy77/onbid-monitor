"""
법원경매 상가 수집기 (courtauction.go.kr)
=========================================

서울 + 고양시(덕양·일산동·일산서)의 상가(상업용및업무용)·주택(아파트·연립·다세대·빌라·단독·다가구)
경매 물건을 수집하고, onbid_collect2.py 와 같은 가정으로 필요월세를 역산해
온비드와 같은 컬럼 구성의 CSV로 구분별로 저장한다. 가격 조건은 최저매각가 기준이다.

법원경매정보에는 공식 API가 없다. 2025년 개편 이후 화면은 WebSquare가 내부 JSON 호출로 채우는데,
tools/court_capture.py 로 캡처해 보니 물건상세검색은 아래 엔드포인트 하나로 동작했다.
쿠키나 로그인 없이 requests 만으로 호출된다.

    POST /pgj/pgjsearch/searchControllerMain.on
    body: {"dma_pageInfo": {...}, "dma_srchGdsDtlSrchInfo": {...}}

알아낸 규칙
-----------
- 시도는 rprsAdongSdCd (서울 11, 경기 41). 시군구는 rprsAdongSggCd 에 화면 드롭다운의 3자리 코드를 넣는다
  (고양 덕양 471 / 일산동 285 / 일산서 287). 5자리(41471)를 넣으면 0건이 된다.
- 용도: 대분류 20000(건물) → 중분류 21100(상업용및업무용) / 20100(주거용건물).
  소분류는 코드 하나만 제대로 걸린다 (콤마로 여러 개를 넣으면 엉뚱한 결과). 그래서 중분류로 받고 소분류는 받은 뒤 거른다.
- 감정가 범위는 aeeEvlAmtMin / aeeEvlAmtMax, 최저매각가 범위는 lwsDspslPrcMin / lwsDspslPrcMax (원 단위 문자열).
  최저매각가 상한을 걸어도 조금 넘는 행이 섞여 오므로 받은 뒤 한 번 더 거른다.
- 매각기일 범위 bidBgngYmd~bidEndYmd. 시작일이 과거면 0건이 나오므로 오늘부터 조회한다.
  공고된 기일은 대략 2~3개월 앞까지만 있어서 끝을 넉넉히 잡아도 된다.
- 결과는 '목록' 단위 행이다 (일괄매각이면 토지·건물이 따로 나온다). 페이징도 이 행 기준이고
  pageSize 는 50까지 된다 (100은 400 오류). 물건 단위로 쓰려면 법원+사건번호+물건번호로 묶어야 한다.

실행
----
    python court_collect.py

산출물
------
    data/court_commercial_YYYY-MM-DD.csv    상가 (온비드 CSV와 같은 컬럼 + 경매 전용 컬럼)
    data/court_residential_YYYY-MM-DD.csv   주택
    data/court_commercial_latest.json       상가 신규 판별용
    data/court_residential_latest.json      주택 신규 판별용
"""

import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

# 수익률 역산 가정과 공통 필터는 온비드 수집기와 같은 값을 쓴다. 한쪽만 바꾸면 비교가 어긋난다.
from onbid_collect2 import (
    CATEGORIES as ONBID_CATEGORIES,
    EXCLUDE_SHARE,
    MIN_PY,
    add_rent_columns,
    save_empty,
    save_output,
)

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

BASE_URL = "https://www.courtauction.go.kr"
SEARCH_URL = BASE_URL + "/pgj/pgjsearch/searchControllerMain.on"

# 브라우저가 보내는 헤더를 그대로 흉내낸다. submissionid / sc-userid 는 WebSquare 가 붙이는 값이다.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": BASE_URL + "/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml",
    "submissionid": "mf_wfm_mainFrame_sbm_selectGdsDtlSrch",
    "sc-userid": "SYSTEM",
}

KST = timezone(timedelta(hours=9))
PAGE_SIZE = 50      # 서버 상한. 100은 400 오류
BID_DAYS = 180      # 오늘부터 며칠 뒤 매각기일까지 볼지

# 조회 대상 지역. 시도 하나 + (선택) 시군구 하나씩 호출한다.
TARGETS = [
    {"label": "서울특별시", "sd": "11", "sgg": ""},
    {"label": "고양시 덕양구", "sd": "41", "sgg": "471"},
    {"label": "고양시 일산동구", "sd": "41", "sgg": "285"},
    {"label": "고양시 일산서구", "sd": "41", "sgg": "287"},
]

USAGE_LCL = "20000"   # 건물


def _onbid(label):
    return next(c for c in ONBID_CATEGORIES if c["구분"] == label)


# 구분별 조건. 구분 이름·slug·취득세율·층별계수 사용 여부는 온비드 설정을 그대로 따르고,
# 용도코드와 가격 조건(최저매각가 상한)만 경매용으로 덮어쓴다.
CATEGORIES = [
    {
        **_onbid("상가"),
        "mcl": "21100",             # 상업용및업무용
        "scl": None,                # 소분류 전체 (근린생활시설·판매·업무 등)
        "max_min_price": 500_000_000,
        "latest": "court_commercial_latest.json",
    },
    {
        **_onbid("주택"),
        "mcl": "20100",             # 주거용건물
        # 단독, 다가구, 아파트, 연립, 다세대, 빌라
        # (다중주택·기숙사·상가주택·오피스텔·주상복합 제외)
        "scl": {"20101", "20102", "20104", "20105", "20106", "20108"},
        "max_min_price": 100_000_000,
        "latest": "court_residential_latest.json",
    },
]

# 경매는 유찰 1회마다 최저가가 20~30%씩 내려가므로 공매(주 단위 재입찰)보다 기준을 낮게 둔다.
FAILED_BID_WARN = 3
FAILED_BID_STRONG_WARN = 5

# 물건비고에 이 단어가 있으면 경고 컬럼에 붙인다.
RISK_KEYWORDS = [
    "유치권", "법정지상권", "분묘", "대지권미등기", "대지권 미등기", "대지권없음",
    "건물만", "토지만", "토지별도등기", "선순위", "대항력", "위반건축물", "지분",
]

# 소분류 코드 → 이름 (selectSclLst.on 응답)
SCL_NAMES = {
    # 주거용건물 20100
    "20101": "단독주택", "20102": "다가구주택", "20103": "다중주택",
    "20104": "아파트", "20105": "연립주택", "20106": "다세대주택",
    "20107": "기숙사", "20108": "빌라", "20109": "상가주택",
    "20110": "오피스텔", "20111": "주상복합",
    # 상업용및업무용 21100
    "21101": "근린생활시설", "21102": "문화및집회시설", "21103": "종교시설",
    "21104": "판매시설", "21105": "운수시설", "21106": "의료시설",
    "21107": "교육연구시설", "21108": "노유자시설", "21109": "수련시설",
    "21110": "운동시설", "21111": "업무시설", "21112": "숙박시설",
    "21113": "위락시설", "21114": "교정및군사시설", "21115": "방송통신시설",
    "21116": "발전시설", "21117": "묘지관련시설", "21118": "관광휴게시설",
}

# 목록구분(mokGbncd): 01 토지, 02 건물, 03 집합건물
MOK_LAND = "01"


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------

def search_body(target, category, page, start, end):
    """물건상세검색 요청 본문. 화면이 보내는 필드를 모두 채워야 서버가 받아준다."""
    return {
        "dma_pageInfo": {
            "pageNo": page, "pageSize": PAGE_SIZE, "bfPageNo": "", "startRowNo": "",
            "totalCnt": "", "totalYn": "Y" if page == 1 else "N", "groupTotalCount": "",
        },
        "dma_srchGdsDtlSrchInfo": {
            "rletDspslSpcCondCd": "", "bidDvsCd": "000331", "mvprpRletDvsCd": "00031R",
            "cortAuctnSrchCondCd": "0004601",
            "rprsAdongSdCd": target["sd"], "rprsAdongSggCd": target["sgg"], "rprsAdongEmdCd": "",
            "rdnmSdCd": "", "rdnmSggCd": "", "rdnmNo": "",
            "mvprpDspslPlcAdongSdCd": "", "mvprpDspslPlcAdongSggCd": "", "mvprpDspslPlcAdongEmdCd": "",
            "rdDspslPlcAdongSdCd": "", "rdDspslPlcAdongSggCd": "", "rdDspslPlcAdongEmdCd": "",
            "cortOfcCd": "", "jdbnCd": "", "execrOfcDvsCd": "",
            "lclDspslGdsLstUsgCd": USAGE_LCL, "mclDspslGdsLstUsgCd": category["mcl"],
            "sclDspslGdsLstUsgCd": "",
            "cortAuctnMbrsId": "",
            "aeeEvlAmtMin": "", "aeeEvlAmtMax": "",
            "lwsDspslPrcRateMin": "", "lwsDspslPrcRateMax": "",
            "flbdNcntMin": "", "flbdNcntMax": "", "objctArDtsMin": "", "objctArDtsMax": "",
            "mvprpArtclKndCd": "", "mvprpArtclNm": "", "mvprpAtchmPlcTypCd": "",
            "notifyLoc": "on", "lafjOrderBy": "", "pgmId": "PGJ151F01", "csNo": "",
            "cortStDvs": "2", "statNum": 1,
            "bidBgngYmd": start, "bidEndYmd": end, "dspslDxdyYmd": "",
            "fstDspslHm": "", "scndDspslHm": "", "thrdDspslHm": "", "fothDspslHm": "",
            "dspslPlcNm": "", "lwsDspslPrcMin": "",
            "lwsDspslPrcMax": str(category["max_min_price"]),
            "grbxTypCd": "", "gdsVendNm": "", "fuelKndCd": "",
            "carMdyrMax": "", "carMdyrMin": "", "carMdlNm": "", "sideDvsCd": "",
        },
    }


def fetch_page(session, target, category, page, start, end):
    body = search_body(target, category, page, start, end)
    response = session.post(SEARCH_URL, json=body, timeout=30)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        print("    ! JSON이 아닌 응답을 받았습니다. 앞부분을 표시합니다:")
        print("      " + response.text[:400].replace("\n", " "))
        return [], 0

    data = payload.get("data") or {}
    if payload.get("status") != 200 or "dlt_srchResult" not in data:
        print(f"    ! 예상과 다른 응답: {payload.get('message')} / {str(payload.get('errors'))[:200]}")
        return [], 0

    total = data.get("dma_pageInfo", {}).get("totalCnt") or 0
    return data["dlt_srchResult"] or [], int(total)


def collect_region(session, target, category, start, end):
    print(f"\n[{category['구분']} / {target['label']}]")
    rows, page, total = [], 1, None
    while True:
        items, count = fetch_page(session, target, category, page, start, end)
        if page == 1:
            total = count
        if not items:
            break
        rows.extend(items)
        print(f"  page {page}: {len(items)}행 (누적 {len(rows)}/{total})")
        if total and len(rows) >= total:
            break
        page += 1
        time.sleep(0.5)      # 서버 배려
    return rows


# ---------------------------------------------------------------------------
# 가공
# ---------------------------------------------------------------------------

AREA_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*㎡")
FLOOR_RE = re.compile(r"(지하)?(\d+)층")
HO_RE = re.compile(r"(비|B)?(\d{3,4})호")


def sum_area(text):
    """'1층 92.34㎡\\n2층 66.09㎡' 같은 면적 목록을 합산한다. '(연면적 제외)' 줄은 뺀다."""
    if not isinstance(text, str):
        return None
    total, found = 0.0, False
    for line in text.splitlines():
        if "제외" in line:
            continue
        for m in AREA_RE.finditer(line):
            total += float(m.group(1).replace(",", ""))
            found = True
    return round(total, 2) if found else None


def extract_floor(buld_list):
    """'12층1212호', '지하1층 B101호' 에서 층을 뽑는다. 건물 전체 매각이면 층을 정하지 않는다."""
    if not isinstance(buld_list, str) or not buld_list:
        return None, ""
    m = FLOOR_RE.search(buld_list)
    if not m:
        # '지층비102호', '지하층비104호' 처럼 숫자 없이 지하만 표기한 경우
        if re.search(r"지(하)?층", buld_list):
            return None, "Y"
        # 층 표기 없이 호수만 있는 경우: '501호' → 5층, '비01호'/'B01호' → 지하
        h = HO_RE.search(buld_list)
        if h:
            if h.group(1):
                return None, "Y"
            return int(h.group(2)[:-2]), ""
        return None, ""
    return int(m.group(2)), "Y" if m.group(1) else ""


def make_pnu(row):
    """법정동코드(10) + 산여부(1: 일반, 2: 산) + 본번(4) + 부번(4). 지번이 숫자가 아니면 만들지 않는다."""
    dong = row.get("srchHjguRdCd") or ""
    lotno = (row.get("daepyoLotno") or "").strip()
    m = re.fullmatch(r"(산)?\s*(\d+)(?:-(\d+))?", lotno)
    if len(dong) != 10 or not m:
        return ""
    return f"{dong}{'2' if m.group(1) else '1'}{int(m.group(2)):04d}{int(m.group(3) or 0):04d}"


def warn_label(failed, remark):
    labels = []
    if failed >= FAILED_BID_STRONG_WARN:
        labels.append(f"🔴 유찰{failed}회")
    elif failed >= FAILED_BID_WARN:
        labels.append(f"⚠ 유찰{failed}회")
    if isinstance(remark, str):
        compact = remark.replace(" ", "")
        hits = [k for k in RISK_KEYWORDS if k.replace(" ", "") in compact]
        labels.extend(dict.fromkeys(k.replace(" ", "") for k in hits))
    return " ".join(labels)


def group_items(raw_rows, category):
    """목록 단위 행을 물건(법원+사건+물건번호) 단위로 묶는다."""
    groups = {}
    for r in raw_rows:
        key = (r["boCd"], r["saNo"], r["maemulSer"])
        groups.setdefault(key, {})[r["docid"]] = r     # 지역 조회가 겹쳐도 같은 행은 한 번만

    items = []
    for (bo_cd, sa_no, ser), docs in groups.items():
        parts = list(docs.values())
        buildings = [p for p in parts if p.get("mokGbncd") != MOK_LAND]
        lands = [p for p in parts if p.get("mokGbncd") == MOK_LAND]
        main = buildings[0] if buildings else parts[0]

        # 건물 면적은 pjbBuldList 에 있다. areaList 는 비어 있는 경우가 많다 (서울 주택 기준 약 1/3).
        bld_area = sum(filter(None, (
            sum_area(p.get("pjbBuldList") or p.get("areaList")) for p in buildings
        ))) or None
        land_area = sum(filter(None, (sum_area(p.get("areaList")) for p in lands))) or None
        floor, basement = extract_floor(main.get("buldList")) if len(buildings) == 1 else (None, "")

        appraisal = int(main.get("gamevalAmt") or 0)
        minimum = int(main.get("minmaePrice") or 0)
        failed = int(main.get("yuchalCnt") or 0)
        remark = (main.get("mulBigo") or "").strip()
        sale_dt = (main.get("maeGiil") or "") + (main.get("maeHh1") or "")

        items.append({
            "구분": category["구분"],
            "_scl": main.get("sclsUtilCd"),
            "물건관리번호": f"{main['jiwonNm']} {main['srnSaNo']}-{ser}",
            "물건명": (main.get("printSt") or "").strip(),
            "층": floor,
            "지하여부": basement,
            "용도": SCL_NAMES.get(main.get("sclsUtilCd"), main.get("dspslUsgNm") or ""),
            "시도": main.get("hjguSido", ""),
            "시군구": main.get("hjguSigu", ""),
            "읍면동": main.get("hjguDong", ""),
            "감정가": appraisal,
            "건물면적": bld_area,
            "토지면적": land_area,
            "유찰횟수": failed,
            "경고": warn_label(failed, remark),
            "지분여부": "Y" if "지분" in remark else "N",
            "명도책임": "매수인(인도명령 가능)",
            "입찰시작": sale_dt,
            "입찰마감": sale_dt,       # 기일입찰이라 매각기일 당일 마감
            "배분요구종기": "",         # 목록 응답에는 없다 (사건 상세 조회 필요)
            "상태": "진행" if main.get("mulJinYn") == "Y" else f"상태코드 {main.get('mulStatcd')}",
            "PNU": make_pnu(main),
            "최종수정": "",
            "온비드링크": "",
            # --- 경매 전용 ---
            "출처": "법원경매",
            "법원": main["jiwonNm"],
            "담당계": main.get("jpDeptNm", ""),
            "사건번호": main["srnSaNo"],
            "물건번호": int(ser),
            "최저매각가": minimum,
            "최저가율(%)": round(minimum / appraisal * 100) if appraisal else None,
            "매각기일": main.get("maeGiil", ""),
            "매각장소": main.get("maePlace", ""),
            "물건비고": remark,
        })
    return pd.DataFrame(items)


def build_dataframe(raw_rows, category):
    label = category["구분"]
    print(f"\n[{label}] 필터 적용")
    df = group_items(raw_rows, category)
    if df.empty:
        return df
    print(f"  물건 단위로 묶음: {len(raw_rows)}행 → {len(df)}건")

    if category["scl"]:
        before = len(df)
        df = df[df["_scl"].isin(category["scl"])]
        print(f"  {label} 소분류만: {before} → {len(df)}")

    cap = category["max_min_price"]
    before = len(df)
    df = df[df["최저매각가"].between(1, cap)]
    print(f"  최저매각가 {cap:,}원 이하: {before} → {len(df)}")

    before = len(df)
    if EXCLUDE_SHARE:
        df = df[df["지분여부"] != "Y"]
        print(f"  지분물건 제외: {before} → {len(df)}")

    df = df.assign(평수=(df["건물면적"] / 3.3058).round(1))
    before = len(df)
    df = df[df["평수"].fillna(0) >= MIN_PY].drop(columns="_scl").copy()
    print(f"  평수 {MIN_PY}평 이상: {before} → {len(df)}")

    if df.empty:
        return df

    # 온비드와 같은 식으로 계산하되, 가정낙찰가는 max(최저매각가 × 1.05, 감정가 × 70%) 로 잡는다.
    add_rent_columns(df, "감정가", category, min_price_col="최저매각가")
    return df


# ---------------------------------------------------------------------------

def main():
    # GitHub Actions 러너는 UTC 라 08:00 KST 실행 때 date.today() 가 전날이 된다.
    # 시작일이 과거면 검색 결과가 0건이 되므로 한국 날짜를 쓴다.
    start = datetime.now(KST).date()
    end = start + timedelta(days=BID_DAYS)
    start_s, end_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    print(f"매각기일 {start_s} ~ {end_s}")

    session = requests.Session()
    session.headers.update(HEADERS)

    for category in CATEGORIES:
        raw = []
        for target in TARGETS:
            try:
                raw.extend(collect_region(session, target, category, start_s, end_s))
            except requests.RequestException as exc:
                print(f"  ! 요청 실패: {exc}")

        print(f"\n[{category['구분']}] 총 {len(raw)}행 수집.")
        if not raw:
            continue

        df = build_dataframe(raw, category)
        if df.empty:
            save_empty(category, "court")
            continue

        save_output(
            df, category, "court", category["latest"],
            ["물건관리번호", "층", "용도", "감정가", "최저매각가", "최저가율(%)", "평수",
             "평당필요월세", "1층환산 평당필요월세", "매각기일", "경고"],
        )


if __name__ == "__main__":
    main()
