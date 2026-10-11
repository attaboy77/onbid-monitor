"""
온비드 부동산 물건상세 조회 & 병합
====================================

onbid_collect2.py 로 수집해 둔 CSV(data/onbid_commercial_*.csv, data/onbid_residential_*.csv)를 읽어서,
"차세대 온비드 부동산 물건상세 조회서비스"(OnbidRlstDtlSrvc2 / getRlstDtlInf2)를
물건관리번호(cltrMngNo)별로 호출한 뒤 아래 정보를 CSV에 새 컬럼으로 합친다.

    - 면적상세      : 지목(종별)별 면적 breakdown (예: 토지>대, 건물>건물)
    - 전유면적      : 면적상세 중 "건물"류 면적의 합계(㎡). 구분소유 등기가
                      끝난 통상적인 상가/빌라 1개 호실 물건은 이 값이 건물면적과 같다.
    - 임대차정보    : leasInfList 원문 요약 (구분/성명/보증금/차임/전입일/확정일자)
    - 보증금·차임·전입일·확정일자 : 임대차정보 중 대표 1건(등기부/배분요구 기준 값이
                      채워진 첫 항목)을 대표값 컬럼으로 분리
    - 등기현황      : rgstPrmrInfList(등기사항증명서 주요정보) 원문 요약
    - 등기권리종류  : 등기현황에 등장하는 권리종류명을 중복 제거해 나열
    - 상세조회      : 성공 / NODATA / 실패. 성공이 아니면 위 컬럼들이 비어 있다.

요청이 타임아웃·연결 오류·5xx 로 실패하면 간격을 늘려 가며 MAX_RETRIES 번까지 다시 시도한다.
여러 물건이 연달아 실패하면 API 장애로 보고 남은 물건은 호출하지 않는다 (워크플로 제한 시간 보호).
상세조회가 전부 실패해도 상세 컬럼을 비운 채 목록 데이터만으로 CSV를 만들고 경고만 남긴다.

주의
----
이 API는 "현재 입찰 중이거나 입찰예정인 물건"만 조회된다. 오래된 CSV의 물건이
이미 낙찰/마감되었으면 NODATA_ERROR 로 빈 값이 채워진다.

실행
----
    python onbid_detail.py                       상가·주택 각각 가장 최근 원본 CSV 사용
    python onbid_detail.py data/onbid_commercial_2026-09-07.csv   특정 파일 하나만 처리

산출물
------
    data/onbid_commercial_detail_YYYY-MM-DD.csv
    data/onbid_residential_detail_YYYY-MM-DD.csv
"""

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from onbid_collect2 import SERVICE_KEY, HEADERS  # 기존 수집기의 인증키/헤더 재사용

ENDPOINT = "https://apis.data.go.kr/B010003/OnbidRlstDtlSrvc2/getRlstDtlInf2"
OUT_DIR = Path(__file__).parent / "data"

SLUGS = ("commercial", "residential")   # 상가, 주택

# onbid_collect2.py 산출물(onbid_{slug}_YYYY-MM-DD.csv)만 원본으로 인정한다.
# _detail_/_norisk_ 파일까지 잡히면 상세 컬럼이 중복 병합돼 _x/_y 로 깨진다.
SRC_NAME_RE = re.compile(r"onbid_(commercial|residential)_(\d{4}-\d{2}-\d{2})\.csv")

DETAIL_COLUMNS = [
    "면적상세", "전유면적", "임대차정보", "보증금액", "차임",
    "전입일자", "확정일자", "등기현황", "등기권리종류", "상세조회",
]

TIMEOUT = 15            # 초
MAX_RETRIES = 3         # 타임아웃 등 일시적 오류일 때 추가 시도 횟수 (최대 1 + 3번 요청)
RETRY_BASE_DELAY = 2    # 재시도 간격: 2초, 4초, 8초
ABORT_AFTER = 3         # 재시도까지 다 실패한 물건이 연달아 이만큼이면 남은 물건은 건너뛴다


def warn(msg):
    """stderr 경고. GitHub Actions 에서는 워크플로 요약에 보이도록 annotation 으로 남긴다."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{msg}", flush=True)
    else:
        print(f"    ! {msg}", file=sys.stderr, flush=True)


def mask_key(text):
    """예외 메시지의 URL에 인증키가 들어가므로 로그에 남기기 전에 가린다."""
    return re.sub(r"serviceKey=[^&\s]*", "serviceKey=<KEY>", str(text))


def get_with_retry(params, label):
    """타임아웃·연결 오류·5xx·429 면 간격을 두고 다시 요청한다. 그 밖의 오류는 바로 올린다."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.get(ENDPOINT, params=params, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code < 500 and r.status_code != 429:
                return r
            error = requests.HTTPError(f"HTTP {r.status_code}", response=r)
        except (requests.Timeout, requests.ConnectionError) as exc:
            error = exc
        if attempt == MAX_RETRIES:
            raise error
        delay = RETRY_BASE_DELAY * 2 ** attempt
        print(f"    {label}: {type(error).__name__} - {delay}초 후 재시도 "
              f"({attempt + 1}/{MAX_RETRIES})", flush=True)
        time.sleep(delay)


