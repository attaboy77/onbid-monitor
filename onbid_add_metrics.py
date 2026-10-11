"""
onbid_{commercial,residential}_detail CSV에 수익성/권리분석 컬럼을 추가한다.
==========================================================================

onbid_detail.py 가 만든 data/onbid_commercial_detail_*.csv (상가) 와
data/onbid_residential_detail_*.csv (주택) 를 각각 읽어
아래 컬럼을 새로 계산해서 같은 파일에 덮어쓴다. (구조화된 원본 API 응답을
다시 부르지 않고, CSV에 이미 저장된 '임대차정보'/'등기현황' 텍스트 컬럼을
다시 파싱해서 계산한다)

    전유평수          = 전유면적 / 3.3058
    평당필요월세      = 필요월세 / 전유평수                (기존엔 bldSqms 기반 '평수' 사용)
    1층환산 평당필요월세 = 평당필요월세 / 층별계수           (위 재계산값을 반영해 함께 갱신)
                        주택은 층별계수가 비어 있어 이 값도 비어 있다.

    등기최선순위일자   = 등기현황 중 '위임기관'을 제외한 권리들의 설정일자 중 가장 빠른 날짜
                        (위임기관은 KAMCO가 압류재산을 위임받은 행정정보일 뿐 등기상
                        권리가 아니므로 말소기준권리 판단에서 제외했다)

    보증금액/차임/전입일자/확정일자 (대표값)
                      = 임대차정보목록이 여러 건일 때, "전입일자가 가장 빠른 항목"을
                        대표로 삼아 채운다. 전입일자가 있는 항목이 하나도 없으면
                        (전세권만 있는 경우 등) 값이 채워진 첫 항목으로 대체한다.

    인수보증금        = 임대차정보목록 중 전입일자가 등기최선순위일자보다 빠른
                        (=선순위) 항목 '전부'의 보증금 합계. 대표 1건이 아니라
                        선순위 임차인이 여러 명이면 그만큼 합산된다.
    실질취득가        = 가정낙찰가 + 인수보증금
    인수위험          = 인수보증금 > 0 이면 "선순위임차인 인수", 아니면 빈 값
    실제수익률(%)     = 대표 차임 × 12 / 실질취득가 × 100        (차임 정보 없으면 빈 값)
    판정              = 대표 차임 >= 필요월세 → "적합" / 미만 → "미달" / 차임 정보 없음 → "확인필요"

주의
----
- 등기부상 '전세권자'처럼 전입일자 없이 확정일자만 있는 권리는 대항력(전입) 판단
  대상이 아니므로 선순위 여부 계산에서 제외했다. 정확한 권리분석은 '임대차정보'
  원문을 직접 확인하는 걸 권한다.
- 인수보증금 계산은 등기최선순위일자를 못 구했거나(등기현황 없음) 전입일자가
  있는 임대차 항목이 없으면 0으로 처리한다("인수 없음"이 아니라 "판단 불가"에
  가까우니 인수위험이 비어 있다고 무조건 안전하다고 오인하지 않도록 주의).

산출물
------
    (원본과 동일한 경로) 컬럼이 추가된 onbid_{slug}_detail_*.csv
    data/onbid_{slug}_norisk_YYYY-MM-DD.csv   (날짜는 원본 detail 파일과 같다)
        인수위험이 없는 물건만 모은 별도 CSV.
        상가는 1층환산 평당필요월세, 주택은 평당필요월세 오름차순.

실행
----
    python onbid_add_metrics.py                     상가·주택 각각 최신 detail CSV
    python onbid_add_metrics.py data/onbid_commercial_detail_2026-09-09.csv
"""

import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

OUT_DIR = Path(__file__).parent / "data"

SLUGS = ("commercial", "residential")   # 상가, 주택
DETAIL_NAME_RE = re.compile(r"onbid_(commercial|residential)_detail_(\d{4}-\d{2}-\d{2})\.csv")

PY_SQMS = 3.3058
DATE_RE = re.compile(r"\d{8}")

GRTEE_RE = re.compile(r"보증금 ([\d,]+)원")
RENT_RE = re.compile(r"차임 ([\d,]+)원")
MVIN_RE = re.compile(r"전입 (\d{8})")
CFMTN_RE = re.compile(r"확정 (\d{8})")


def earliest_registry_date(rgst_text):
    """등기현황 텍스트에서 '위임기관'을 제외한 권리들의 최빠른 설정일자를 뽑는다."""
    if not isinstance(rgst_text, str) or not rgst_text:
        return None

    earliest = None
    for seg in rgst_text.split(" | "):
        if seg.startswith("위임기관"):
            continue
        m = DATE_RE.search(seg)
        if not m:
            continue
        ymd = int(m.group())
        if earliest is None or ymd < earliest:
            earliest = ymd
    return earliest


def parse_lease_entries(lease_text):
    """'임대차정보' 텍스트를 항목별로 되돌려 파싱한다.

    onbid_detail.py 의 summarize_lease() 가 만든
    "구분(성명) 보증금 X원[, 차임 Y원][, 전입 Z][, 확정 W]" 형식 항목들을
    " | " 로 분리해서 각 항목의 보증금/차임/전입일자/확정일자를 뽑는다.
    """
    if not isinstance(lease_text, str) or not lease_text:
        return []

    entries = []
    for seg in lease_text.split(" | "):
        g = GRTEE_RE.search(seg)
        r = RENT_RE.search(seg)
        m = MVIN_RE.search(seg)
        c = CFMTN_RE.search(seg)
        entries.append({
            "grtee": int(g.group(1).replace(",", "")) if g else None,
            "rent": int(r.group(1).replace(",", "")) if r else None,
            "mvin": int(m.group(1)) if m else None,
            "cfmtn": int(c.group(1)) if c else None,
        })
    return entries


