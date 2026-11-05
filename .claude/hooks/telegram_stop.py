"""Claude Code Stop 훅: 작업이 끝나면 텔레그램으로 알린다.

토큰·채팅 ID 는 프로젝트 루트의 .env (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) 에서 읽는다.
값이 없거나 전송이 실패해도 Claude Code 를 막지 않도록 항상 exit 0 으로 끝난다.
"""

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREVIEW_CHARS = 200


def read_env(path):
    values = {}
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}

    env = read_env(ROOT / ".env")
    token, chat_id = env.get("TELEGRAM_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("telegram_stop: .env 에 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 가 없어 건너뜀", file=sys.stderr)
        return

    text = f"✅ Claude Code 작업 완료 · {ROOT.name} · {datetime.now():%m-%d %H:%M}"
    last = (payload.get("last_assistant_message") or "").strip()
    if last:
        preview = last[:PREVIEW_CHARS] + ("…" if len(last) > PREVIEW_CHARS else "")
        text += f"\n\n{preview}"

    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10) as r:
            r.read()
    except Exception as exc:  # 알림 실패로 세션을 방해하지 않는다. 토큰이 섞인 URL 은 찍지 않는다.
        print(f"telegram_stop: 전송 실패 ({type(exc).__name__})", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