def fetch_detail(cltr_mng_no):
    """물건관리번호 하나에 대한 상세정보를 조회한다.

    같은 물건관리번호라도 회차별로 여러 건이 나올 수 있는데, 면적/임대차/등기
    정보는 회차와 무관하게 동일하므로 첫 번째 항목만 쓰면 된다.

    반환값은 (item, 실패사유). 성공이면 실패사유가 None,
    물건이 마감돼 조회되지 않으면 "NODATA", 그 밖의 실패는 사유 문자열.
    """
    params = {
        "serviceKey": SERVICE_KEY,
        "pageNo": 1,
        "numOfRows": 10,
        "resultType": "json",
        "cltrMngNo": cltr_mng_no,
    }
    try:
        r = get_with_retry(params, cltr_mng_no)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        return None, f"요청 실패 ({mask_key(exc)})"

    # 정상 응답은 {"header": ...}, 오류 응답은 {"result": ...} 로 온다.
    header = data.get("header") or data.get("result") or {}
    code = header.get("resultCode")
    msg = header.get("resultMsg")
    if msg == "NODATA_ERROR":
        return None, "NODATA"
    if code != "00":
        return None, f"API 오류 resultCode={code} resultMsg={msg} 응답={str(data)[:200]}"

    items = ((data.get("body") or {}).get("items") or {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    if not items:
        return None, "NODATA"
    return items[0], None


def fmt_amt(v):
    if v in (None, "", 0):
        return ""
    try:
        return f"{int(float(v)):,}"
    except (TypeError, ValueError):
        return str(v)


def summarize_sqms(sqms_list):
    """면적상세 텍스트와 전유(건물)면적 합계를 만든다."""
    if not sqms_list:
        return "", None

    parts = []
    bld_total = 0.0
    has_bld = False
    for row in sqms_list:
        cland = (row.get("clandCont") or "").strip()
        sqms = (row.get("sqmsCont") or "").strip()
        note = (row.get("dtlCltrNm") or "").strip()
        piece = f"{cland} {sqms}".strip()
        if note:
            piece += f"({note})"
        parts.append(piece)

        if cland.startswith("건물"):
            has_bld = True
            try:
                bld_total += float(sqms.replace("㎡", "").replace(",", ""))
            except ValueError:
                pass

    return " / ".join(parts), (round(bld_total, 2) if has_bld else None)


def summarize_lease(leas_list):
    """임대차정보 텍스트 요약 + 대표값(보증금/차임/전입일/확정일자)."""
    if not leas_list:
        return "", None, None, None, None

    parts = []
    rep_grtee = rep_rent = rep_mvin = rep_cfmtn = None
    for row in leas_list:
        div = row.get("irstDivNm") or ""
        name = row.get("cltrInprNm") or ""
        grtee = row.get("bidGrteeAmt")
        rent = row.get("mthrAmt")
        mvin = row.get("mvinYmd")
        cfmtn = row.get("cfmtnYmd")

        piece = f"{div}({name}) 보증금 {fmt_amt(grtee) or '-'}원"
        if rent not in (None, "", 0):
            piece += f", 차임 {fmt_amt(rent)}원"
        if mvin:
            piece += f", 전입 {mvin}"
        if cfmtn:
            piece += f", 확정 {cfmtn}"
        parts.append(piece)

        # 대표값: 보증금/전입일/확정일 중 하나라도 채워진 첫 항목을 대표로 삼는다
        if rep_grtee is None and any([grtee, rent, mvin, cfmtn]):
            rep_grtee, rep_rent, rep_mvin, rep_cfmtn = grtee, rent, mvin, cfmtn

    return " | ".join(parts), rep_grtee, rep_rent, rep_mvin, rep_cfmtn


def summarize_registry(rgst_list):
    """등기현황 텍스트 요약 + 등기권리종류(중복제거) 목록."""
    if not rgst_list:
        return "", ""

    parts = []
    rights = []
    for row in rgst_list:
        kind = row.get("irstDivNm") or ""
        name = row.get("cltrInprNm") or ""
        ymd = row.get("rgstYmd") or ""
        amt = row.get("inprStngAmt")
        piece = f"{kind}({name}) {ymd}".strip()
        if amt not in (None, "", 0):
            piece += f" {fmt_amt(amt)}원"
        parts.append(piece)
        if kind and kind not in rights:
            rights.append(kind)

    return " | ".join(parts), "/".join(rights)


def build_detail_columns(cltr_mng_nos):
    rows = []
    nodata, errors = [], []
    total = len(cltr_mng_nos)
    streak = 0   # 요청 자체가 연달아 실패한 물건 수
    for i, mng_no in enumerate(cltr_mng_nos, 1):
        if streak >= ABORT_AFTER:
            rest = cltr_mng_nos[i - 1:]
            warn(f"상세조회가 {streak}건 연속 실패해 API 장애로 보고 남은 {len(rest)}건은 건너뜁니다.")
            errors.extend(rest)
            rows.extend({"물건관리번호": n, "상세조회": "실패"} for n in rest)
            break

        print(f"  [{i}/{total}] {mng_no}")
        item, reason = fetch_detail(mng_no)

        if item is None:
            if reason == "NODATA":
                nodata.append(mng_no)
                streak = 0
                warn(f"{mng_no}: 상세정보 없음(NODATA, 낙찰/마감 추정)")
            else:
                errors.append(mng_no)
                streak += 1
                warn(f"{mng_no}: 상세조회 실패 - {reason}")
            rows.append({"물건관리번호": mng_no, "상세조회": reason if reason == "NODATA" else "실패"})
            time.sleep(0.25)
            continue

        streak = 0

        area_text, exclusive_area = summarize_sqms(item.get("sqmsList"))
        lease_text, grtee, rent, mvin, cfmtn = summarize_lease(item.get("leasInfList"))
        rgst_text, rgst_kinds = summarize_registry(item.get("rgstPrmrInfList"))

        rows.append({
            "물건관리번호": mng_no,
            "면적상세": area_text,
            "전유면적": exclusive_area,
            "임대차정보": lease_text,
            "보증금액": grtee,
            "차임": rent,
            "전입일자": mvin,
            "확정일자": cfmtn,
            "등기현황": rgst_text,
            "등기권리종류": rgst_kinds,
            "상세조회": "성공",
        })
        time.sleep(0.25)

    # 전부 실패해도 컬럼 구성이 항상 같도록 맞춘다.
    detail_df = pd.DataFrame(rows).reindex(columns=["물건관리번호"] + DETAIL_COLUMNS)
    return detail_df, nodata, errors


def latest_source(slug):
    candidates = sorted(
        p for p in OUT_DIR.glob(f"onbid_{slug}_*.csv")
        if (m := SRC_NAME_RE.fullmatch(p.name)) and m.group(1) == slug
    )
    return candidates[-1] if candidates else None


def process(src_path):
    """원본 CSV 하나에 상세정보를 붙여 저장한다. 상세조회 성공 건수를 돌려준다 (대상 없으면 None)."""
    m = SRC_NAME_RE.fullmatch(src_path.name)
    slug = m.group(1) if m else "commercial"

    print(f"\n원본 CSV: {src_path}")
    base = pd.read_csv(src_path, encoding="utf-8-sig", dtype={"물건관리번호": str})
    print(f"{len(base)}건 로드")

    stale = [c for c in DETAIL_COLUMNS if c in base.columns]
    if stale:
        warn(f"원본 CSV에 이미 상세 컬럼이 있어 버리고 새로 조회합니다: {stale}")
        base = base.drop(columns=stale)

    mng_nos = base["물건관리번호"].dropna().unique().tolist()
    print(f"상세조회 대상 물건관리번호 {len(mng_nos)}건")
    if not mng_nos:
        warn(f"{src_path.name}: 상세조회 대상이 0건입니다.")
        return None

    detail_df, nodata, errors = build_detail_columns(mng_nos)
    ok = len(mng_nos) - len(nodata) - len(errors)
    print(f"상세조회 결과: 성공 {ok} / NODATA {len(nodata)} / 실패 {len(errors)} (총 {len(mng_nos)}건)")

    # 성공이 0건이어도 CSV는 만든다. 상세 컬럼은 비어 있고, 뒤 단계는 목록 데이터만으로 진행한다
    # (onbid_add_metrics.py 가 상세조회 컬럼을 보고 해당 물건을 '권리 미확인'으로 표시한다).
    if ok == 0:
        warn(f"{src_path.name}: 상세조회 성공 0건 - 상세 컬럼 없이 목록 데이터만으로 진행합니다. "
             f"(NODATA {len(nodata)}, 실패 {len(errors)})")

    merged = base.merge(detail_df, on="물건관리번호", how="left")

    stamp = datetime.now().strftime("%Y-%m-%d")
    out_path = OUT_DIR / f"onbid_{slug}_detail_{stamp}.csv"
    merged.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"{len(merged)}건을 {out_path} 에 저장했습니다. (상세조회 성공 {ok}/{len(mng_nos)}건)")
    return ok


def main():
    if len(sys.argv) > 1:
        sources = [Path(sys.argv[1])]
    else:
        sources = [p for p in (latest_source(slug) for slug in SLUGS) if p]
        if not sources:
            sys.exit("data/ 폴더에 onbid_commercial_*.csv / onbid_residential_*.csv 가 없습니다. "
                     "먼저 onbid_collect2.py 를 실행하세요.")

    results = [process(p) for p in sources]

    # 대상이 있었는데 한 건도 성공하지 못했다면 인증키나 API 문제일 가능성이 크다.
    # 경매 수집·웹페이지 등 뒤 단계는 계속 돌도록 실패로 끝내지 않고 경고만 남긴다.
    attempted = [r for r in results if r is not None]
    if attempted and sum(attempted) == 0:
        warn("상세조회 성공 0건 - 인증키(ONBID_KEY)와 API 상태를 확인하세요. 목록 데이터만으로 계속합니다.")


if __name__ == "__main__":
    main()
