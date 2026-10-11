"""
온비드 수집 결과 텔레그램 알림
==============================

onbid_add_metrics.py 까지 끝난 최신 data/onbid_commercial_detail_*.csv 를 읽어
아래 내용을 텔레그램 메시지로 보낸다.

    1. 🆕 신규 물건        : '신규' == 'Y' 인 물건 (맨 위에 따로 표시, 인수위험 있으면 ⚠️)
    2. 🏆 추천 TOP 5       : 인수위험 없는 물건 중 1층환산 평당필요월세 오름차순 상위 5건

각 물건에는 온비드 링크를 붙인다. 신규 물건도 추천 물건도 없으면 메시지를 보내지 않는다.

전송 실패(토큰 없음, 네트워크 오류, API 오류 등)는 경고만 남기고 항상 exit 0 으로
끝난다. 알림 때문에 워크플로 전체가 실패하지 않도록 하기 위함이다.

환경변수
--------
    TELEGRAM_TOKEN     봇 토큰 (BotFather 발급)
    TELEGRAM_CHAT_ID   받을 채팅 ID

실행
----
    python onbid_telegram.py                                         최신 detail CSV 사용
    python onbid_telegram.py data/onbid_commercial_detail_2026-10-11.csv
    python onbid_telegram.py --dry-run                               보내지 않고 메시지만 출력
"""

import html
import os
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
MAX_NEW = 10          # 신규 물건이 너무 많을 때 메시지가 길어지지 않도록 자른다
TG_MAX_LEN = 4096     # 텔레그램 메시지 최대 길이
RANK_COL = "1층환산 평당필요월세"


def warn(msg):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{msg}", flush=True)
    else:
        print(f"! {msg}", file=sys.stderr, flush=True)


def fmt_won(v):
    try:
        if pd.isna(v):
            return "-"
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def format_item(row, mark=""):
    """물건 1건을 텔레그램 HTML 형식 몇 줄로 만든다."""
    name = html.escape(str(row.get("물건명") or "-"))
    link = row.get("온비드링크")
    if isinstance(link, str) and link.startswith("http"):
        title = f'<a href="{html.escape(link, quote=True)}">{name}</a>'
    else:
        title = f"{name} (링크 없음)"

    lines = [f"{mark}{title}"]
    lines.append(
        f"   감정가 {fmt_won(row.get('감정가'))}원 · 필요월세 {fmt_won(row.get('필요월세'))}원"
    )
    lines.append(
        f"   1층환산 평당 {fmt_won(row.get(RANK_COL))}원 · 판정 {html.escape(str(row.get('판정') or '-'))}"
    )
    return "\n".join(lines)


def build_message(df, stamp):
    """보낼 메시지를 만든다. 보낼 내용이 없으면 None."""
    risk = df["인수위험"].fillna("") if "인수위험" in df.columns else pd.Series("", index=df.index)
    is_new = df["신규"].fillna("") == "Y" if "신규" in df.columns else pd.Series(False, index=df.index)

    new_df = df[is_new]
    safe_df = df[risk == ""]
    if RANK_COL in df.columns:
        new_df = new_df.sort_values(RANK_COL, na_position="last")
        safe_df = safe_df.sort_values(RANK_COL, na_position="last")
    top_df = safe_df.head(TOP_N)

    if new_df.empty and top_df.empty:
        return None

    parts = [
        f"<b>🏢 온비드 상가 공매 ({html.escape(stamp)})</b>",
        f"전체 {len(df)}건 · 신규 {len(new_df)}건 · 인수위험 없음 {len(safe_df)}건",
    ]

    if not new_df.empty:
        parts.append(f"\n<b>🆕 신규 물건 ({len(new_df)}건)</b>")
        for _, row in new_df.head(MAX_NEW).iterrows():
            mark = "⚠️ " if risk.get(row.name, "") else "• "
            parts.append(format_item(row, mark))
        if len(new_df) > MAX_NEW:
            parts.append(f"… 외 {len(new_df) - MAX_NEW}건")
        if any(risk.get(i, "") for i in new_df.index):
            parts.append("⚠️ = 선순위임차인 인수 위험")

    if not top_df.empty:
        parts.append(f"\n<b>🏆 인수위험 없는 물건 TOP {len(top_df)}</b> (1층환산 평당필요월세 낮은 순)")
        for rank, (_, row) in enumerate(top_df.iterrows(), 1):
            new_mark = " 🆕" if is_new.get(row.name, False) else ""
            parts.append(format_item(row, f"{rank}.{new_mark} "))

    return "\n".join(parts)


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

    if args:
        src_path = Path(args[0])
    else:
        candidates = sorted(OUT_DIR.glob("onbid_commercial_detail_*.csv"))
        if not candidates:
            warn("data/ 에 onbid_commercial_detail_*.csv 가 없어 텔레그램 알림을 건너뜁니다.")
            return
        src_path = candidates[-1]

    df = pd.read_csv(src_path, encoding="utf-8-sig", dtype={"물건관리번호": str})
    stamp = src_path.stem.replace("onbid_commercial_detail_", "")

    text = build_message(df, stamp)
    if text is None:
        print("신규 물건도 인수위험 없는 물건도 없어 텔레그램 메시지를 보내지 않습니다.")
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
