"""
법원경매정보(courtauction.go.kr) XHR 캡처 도구
==============================================

브라우저를 화면에 띄워 두고, 사용자가 직접 물건상세검색을 하는 동안 발생하는
XHR/fetch 요청과 응답을 JSONL 파일에 한 줄씩 기록한다.
court_collect.py 의 엔드포인트·파라미터를 알아내기 위한 1회성 도구다.

실행
----
    pip install playwright
    python -m playwright install chromium
    python tools/court_capture.py [출력파일.jsonl]

브라우저 창을 닫으면 종료된다.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

START_URL = "https://www.courtauction.go.kr/"
MAX_BODY = 300_000   # 응답 본문 저장 상한 (문자)

out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).parent / f"court_xhr_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
)
out_file = out_path.open("w", encoding="utf-8")
count = 0


def on_response(response):
    global count
    request = response.request
    if request.resource_type not in ("xhr", "fetch"):
        return
    if "courtauction.go.kr" not in request.url:
        return
    try:
        body = response.text()
    except Exception as exc:  # 리다이렉트·바이너리 등
        body = f"<본문 읽기 실패: {exc}>"
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "method": request.method,
        "url": request.url,
        "request_headers": request.headers,
        "post_data": request.post_data,
        "status": response.status,
        "response_headers": response.headers,
        "response_body": body[:MAX_BODY],
        "truncated": len(body) > MAX_BODY,
    }
    out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    out_file.flush()
    count += 1
    print(f"[{count}] {request.method} {response.status} {request.url}", flush=True)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(locale="ko-KR", viewport={"width": 1400, "height": 900})
    context.on("response", on_response)
    page = context.new_page()
    page.goto(START_URL)
    print(f"캡처 중 → {out_path}", flush=True)
    print("물건상세검색에서 조건을 넣고 검색한 뒤, 다음 페이지/상세보기도 한 번씩 눌러 주세요.", flush=True)
    print("다 끝나면 브라우저 창을 닫으면 됩니다.", flush=True)
    # 사용자가 창을 닫을 때까지 대기
    page.wait_for_event("close", timeout=0)
    browser.close()

out_file.close()
print(f"총 {count}건 저장: {out_path}")
