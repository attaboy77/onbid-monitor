"""
경매·공매 수집 결과 텔레그램 알림
=================================

같은 날짜에 만들어진 아래 CSV들을 읽어 상가/주택 섹션별 TOP 5 를 텔레그램으로 보낸다.

    공매(온비드)  data/onbid_{commercial,residential}_detail_YYYY-MM-DD.csv  (onbid_add_metrics.py 까지 끝난 것)
    경매(법원)    data/court_{commercial,residential}_YYYY-MM-DD.csv        (court_collect.py 결과)

날짜는 인자로 주지 않으면 data/ 에 있는 수집 결과 중 가장 최근 날짜를 쓴다. 그 날짜 파일이 없는
출처는 건너뛴다 (며칠 전 파일을 오늘 것처럼 보내지 않기 위해).

섹션별 TOP 5 는 인수위험이 없는 물건만 골라 공매·경매를 합쳐서 줄 세운다.
    - 상가: 1층환산 평당필요월세 낮은 순
    - 주택: 평당필요월세 낮은 순 (주택은 층별계수를 쓰지 않는다)
    - 인수위험: 공매는 '인수위험' 컬럼(선순위임차인 인수), 경매는 물건비고에서 뽑은 '경고' 중
      INHERIT_RISK_WORDS 에 해당하는 단어가 있으면 제외한다.
    - 신규 물건은 🆕, 유찰 3회 이상은 ⚠ 로 표시한다.

전송 실패(토큰 없음, 네트워크 오류, API 오류 등)는 경고만 남기고 항상 exit 0 으로
끝난다. 알림 때문에 워크플로 전체가 실패하지 않도록 하기 위함이다.

환경변수
--------
    TELEGRAM_TOKEN     봇 토큰 (BotFather 발급)
    TELEGRAM_CHAT_ID   받을 채팅 ID

실행
----
    python onbid_telegram.py                         가장 최근 날짜
    python onbid_telegram.py 2026-10-11              특정 날짜
    python onbid_telegram.py --dry-run               보내지 않고 메시지만 출력
"""

import html
import os
import re
import sys
from pathlib import Path

import pandas as pd
import requests

# Windows 콘솔(cp949)에서도 이모지/한글이 깨지지 않도록 표준출력을 UTF-8로 강제한다.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# 로컬 실행 시 .env 의 TELEGRAM_* 를 읽는다. 없으면 조용히 넘어간다.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

OUT_DIR = Path(__file__).parent / "data"
TOP_N = 5
TG_MAX_LEN = 4096     # 텔레그램 메시지 최대 길이
FAILED_BID_WARN = 3   # 유찰 이 횟수 이상이면 ⚠
NAME_MAX = 28         # 줄인 물건명 최대 길이

# 섹션: (slug, 제목, 정렬 기준 컬럼, 정렬 기준 표시명)
SECTIONS = [
    ("commercial", "🏢 상가", "1층환산 평당필요월세", "1층환산 평당"),
    ("residential", "🏠 주택", "평당필요월세", "평당"),
]

# 경매 '경고' 중 낙찰자가 떠안을 수 있는 권리. 이 단어가 있으면 TOP 에서 뺀다.
INHERIT_RISK_WORDS = ("선순위", "대항력", "유치권", "법정지상권")

STAMP_RE = re.compile(r"(?:onbid|court)_(?:commercial|residential)_(\d{4}-\d{2}-\d{2})\.csv")


def warn(msg):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{msg}", flush=True)
    else:
        print(f"! {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 데이터 읽기
# ---------------------------------------------------------------------------

def latest_stamp():
    stamps = [m.group(1) for p in OUT_DIR.glob("*.csv") if (m := STAMP_RE.fullmatch(p.name))]
    return max(stamps) if stamps else None


def read_csv(path):
    if not path.exists():
        return None
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"물건관리번호": str})


