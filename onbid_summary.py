"""
온비드 수집 결과 요약 (신규 물건 위주)
====================================

onbid_add_metrics.py 까지 끝난 최신 data/onbid_commercial_detail_*.csv 를 읽어
전체 현황과 '신규' == 'Y' 인 물건을 Markdown 표로 정리해 표준출력에 찍는다.

GitHub Actions에서는 $GITHUB_STEP_SUMMARY 로 리다이렉트해서 워크플로 실행 화면의
Job Summary에 남긴다 (onbid-daily.yml 참고).

실행
----
    python onbid_summary.py                                        최신 detail CSV 사용
    python onbid_summary.py data/onbid_commercial_detail_2026-09-09.csv
    python onbid_summary.py >> "$GITHUB_STEP_SUMMARY"               (Actions에서)
"""

import sys
from pathlib import Path

import pandas as pd

# Windows 콘솔(cp949)에서도 이모지/한글이 깨지지 않도록 표준출력을 UTF-8로 강제한다.
# (GitHub Actions는 이미 UTF-8이라 영향 없음)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

OUT_DIR = Path(__file__).parent / "data"


def fmt_won(v):
    try:
        if pd.isna(v):
            return "-"
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def main():
    if len(sys.argv) > 1:
        src_path = Path(sys.argv[1])
    else:
        candidates = sorted(OUT_DIR.glob("onbid_commercial_detail_*.csv"))
        if not candidates:
            print("## 🏢 온비드 상가 공매 수집 결과\n")
            print("data/ 에 onbid_commercial_detail_*.csv 가 없습니다. 먼저 수집·상세조회를 실행하세요.")
            return
        src_path = candidates[-1]

    if not src_path.exists():
        print("## 🏢 온비드 상가 공매 수집 결과\n")
        print(f"{src_path} 파일을 찾을 수 없습니다.")
        return

    df = pd.read_csv(src_path, encoding="utf-8-sig", dtype={"물건관리번호": str})

    stamp = src_path.stem.replace("onbid_commercial_detail_", "")
    print(f"## 🏢 온비드 상가 공매 수집 결과 ({stamp})\n")

    total = len(df)
    ok_n = int((df["판정"] == "적합").sum()) if "판정" in df.columns else 0
    need_n = int((df["판정"] == "확인필요").sum()) if "판정" in df.columns else 0
    fail_n = total - ok_n - need_n
    risk_n = int((df["인수위험"] == "선순위임차인 인수").sum()) if "인수위험" in df.columns else 0

    print(f"- 전체 대상: **{total}건**")
    print(f"- 판정: 적합 {ok_n}건 / 확인필요 {need_n}건 / 미달 {fail_n}건")
    print(f"- 선순위임차인 인수 위험: {risk_n}건")
    print()

    if "신규" not in df.columns:
        print("_신규 여부를 판별할 수 없습니다 (신규 컬럼 없음)._")
        return

    new_df = df[df["신규"] == "Y"].copy()

    if new_df.empty:
        print("### 🆕 신규 물건\n")
        print("오늘은 새로 뜬 물건이 없습니다.")
        return

    if "1층환산 평당필요월세" in new_df.columns:
        new_df = new_df.sort_values("1층환산 평당필요월세", ascending=True)

    print(f"### 🆕 신규 물건 ({len(new_df)}건)\n")
    print("| 물건명 | 지역 | 감정가 | 필요월세 | 1층환산 평당필요월세 | 판정 | 인수위험 | 링크 |")
    print("|---|---|---|---|---|---|---|---|")

    for _, row in new_df.iterrows():
        name = str(row.get("물건명", "") or "").replace("|", "/")
        region = " ".join(
            str(row.get(c, "")) for c in ("시도", "시군구", "읍면동")
            if pd.notna(row.get(c)) and str(row.get(c)).strip()
        )
        appraisal = fmt_won(row.get("감정가"))
        rent = fmt_won(row.get("필요월세"))
        py_rent = fmt_won(row.get("1층환산 평당필요월세"))
        verdict = row.get("판정") or "-"
        risk = row.get("인수위험") or "-"
        link = row.get("온비드링크", "")
        link_md = f"[링크]({link})" if isinstance(link, str) and link.startswith("http") else "-"
        print(f"| {name} | {region} | {appraisal} | {rent} | {py_rent} | {verdict} | {risk} | {link_md} |")


if __name__ == "__main__":
    main()
