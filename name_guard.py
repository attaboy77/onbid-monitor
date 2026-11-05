"""
사람 이름 유출 검사
===================

법원경매 데이터에는 임차인·소유자 실명이 섞여 들어올 수 있다.
    - 목록의 물건비고: '주택임차권자 홍길동', '임차인 홍길동의 …' 처럼 자유 기재
    - 매각물건명세서·현황조사서 (court_rights.py 가 읽는다)

그래서 두 겹으로 막는다.
    1) court_collect.py 는 물건비고를 저장하기 전에 mask_names() 로 이름을 '○○○' 로 가린다.
       court_rights.py 는 판정 컬럼과 캐시를 쓰기 전에 court_rights.validate_*() 로 검사하고,
       걸리면 아무것도 쓰지 않고 실패한다.
    2) 이 스크립트를 커밋 직전에 돌려 디스크에 남은 그날 court CSV 전체와 권리분석 캐시를 다시 본다.
       이름처럼 보이는 문자열이 있거나 1)에서 권리분석이 막혔으면 exit 1 로 실행을 실패시켜
       커밋·배포를 막는다.

'이름처럼 보이는 문자열'
------------------------
    사람을 가리키는 말(임차인·임차권자·소유자·채무자·신고인 …) 바로 뒤에 오는 한글 덩어리 중
    조사를 뗀 2~4글자가 흔한 성씨로 시작하는 것. 회사·기관명(주택도시보증공사)처럼 긴 덩어리,
    '변경'·'조사' 같은 일반 낱말은 뺀다. 형식이 정해진 판정 컬럼·캐시는 court_rights 쪽에서
    허용 목록으로 더 엄격하게 본다.

로그(GitHub Actions 로그는 공개 저장소면 누구나 본다)에는 위치와 글자 수만 남기고 값은 찍지 않는다.

실행
----
    python name_guard.py               가장 최근 날짜의 court CSV + 캐시
    python name_guard.py 2026-11-05    특정 날짜
"""

import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

OUT_DIR = Path(__file__).parent / "data"
CACHE_PATH = OUT_DIR / "court_rights_cache.json"
COURT_NAME_RE = re.compile(r"court_(commercial|residential)_(\d{4}-\d{2}-\d{2})\.csv")

# court_rights.py 가 쓰기 전 검사에 걸리면 GITHUB_ENV 에 이 변수를 남긴다.
BLOCKED_ENV = "COURT_RIGHTS_NAME_GUARD"

MASK = "○○○"

ROLE_WORDS = (
    "임차권자", "임차인", "전차인", "소유자", "채무자", "점유자", "양수인", "승계인", "유치권자",
    "신고인", "신청인", "공유자", "상속인", "대리인", "명의자", "배우자", "성명", "이름",
)
# 흔한 성씨 (한 글자). 복성(남궁·황보·제갈·선우·독고)은 첫 글자가 이미 들어 있다.
SURNAMES = set(
    "김이박최정강조윤장임한오서신권황안송류유전홍고문양손배백허남심노하곽성차주우구민진나지엄채원천"
    "방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예봉경사부봉빈감태계"
    "피두갈목형라독"
)
# 사람을 가리키는 말 뒤에 자주 오는 일반 낱말 (성씨 글자로 시작해도 이름이 아니다)
STOPWORDS = {
    "변경", "조사", "전입", "전액", "전부", "이외", "이며", "이고", "이나", "이상", "이하", "이후",
    "신고", "신청", "주장", "정보", "미상", "불명", "등", "한국", "서울", "경기", "주택", "공유", "공유자",
    "소유", "유치권", "우선", "기타", "가족", "본인", "명의", "현황", "점유", "주소", "주민", "임대",
    "임차", "임차인", "임차권", "임차권자", "채무자", "채권자", "소유자", "점유자", "겸", "겸소유자",
    "부부", "모두", "각각", "전원", "사망", "배당", "배당요구", "확정", "확정일자", "보증금", "대항력",
    "인수", "인도", "명도", "제출", "진술", "주식회사", "법인", "회사", "공사", "은행", "보험", "조합",
    "금융", "신탁", "대표", "대표이사", "측", "임", "이", "외", "들", "제", "소", "주", "유", "전",
}
# 이름 뒤에 붙는 조사 (긴 것부터 뗀다)
PARTICLES = sorted(
    ["으로부터", "로부터", "에게서", "에게", "께서", "으로", "이며", "이고", "이나", "와의", "과의",
     "에", "의", "은", "는", "이", "가", "을", "를", "와", "과", "로", "도", "외", "씨", "님", "등"],
    key=len, reverse=True,
)
MAX_RUN = 6     # 사람 말 뒤 한글 덩어리가 이보다 길면 회사·기관명으로 본다