def load_section(slug, stamp):
    """한 섹션의 공매·경매 물건을 합친다. 출처·인수위험·전유평수 컬럼을 맞춘다."""
    frames = []

    onbid = read_csv(OUT_DIR / f"onbid_{slug}_detail_{stamp}.csv")
    if onbid is None:
        if (OUT_DIR / f"onbid_{slug}_{stamp}.csv").exists():
            warn(f"onbid_{slug}_detail_{stamp}.csv 가 없어 공매 {slug} 를 건너뜁니다 (상세조회 실패 추정).")
    elif not onbid.empty:
        onbid["출처"] = "공매"
        onbid["_위험"] = onbid.get("인수위험", pd.Series("", index=onbid.index)).fillna("") != ""
        if "전유평수" not in onbid.columns:
            onbid["전유평수"] = onbid["평수"]
        onbid["전유평수"] = onbid["전유평수"].fillna(onbid["평수"])
        frames.append(onbid)

    court = read_csv(OUT_DIR / f"court_{slug}_{stamp}.csv")
    if court is not None and not court.empty:
        court["출처"] = "경매"
        warns = court["경고"].fillna("")
        court["_위험"] = warns.map(lambda w: any(k in w for k in INHERIT_RISK_WORDS))
        court["전유평수"] = court["평수"]   # 경매 목록의 면적은 집합건물이면 전유면적이다
        frames.append(court)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# 포맷
# ---------------------------------------------------------------------------

def is_missing(v):
    return v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip())


def fmt_price(v):
    """원 → '6.46억' / '9,800만'."""
    if is_missing(v):
        return "-"
    v = float(v)
    if v >= 100_000_000:
        return f"{v / 100_000_000:.2f}".rstrip("0").rstrip(".") + "억"
    return f"{v / 10_000:,.0f}만"


def fmt_man(v):
    """원 → '115만' / '3.5만' (월세처럼 작은 금액)."""
    if is_missing(v):
        return "-"
    man = float(v) / 10_000
    return f"{man:,.0f}만" if man >= 10 else f"{man:.1f}만"


LOT_RE = re.compile(r"산?\d+(-\d+)?")
# 건물명 뒤에 오는 동·층·호·필지 표기에서 끊는다
STOP_RE = re.compile(r"(층|호)$|^외$|필지$|^제\S*동$|^[가-힣A-Za-z]동$|^\d|^지하|^\[")


def short_name(row):
    """'서울특별시 금천구 시흥동 957-1 현진빌라 제1층 제101호' → '금천구 시흥동 현진빌라'."""
    name = str(row.get("물건명") or "")
    sgg = str(row.get("시군구") or "").split()
    sgg = sgg[-1] if sgg else ""
    dong = str(row.get("읍면동") or "").strip()

    building = ""
    m = re.search(r"\(([^)]*)\)\s*$", name)
    if m:
        # 도로명 주소: '... 2층207호 (봉천동,청룡오피스텔)'
        parts = [p.strip() for p in m.group(1).split(",")]
        building = parts[-1] if len(parts) > 1 else ""
    elif dong and dong in name:
        tokens = name.split(dong, 1)[1].split()
        lot, words = "", []
        for i, t in enumerate(tokens):
            if i == 0 and LOT_RE.fullmatch(t):
                lot = t
                continue
            if STOP_RE.search(t):
                break
            words.append(t)
        building = " ".join(words) or lot

    short = " ".join(x for x in (sgg, dong, building) if x) or name
    return short if len(short) <= NAME_MAX else short[: NAME_MAX - 1] + "…"


def fmt_floor(row):
    floor = row.get("층")
    basement = str(row.get("지하여부") or "") == "Y"
    if basement:
        return f"지하{int(floor)}층" if not is_missing(floor) else "지하"
    if is_missing(floor):
        return "층 -"
    return f"{int(floor)}층"


def risk_notes(row):
    """경고 컬럼에서 유찰 표시를 뺀 권리 메모 (경매)."""
    w = row.get("경고")
    if is_missing(w):
        return ""
    return " ".join(t for t in w.split() if "유찰" not in t and t not in ("⚠", "🔴"))


