"""
경매·공매 수집 결과 웹페이지 생성 (GitHub Pages)
================================================

onbid_telegram.py 와 같은 CSV(같은 날짜의 공매 detail + 경매 court)를 읽어
site/index.html 한 장으로 만든다. 데이터는 HTML 안에 JSON으로 박아 넣어서
별도 파일 요청 없이 열린다.

    - 상가/주택 탭, 출처(전체/공매/경매) 필터, 인수위험 제외 토글, 검색
    - 컬럼 머리글 클릭으로 정렬 (휴대폰에서는 카드 보기 + 정렬 드롭다운)
    - 인수위험 물건은 붉은 배경으로 표시 (판정 기준은 onbid_telegram.load_section 과 같다)
    - 물건명 옆 '🗺 지도': 카카오맵 주소 검색 (onbid_telegram.map_link, 주소를 못 뽑으면 생략)
    - 물건명에 링크: 공매는 온비드 물건 상세, 경매는 법원경매 경매사건검색 화면
      (법원경매는 물건별 고정 주소가 없다. onbid_telegram.COURT_CASE_SEARCH_URL 참고.
       대신 사건번호 옆 '번호 복사' 버튼으로 검색창에 넣을 일련번호를 복사한다)

실행
----
    python build_site.py                 가장 최근 날짜
    python build_site.py 2026-10-11      특정 날짜
"""

import json
import sys
from pathlib import Path

import pandas as pd

from onbid_telegram import (
    SECTIONS,
    court_case,
    fmt_floor,
    fmt_man,
    fmt_price,
    is_missing,
    item_link,
    latest_stamp,
    map_link,
    load_section,
    risk_notes,
    short_name,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

SITE_DIR = Path(__file__).parent / "site"


def num(v):
    """JSON 에 넣을 숫자. 비어 있으면 None."""
    if is_missing(v):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else round(f, 1)


def fmt_date(v):
    """'202610141700' / '20261014' / '2026.10.14' → '2026-10-14'."""
    if is_missing(v):
        return ""
    digits = "".join(c for c in str(v) if c.isdigit())
    if len(digits) < 8:
        return ""
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def to_record(row):
    source = row["출처"]
    court = source == "경매"
    case, case_no = "", ""
    if court:
        court_nm, head, case_no, ser = court_case(row)
        case = f"{court_nm} {head}{case_no}-{ser}".strip()

    if court:
        risk = risk_notes(row)
        deadline = fmt_date(row.get("매각기일"))
        price = row.get("최저매각가")
    else:
        risk = "" if is_missing(row.get("인수위험")) else str(row.get("인수위험"))
        deadline = fmt_date(row.get("입찰마감"))
        price = row.get("가정낙찰가")

    region = " ".join(str(row.get(c) or "") for c in ("시군구", "읍면동") if not is_missing(row.get(c)))
    failed = row.get("유찰횟수")
    return {
        "src": source,
        "name": short_name(row),
        "full": str(row.get("물건명") or ""),
        "link": item_link(row),
        "map": map_link(row),
        "case": case,
        "caseNo": case_no,
        "region": region,
        "use": "" if is_missing(row.get("용도")) else str(row.get("용도")),
        "floor": fmt_floor(row),
        "floorN": (-1 if str(row.get("지하여부") or "") == "Y" else 1) * (num(row.get("층")) or 0),
        "py": num(row.get("전유평수")),
        "appraisal": num(row.get("감정가")),
        "appraisalT": fmt_price(row.get("감정가")),
        "price": num(price),
        "priceT": fmt_price(price),
        "rent": num(row.get("필요월세")),
        "rentT": fmt_man(row.get("필요월세")),
        "pyRent": num(row.get("평당필요월세")),
        "pyRentT": fmt_man(row.get("평당필요월세")),
        "flRent": num(row.get("1층환산 평당필요월세")),
        "flRentT": fmt_man(row.get("1층환산 평당필요월세")),
        "failed": 0 if is_missing(failed) else int(failed),
        "deadline": deadline,
        "verdict": "" if is_missing(row.get("판정")) else str(row.get("판정")),
        "riskFlag": bool(row["_위험"]),
        "risk": risk,
        "new": str(row.get("신규") or "") == "Y",
    }


def build_data(stamp):
    data = {"stamp": stamp, "sections": []}
    for slug, title, rank_col, _ in SECTIONS:
        df = load_section(slug, stamp)
        records = [] if df.empty else [to_record(r) for _, r in df.iterrows()]
        data["sections"].append({
            "slug": slug,
            "title": title,
            "rankKey": "flRent" if rank_col.startswith("1층환산") else "pyRent",
            "items": records,
        })
    return data


def main():
    stamp = sys.argv[1] if len(sys.argv) > 1 else latest_stamp()
    if not stamp:
        sys.exit("data/ 에 수집 결과 CSV가 없습니다.")

    data = build_data(stamp)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", payload).replace("__STAMP__", stamp)

    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")
    # 검색엔진 노출 방지. 프로젝트 페이지(사용자.github.io/저장소/)는 도메인 루트가 아니라
    # robots.txt 가 무시되므로, HTML 의 noindex 메타 태그가 실제로 막는 역할을 한다.
    (SITE_DIR / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    counts = ", ".join(f"{s['title']} {len(s['items'])}건" for s in data["sections"])
    print(f"{SITE_DIR / 'index.html'} 생성 ({stamp}: {counts})")


TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>경매·공매 물건 __STAMP__</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #fff; --text: #1d2330; --muted: #6b7280; --line: #e3e6eb;
  --accent: #2557d6; --risk-bg: #fdecec; --risk-line: #f3b4b4; --risk-text: #b42318;
  --new: #0e7a3e; --warn: #b45309;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151b; --panel: #1b1f27; --text: #e6e8ec; --muted: #9aa3b2; --line: #2c323d;
    --accent: #7fa2ff; --risk-bg: #3a1d1f; --risk-line: #6b2b2f; --risk-text: #ff9b94;
    --new: #5ad08e; --warn: #f0b252;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif; }
header { padding: 16px 16px 8px; max-width: 1400px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: 13px; }
main { max-width: 1400px; margin: 0 auto; padding: 0 16px 32px; }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); margin: 8px 0 12px; }
.tab { border: 0; background: none; color: var(--muted); font: inherit; font-weight: 600;
  padding: 10px 14px; border-bottom: 2px solid transparent; cursor: pointer; }
.tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--accent); }
.controls { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; margin-bottom: 10px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; background: var(--panel); color: var(--text); font: inherit; padding: 6px 12px; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed="true"] { background: var(--accent); color: #fff; }
label.chk { display: inline-flex; gap: 6px; align-items: center; cursor: pointer; }
input[type=search], select { font: inherit; padding: 6px 10px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--panel); color: var(--text); }
input[type=search] { flex: 1 1 180px; min-width: 0; }
#mobileSort { display: none; }
.count { color: var(--muted); font-size: 13px; margin-bottom: 8px; }
.legend { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 14px; height: 14px; border-radius: 3px; background: var(--risk-bg); border: 1px solid var(--risk-line); }
.wrap { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; vertical-align: top; }
th { position: sticky; top: 0; background: var(--panel); font-size: 12px; color: var(--muted);
  cursor: pointer; user-select: none; }
th:hover { color: var(--text); }
th[aria-sort="ascending"]::after { content: " ▲"; }
th[aria-sort="descending"]::after { content: " ▼"; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
td.name { white-space: normal; min-width: 220px; max-width: 340px; }
td.name a { color: var(--accent); font-weight: 600; text-decoration: none; }
td.name a:hover { text-decoration: underline; }
.full, .case { display: block; color: var(--muted); font-size: 12px; }
.map { margin-left: 6px; font-size: 12px; white-space: nowrap; }
.copy { font: inherit; font-size: 11px; padding: 0 6px; margin-left: 4px; cursor: pointer;
  color: inherit; background: transparent; border: 1px solid currentColor; border-radius: 4px; }
tr.risk td { background: var(--risk-bg); }
tr.risk td:first-child { box-shadow: inset 3px 0 var(--risk-text); }
.risktxt { color: var(--risk-text); font-weight: 600; white-space: normal; }
.badge { display: inline-block; font-size: 11px; font-weight: 700; padding: 1px 6px; border-radius: 4px;
  border: 1px solid var(--line); margin-right: 4px; }
.b-공매 { color: #6d4bd1; border-color: currentColor; }
.b-경매 { color: #0b7c86; border-color: currentColor; }
.new { color: var(--new); font-weight: 700; font-size: 11px; margin-left: 4px; }
.warnN { color: var(--warn); font-weight: 700; }
.empty { padding: 32px; text-align: center; color: var(--muted); }
.k { display: none; }

@media (max-width: 760px) {
  #mobileSort { display: inline-block; }
  .wrap { background: none; border: 0; overflow: visible; }
  table, tbody, tr, td { display: block; width: 100%; }
  thead { display: none; }
  tr { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
    margin-bottom: 10px; display: grid; grid-template-columns: 1fr 1fr; gap: 2px 12px; }
  tr.risk { background: var(--risk-bg); border-color: var(--risk-line); }
  tr.risk td { background: none; }
  tr.risk td:first-child { box-shadow: none; }
  td { border: 0; padding: 2px 0; white-space: normal; text-align: left !important; }
  td.name, td.wide { grid-column: 1 / -1; max-width: none; min-width: 0; }
  td.name { margin-bottom: 4px; }
  td:empty, td.hideM { display: none; }
  .k { display: inline; color: var(--muted); font-size: 12px; margin-right: 6px; }
}
</style>
</head>
<body>
<header>
  <h1>경매·공매 물건</h1>
  <div class="sub">수집일 __STAMP__ · 서울·고양 · 공매=온비드, 경매=법원경매</div>
</header>
<main>
  <div class="tabs" role="tablist" id="tabs"></div>
  <div class="controls">
    <div class="seg" id="srcSeg">
      <button data-src="" aria-pressed="true">전체</button>
      <button data-src="공매" aria-pressed="false">공매</button>
      <button data-src="경매" aria-pressed="false">경매</button>
    </div>
    <label class="chk"><input type="checkbox" id="hideRisk"> 인수위험 제외</label>
    <label class="chk"><input type="checkbox" id="onlyNew"> 신규만</label>
    <input type="search" id="q" placeholder="지역·물건명·사건번호 검색">
    <select id="mobileSort" aria-label="정렬"></select>
  </div>
  <div class="count"><span id="count"></span> · <span class="legend"><i></i>인수위험 물건</span></div>
  <div class="wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
</main>
<script>
const DATA = __DATA__;

const COLS = [
  { key: "name", label: "물건", cls: "name", text: true },
  { key: "src", label: "출처", text: true, hideM: true },
  { key: "floorN", label: "층", n: true },
  { key: "py", label: "전유평", n: true },
  { key: "appraisal", label: "감정가", n: true },
  { key: "price", label: "최저/가정낙찰", n: true },
  { key: "rent", label: "필요월세", n: true },
  { key: "pyRent", label: "평당필요월세", n: true },
  { key: "flRent", label: "1층환산 평당", n: true, onlyCommercial: true },
  { key: "failed", label: "유찰", n: true },
  { key: "deadline", label: "입찰·매각일", text: true },
  { key: "verdict", label: "판정", text: true },
  { key: "risk", label: "인수위험·비고", text: true, cls: "wide" },
];

const state = { tab: 0, src: "", hideRisk: false, onlyNew: false, q: "", sortKey: null, dir: 1 };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function cols() {
  return COLS.filter((c) => !c.onlyCommercial || DATA.sections[state.tab].slug === "commercial");
}

function renderTabs() {
  $("tabs").innerHTML = DATA.sections.map((s, i) =>
    `<button class="tab" role="tab" data-i="${i}" aria-selected="${i === state.tab}">${esc(s.title)} <small>${s.items.length}</small></button>`
  ).join("");
}

function renderHead() {
  $("head").innerHTML = cols().map((c) => {
    const sort = state.sortKey === c.key ? (state.dir > 0 ? "ascending" : "descending") : "none";
    return `<th data-key="${c.key}" class="${c.n ? "n" : ""}" aria-sort="${sort}">${esc(c.label)}</th>`;
  }).join("");
  $("mobileSort").innerHTML = cols().flatMap((c) => [
    `<option value="${c.key}:1">${esc(c.label)} ↑</option>`,
    `<option value="${c.key}:-1">${esc(c.label)} ↓</option>`,
  ]).join("");
  $("mobileSort").value = `${state.sortKey}:${state.dir}`;
}

function cell(c, it) {
  const k = `<span class="k">${esc(c.label)}</span>`;
  switch (c.key) {
    case "name": {
      const title = it.link
        ? `<a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(it.name)}</a>`
        : esc(it.name);
      return `<td class="name"><span class="badge b-${esc(it.src)}">${esc(it.src)}</span>${title}` +
        (it.new ? `<span class="new">NEW</span>` : "") +
        (it.map ? `<a class="map" href="${esc(it.map)}" target="_blank" rel="noopener">🗺 지도</a>` : "") +
        `<span class="full">${esc(it.full)}</span>` +
        (it.case ? `<span class="case">${esc(it.case)} ` +
          `<button type="button" class="copy" data-copy="${esc(it.caseNo)}">번호 복사</button></span>` : "") + `</td>`;
    }
    case "src": return `<td class="hideM">${esc(it.src)}</td>`;
    case "floorN": return `<td class="n">${k}${esc(it.floor)}</td>`;
    case "py": return `<td class="n">${k}${it.py == null ? "-" : it.py.toFixed(1) + "평"}</td>`;
    case "appraisal": return `<td class="n">${k}${esc(it.appraisalT)}</td>`;
    case "price": return `<td class="n">${k}${esc(it.priceT)}</td>`;
    case "rent": return `<td class="n">${k}${esc(it.rentT)}</td>`;
    case "pyRent": return `<td class="n">${k}${esc(it.pyRentT)}</td>`;
    case "flRent": return `<td class="n">${k}${esc(it.flRentT)}</td>`;
    case "failed": return `<td class="n">${k}<span class="${it.failed >= 3 ? "warnN" : ""}">${it.failed}회</span></td>`;
    case "deadline": return `<td>${k}${esc(it.deadline || "-")}</td>`;
    case "verdict": return it.verdict ? `<td>${k}${esc(it.verdict)}</td>` : `<td class="hideM">-</td>`;
    case "risk": return it.risk ? `<td class="wide">${k}<span class="${it.riskFlag ? "risktxt" : ""}">${esc(it.risk)}</span></td>` : `<td class="hideM"></td>`;
  }
}

function compare(a, b) {
  const k = state.sortKey;
  const va = a[k], vb = b[k];
  const ea = va == null || va === "", eb = vb == null || vb === "";
  if (ea || eb) return ea === eb ? 0 : ea ? 1 : -1;   // 빈 값은 방향과 무관하게 항상 뒤로
  if (typeof va === "number" && typeof vb === "number") return (va - vb) * state.dir;
  return String(va).localeCompare(String(vb), "ko") * state.dir;
}

function renderBody() {
  const sec = DATA.sections[state.tab];
  const q = state.q.trim().toLowerCase();
  const rows = sec.items.filter((it) =>
    (!state.src || it.src === state.src) &&
    (!state.hideRisk || !it.riskFlag) &&
    (!state.onlyNew || it.new) &&
    (!q || [it.name, it.full, it.case, it.region, it.use].join(" ").toLowerCase().includes(q))
  ).sort(compare);
  const risky = rows.filter((it) => it.riskFlag).length;
  $("count").textContent = `${rows.length}건 표시 (인수위험 ${risky}건)`;
  $("body").innerHTML = rows.length
    ? rows.map((it) => `<tr class="${it.riskFlag ? "risk" : ""}">${cols().map((c) => cell(c, it)).join("")}</tr>`).join("")
    : `<tr><td class="empty" colspan="${cols().length}">조건에 맞는 물건이 없습니다.</td></tr>`;
}

function render() { renderTabs(); renderHead(); renderBody(); }

function setTab(i) {
  state.tab = i;
  state.sortKey = DATA.sections[i].rankKey;
  state.dir = 1;
  render();
}

$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest(".tab");
  if (b) setTab(+b.dataset.i);
});
$("head").addEventListener("click", (e) => {
  const th = e.target.closest("th");
  if (!th) return;
  const k = th.dataset.key;
  state.dir = state.sortKey === k ? -state.dir : 1;
  state.sortKey = k;
  renderHead(); renderBody();
});
$("mobileSort").addEventListener("change", (e) => {
  const [k, d] = e.target.value.split(":");
  state.sortKey = k; state.dir = +d;
  renderHead(); renderBody();
});
$("srcSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.src = b.dataset.src;
  for (const x of $("srcSeg").children) x.setAttribute("aria-pressed", x === b);
  renderBody();
});
$("hideRisk").addEventListener("change", (e) => { state.hideRisk = e.target.checked; renderBody(); });
$("onlyNew").addEventListener("change", (e) => { state.onlyNew = e.target.checked; renderBody(); });
$("q").addEventListener("input", (e) => { state.q = e.target.value; renderBody(); });
// 경매사건검색 화면의 사건번호 입력칸에 붙여 넣을 일련번호를 복사한다.
$("body").addEventListener("click", (e) => {
  const b = e.target.closest(".copy");
  if (!b) return;
  const done = () => { b.textContent = "복사됨"; setTimeout(() => { b.textContent = "번호 복사"; }, 1500); };
  if (navigator.clipboard) navigator.clipboard.writeText(b.dataset.copy).then(done, () => prompt("사건번호", b.dataset.copy));
  else prompt("사건번호", b.dataset.copy);
});

setTab(0);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