NAME_CONTEXT_RE = re.compile(
    r"(" + "|".join(sorted(ROLE_WORDS, key=len, reverse=True)) + r")"
    r"(\s*[:：]?\s*\(?\s*)([가-힣]+)"
)


def _strip_particle(run):
    for p in PARTICLES:
        if len(run) > len(p) and run.endswith(p):
            return run[: -len(p)]
    return run


def _name_spans(text):
    """이름으로 보이는 부분의 (시작, 끝) 목록."""
    spans = []
    for m in NAME_CONTEXT_RE.finditer(text):
        run = m.group(3)
        if len(run) > MAX_RUN:
            continue
        name = _strip_particle(run)
        if not 2 <= len(name) <= 4 or name[0] not in SURNAMES:
            continue
        if name in STOPWORDS or run in STOPWORDS or name[:2] in STOPWORDS:
            continue
        spans.append((m.start(3), m.start(3) + len(name)))
    return spans


def find_names(text):
    """이름처럼 보이는 문자열 개수와 각 글자 수. 값 자체는 돌려주지 않는다."""
    if not isinstance(text, str):
        return []
    return [end - start for start, end in _name_spans(text)]


def mask_names(text):
    """이름처럼 보이는 부분을 '○○○' 로 바꾼다. '임차인 홍길동의' → '임차인 ○○○의'."""
    if not isinstance(text, str):
        return text
    for start, end in reversed(_name_spans(text)):
        text = text[:start] + MASK + text[end:]
    return text


def has_hangul(text):
    return bool(re.search(r"[가-힣]", str(text)))


# ---------------------------------------------------------------------------
# 파일 검사
# ---------------------------------------------------------------------------

def scan_frame(df, label):
    """DataFrame 의 모든 글자 칸에서 이름처럼 보이는 문자열을 찾는다. [(위치, 글자수)]."""
    hits = []
    for col in df.columns:
        # pandas 3 은 글자 컬럼이 object 가 아니라 str dtype 이라 dtype 으로 거르지 않고 값마다 본다.
        for idx, value in df[col].items():
            for n in find_names(value):
                hits.append((f"{label} 행 {idx} '{col}'", n))
    return hits


def scan_files(stamp):
    """그날 court CSV 전체 + 권리분석 캐시. 문제 목록 (빈 목록이면 통과)."""
    import pandas as pd
    import court_rights

    problems = []
    for path in sorted(OUT_DIR.glob(f"court_*_{stamp}.csv")):
        if not COURT_NAME_RE.fullmatch(path.name):
            continue
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        problems += [f"{where}: 이름 의심 문자열 ({n}글자)" for where, n in scan_frame(df, path.name)]
        if "권리확인" in df.columns:
            problems += [f"{path.name} {p}" for p in court_rights.validate_output(df)]

    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except ValueError:
            problems.append(f"{CACHE_PATH.name}: JSON 을 읽지 못함")
        else:
            problems += [f"{CACHE_PATH.name} {p}" for p in court_rights.validate_cache(cache)]
    return problems


def mark_blocked():
    """court_rights.py 가 쓰기 전 검사에 걸렸음을 뒤 단계(이 스크립트)에 알린다."""
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"{BLOCKED_ENV}=blocked\n")


def report(problems):
    for p in problems[:50]:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::{p}", flush=True)
        else:
            print(f"! {p}", file=sys.stderr, flush=True)
    if len(problems) > 50:
        print(f"… 외 {len(problems) - 50}건", file=sys.stderr)


def main():
    stamps = [m.group(2) for p in OUT_DIR.glob("court_*.csv") if (m := COURT_NAME_RE.fullmatch(p.name))]
    stamp = sys.argv[1] if len(sys.argv) > 1 else (max(stamps) if stamps else None)

    problems = []
    if os.environ.get(BLOCKED_ENV) == "blocked":
        problems.append("court_rights.py 가 판정 결과에서 이름 의심 문자열을 찾아 저장을 막았습니다.")
    if stamp:
        problems += scan_files(stamp)

    if problems:
        report(problems)
        sys.exit(1)
    print(f"이름 검사 통과 (court CSV {stamp or '없음'} · {CACHE_PATH.name})")


if __name__ == "__main__":
    main()
