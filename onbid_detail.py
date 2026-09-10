"""
온비드 부동산 물건상세 조회 & 병합
====================================

onbid_collect2.py 로 수집해 둔 CSV(data/onbid_commercial_*.csv)를 읽어서,
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

주의
----
이 API는 "현재 입찰 중이거나 입찰예정인 물건"만 조회된다. 오래된 CSV의 물건이
이미 낙찰/마감되었으면 NODATA_ERROR 로 빈 값이 채워진다.

실행
----
    python onbid_detail.py                       가장 최근 data/onbid_commercial_*.csv 사용
    python onbid_detail.py data/onbid_commercial_2026-09-07.csv   특정 파일 지정

산출물
------
    data/onbid_commercial_detail_YYYY-MM-DD.csv
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from onbid_collect2 import SERVICE_KEY, HEADERS  # 기존 수집기의 인증키/헤더 재사용

ENDPOINT = "https://apis.data.go.kr/B010003/OnbidRlstDtlSrvc2/getRlstDtlInf2"
OUT_DIR = Path(__file__).parent / "data"


def fetch_detail(cltr_mng_no):
    """물건관리번호 하나에 대한 상세정보를 조회한다.

    같은 물건관리번호라도 회차별로 여러 건이 나올 수 있는데, 면적/임대차/등기
    정보는 회차와 무관하게 동일하므로 첫 번째 항목만 쓰면 된다.
    """
    params = {
        "serviceKey": SERVICE_KEY,
        "pageNo": 1,
        "numOfRows": 10,
        "resultType": "json",
        "cltrMngNo": cltr_mng_no,
    }
    try:
        r = requests.get(ENDPOINT, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"    ! {cltr_mng_no}: 요청 실패 ({exc})")
        return None

    header = data.get("header") or {}
    if header.get("resultCode") != "00":
        msg = header.get("resultMsg")
        if msg != "NODATA_ERROR":
            print(f"    ! {cltr_mng_no}: {msg}")
        return None

    items = (data.get("body") or {}).get("items", {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    return items[0] if items else None


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
    total = len(cltr_mng_nos)
    for i, mng_no in enumerate(cltr_mng_nos, 1):
        print(f"  [{i}/{total}] {mng_no}")
        item = fetch_detail(mng_no)

        if item is None:
            rows.append({"물건관리번호": mng_no})
            time.sleep(0.25)
            continue

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
        })
        time.sleep(0.25)

    return pd.DataFrame(rows)


def main():
    if len(sys.argv) > 1:
        src_path = Path(sys.argv[1])
    else:
        candidates = sorted(OUT_DIR.glob("onbid_commercial_*.csv"))
        candidates = [p for p in candidates if "_detail" not in p.stem]
        if not candidates:
            sys.exit("data/ 폴더에 onbid_commercial_*.csv 가 없습니다. 먼저 onbid_collect2.py 를 실행하세요.")
        src_path = candidates[-1]

    print(f"원본 CSV: {src_path}")
    base = pd.read_csv(src_path, encoding="utf-8-sig", dtype={"물건관리번호": str})
    print(f"{len(base)}건 로드")

    mng_nos = base["물건관리번호"].dropna().unique().tolist()
    print(f"\n상세조회 대상 물건관리번호 {len(mng_nos)}건")

    detail_df = build_detail_columns(mng_nos)
    merged = base.merge(detail_df, on="물건관리번호", how="left")

    stamp = datetime.now().strftime("%Y-%m-%d")
    out_path = OUT_DIR / f"onbid_commercial_detail_{stamp}.csv"
    merged.to_csv(out_path, index=False, encoding="utf-8-sig")

    ok = detail_df["면적상세"].notna().sum() if "면적상세" in detail_df else 0
    print(f"\n{len(merged)}건을 {out_path} 에 저장했습니다. (상세조회 성공 {ok}/{len(mng_nos)}건)")


if __name__ == "__main__":
    main()