def pick_representative(entries):
    """전입일자가 가장 빠른 항목을 대표로 삼는다. 전입일자가 있는 항목이
    하나도 없으면 값이 하나라도 채워진 첫 항목으로 대체한다."""
    with_mvin = [e for e in entries if e["mvin"] is not None]
    if with_mvin:
        return min(with_mvin, key=lambda e: e["mvin"])
    for e in entries:
        if any(v is not None for v in e.values()):
            return e
    return None


def acquired_deposit_sum(entries, earliest_rgst_ymd):
    """전입일자가 등기최선순위일자보다 빠른(=선순위) 항목 전부의 보증금 합계."""
    if earliest_rgst_ymd is None:
        return 0
    total = 0
    for e in entries:
        if e["mvin"] is not None and e["mvin"] < earliest_rgst_ymd and e["grtee"]:
            total += e["grtee"]
    return total


def main():
    if len(sys.argv) > 1:
        sources = [Path(sys.argv[1])]
    else:
        sources = []
        for slug in SLUGS:
            candidates = sorted(OUT_DIR.glob(f"onbid_{slug}_detail_*.csv"))
            if candidates:
                sources.append(candidates[-1])
        if not sources:
            sys.exit("data/ 폴더에 onbid_*_detail_*.csv 가 없습니다. 먼저 onbid_detail.py 를 실행하세요.")

    for src_path in sources:
        process(src_path)


def process(src_path):
    m = DETAIL_NAME_RE.fullmatch(src_path.name)
    slug, stamp = (m.group(1), m.group(2)) if m else ("commercial", datetime.now().strftime("%Y-%m-%d"))
    # 주택은 층별계수를 쓰지 않으므로 평당필요월세로 줄 세운다.
    rank_col = "1층환산 평당필요월세" if slug == "commercial" else "평당필요월세"

    print(f"\n대상 CSV: {src_path}")
    df = pd.read_csv(src_path, encoding="utf-8-sig")
    print(f"{len(df)}건 로드")

    required = ["전유면적", "필요월세", "층별계수", "등기현황", "임대차정보", "가정낙찰가"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        sys.exit(f"필요한 컬럼이 없습니다: {missing}. onbid_detail.py 로 만든 CSV인지 확인하세요.")

    # 1. 전유평수
    df["전유평수"] = (df["전유면적"] / PY_SQMS).round(1)

    # 2. 필요월세/평당필요월세를 전유평수 기준으로 재계산
    df["평당필요월세"] = np.where(
        df["전유평수"].fillna(0) > 0,
        (df["필요월세"] / df["전유평수"]).round(-2),
        np.nan,
    )
    df["1층환산 평당필요월세"] = np.where(
        df["층별계수"].fillna(0) > 0,
        (df["평당필요월세"] / df["층별계수"]).round(-2),
        np.nan,
    )

    # 3. 등기최선순위일자
    df["등기최선순위일자"] = df["등기현황"].map(earliest_registry_date)

    # 4. 임대차정보 재파싱: 항목별 리스트, 대표값(전입일자 최빠른 항목), 선순위 보증금 합계
    lease_entries = df["임대차정보"].map(parse_lease_entries)
    reps = lease_entries.map(pick_representative)

    df["보증금액"] = reps.map(lambda r: r["grtee"] if r else None)
    df["차임"] = reps.map(lambda r: r["rent"] if r else None)
    df["전입일자"] = reps.map(lambda r: r["mvin"] if r else None)
    df["확정일자"] = reps.map(lambda r: r["cfmtn"] if r else None)

    df["인수보증금"] = [
        acquired_deposit_sum(entries, earliest)
        for entries, earliest in zip(lease_entries, df["등기최선순위일자"])
    ]

    # 5. 실질취득가
    df["실질취득가"] = df["가정낙찰가"].fillna(0) + df["인수보증금"]

    # 6. 인수위험
    df["인수위험"] = np.where(df["인수보증금"] > 0, "선순위임차인 인수", "")

    # 7. 실제수익률(%)
    df["실제수익률(%)"] = np.where(
        df["차임"].notna() & (df["실질취득가"] > 0),
        (df["차임"] * 12 / df["실질취득가"] * 100).round(2),
        np.nan,
    )

    # 8. 판정
    def verdict(row):
        if pd.isna(row["차임"]):
            return "확인필요"
        return "적합" if row["차임"] >= row["필요월세"] else "미달"

    df["판정"] = df.apply(verdict, axis=1)

    df.to_csv(src_path, index=False, encoding="utf-8-sig")

    risk_n = (df["인수위험"] == "선순위임차인 인수").sum()
    ok_n = (df["판정"] == "적합").sum()
    need_n = (df["판정"] == "확인필요").sum()
    print(f"\n{src_path} 에 컬럼을 갱신해 덮어썼습니다.")
    print(f"  선순위임차인 인수 위험: {risk_n}건")
    print(f"  판정 - 적합 {ok_n}건 / 확인필요 {need_n}건 / 미달 {len(df) - ok_n - need_n}건")

    # 9. 인수위험 없는 물건만 모아 별도 CSV
    safe = df[df["인수위험"] == ""].sort_values(rank_col, ascending=True)
    safe_path = OUT_DIR / f"onbid_{slug}_norisk_{stamp}.csv"
    safe.to_csv(safe_path, index=False, encoding="utf-8-sig")
    print(f"  인수위험 없는 물건 {len(safe)}건을 {safe_path} 에 저장했습니다.")


if __name__ == "__main__":
    main()