def format_item(rank, row, rank_label, rank_col):
    source = row["출처"]
    new = " 🆕" if str(row.get("신규") or "") == "Y" else ""
    name = html.escape(short_name(row))

    link = row.get("온비드링크")
    if isinstance(link, str) and link.startswith("http"):
        name = f'<a href="{html.escape(link, quote=True)}">{name}</a>'
    lines = [f"{rank}. [{source}] {name}{new}"]

    failed = row.get("유찰횟수")
    failed = 0 if is_missing(failed) else int(failed)
    failed_txt = f"유찰 {failed}회" + (" ⚠" if failed >= FAILED_BID_WARN else "")
    py = row.get("전유평수")
    py_txt = f"전유 {float(py):.1f}평" if not is_missing(py) else "전유 -"
    lines.append(f"   {fmt_floor(row)} · {failed_txt} · {py_txt}")

    prices = [f"감정 {fmt_price(row.get('감정가'))}"]
    if source == "경매":
        prices.append(f"최저 {fmt_price(row.get('최저매각가'))}")
    prices.append(f"필요월세 {fmt_man(row.get('필요월세'))}")
    lines.append("   " + " · ".join(prices))

    tail = f"   {rank_label} {fmt_man(row.get(rank_col))}"
    if source == "경매":
        # 공매 행과 합치면서 물건번호가 실수형이 되므로 정수로 되돌린다.
        case = f"{row.get('법원', '')} {row.get('사건번호', '')}-{int(row.get('물건번호'))}"
        tail += f" · {html.escape(case.strip())}"
        notes = risk_notes(row)
        if notes:
            tail += f" · {html.escape(notes)}"
    lines.append(tail)
    return "\n".join(lines)


def build_message(stamp):
    """보낼 메시지를 만든다. 보낼 내용이 없으면 None."""
    parts = [f"<b>경매·공매 알림 ({html.escape(stamp)})</b>"]
    has_items = False

    for slug, title, rank_col, rank_label in SECTIONS:
        df = load_section(slug, stamp)
        if df.empty:
            continue

        counts = df["출처"].value_counts()
        new_n = int((df.get("신규", pd.Series(dtype=str)).fillna("") == "Y").sum())
        safe = df[~df["_위험"]].sort_values(rank_col, na_position="last")
        top = safe.head(TOP_N)

        parts.append(
            f"\n<b>{title} TOP {len(top)}</b> ({rank_label} 필요월세 낮은 순, 인수위험 제외)\n"
            f"공매 {counts.get('공매', 0)} · 경매 {counts.get('경매', 0)} · 신규 {new_n}건"
        )
        for rank, (_, row) in enumerate(top.iterrows(), 1):
            parts.append(format_item(rank, row, rank_label, rank_col))
            has_items = True

    return "\n".join(parts) if has_items else None


# ---------------------------------------------------------------------------
# 전송
# ---------------------------------------------------------------------------

def truncate(text, limit=TG_MAX_LEN):
    """텔레그램 길이 제한을 넘으면 줄 단위로 잘라낸다."""
    if len(text) <= limit:
        return text
    suffix = "\n… (길이 제한으로 생략)"
    cut = text[: limit - len(suffix)]
    # 태그가 중간에 잘려 HTML 파싱 오류가 나지 않도록 마지막 완결된 줄까지만 남긴다.
    cut = cut[: cut.rfind("\n")]
    return cut + suffix


def send(token, chat_id, text):
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        raise RuntimeError(f"HTTP {resp.status_code} {data.get('description') or resp.text[:200]}")


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv[1:]

    stamp = args[0] if args else latest_stamp()
    if not stamp:
        warn("data/ 에 수집 결과 CSV가 없어 텔레그램 알림을 건너뜁니다.")
        return

    text = build_message(stamp)
    if text is None:
        print(f"{stamp} 에 보낼 물건이 없어 텔레그램 메시지를 보내지 않습니다.")
        return
    text = truncate(text)

    if dry_run:
        print(text)
        return

    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        warn("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않아 텔레그램 알림을 건너뜁니다.")
        return

    try:
        send(token, chat_id, text)
    except Exception as exc:  # 알림 실패로 워크플로가 깨지지 않도록 모든 예외를 경고로 돌린다
        # 예외 메시지의 URL에 봇 토큰이 들어갈 수 있으므로 가린다.
        warn(f"텔레그램 전송 실패: {str(exc).replace(token, '<TOKEN>')}")
        return

    print(f"텔레그램 메시지를 보냈습니다. ({len(text)}자)")


if __name__ == "__main__":
    main()
