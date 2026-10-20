"""
법원경매 매각물건명세서 권리분석
================================

court_collect.py 가 만든 data/court_{commercial,residential}_YYYY-MM-DD.csv 에서
상가·주택 각 TOP 20 (onbid_telegram 과 같은 정렬 기준) 의 매각물건명세서를 받아
선순위 임차인을 뽑고, 온비드와 같은 컬럼명으로 인수위험을 판정해 같은 CSV 에 덮어쓴다.

호출 구조 (2026-10 Playwright 캡처, 로그인 없이 requests 로 동작)
------------------------------------------------------------------
    courtauction  pgj15B/selectAuctnCsSrchRslt.on          물건 상세 JSON. 명세서 머리 항목
                                                           (최선순위 설정, 인수되는 권리, 지상권, 비고)과
                                                           문서 ID·작성일, 배당요구종기가 들어 있다
    courtauction  pgj15B/insertDspslGdsSpecArtcWdrwInf.on  열람 로그를 남기고 뷰어용 encParam 을 준다
    ecfs          sgvomain/selectDocVwrInf.on              문서 목록
    ecfs          sgvomain/getPdf.on                       StreamDocs 접근 토큰
    pvo           streamdocs/v4/documents/{id}             원본 PDF (Accept: application/octet-stream)
    courtauction  pgj15B/selectCurstExmndc.on              현황조사서 JSON (명세서가 없을 때만)

    - index.on 을 먼저 열어 세션 쿠키를 받고 sc-pgmid 헤더(PGJ15BM01 / SGVO201)를 붙여야 한다.
    - getPdf.on 은 화면이 보내는 키가 하나라도 빠지면 500 이다. 그래서 본문 틀을 그대로 둔다.
    - 상세 응답은 사진 base64 때문에 물건당 1~10MB 다.

개인정보
--------
명세서 PDF·현황조사서에는 임차인 실명이 있다. 이름은 어디에도 남기지 않는다.
    - PDF 는 메모리에서만 읽고 디스크에 쓰지 않는다.
    - 점유자 칸은 '새 점유자가 시작되는 행인지' 판단에만 쓰고 값은 읽지 않는다.
    - 캐시·CSV·출력에는 날짜·금액·판정만 남긴다. 명세서의 자유 기재란(인수되는 권리, 비고)도
      이름이 섞일 수 있어 원문은 저장하지 않고 판정에만 쓴다.

판정 (명세서가 있을 때)
-----------------------
    등기최선순위일자  최선순위 설정의 날짜. '(토지) … (건물) …' 처럼 여럿이면 가장 늦은 날짜를 쓴다
                      (늦을수록 선순위로 잡히는 임차인이 많아지는 보수적인 쪽)
    선순위 임차인     권원이 임차인·임차권자·전세 이고 전입(사업자등록)일자가 등기최선순위일자보다 빠른 점유자.
                      한 점유자가 출처(등기·현황조사·권리신고)별로 여러 행이면 전입은 가장 빠른 값,
                      확정은 가장 늦은 값, 보증금은 가장 큰 값, 배당요구는 가장 빠른 값으로 합친다
    인수보증금        선순위 임차인 보증금 합계. 온비드(onbid_add_metrics.py)와 같은 기준이다
    인수위험          아래에 해당하는 것을 ' / ' 로 잇는다. 비어 있으면 명세서상 인수 요인을 못 찾은 것
        선순위임차인 인수              선순위 임차인이 배당요구를 안 했거나 종기 뒤에 했거나,
                                       확정일자가 없거나 최선순위보다 늦어 보증금 잔액이 남을 수 있음
        주의(선순위임차인 배당요구)    선순위 임차인이 종기 안에 배당요구했고 확정일자도 최선순위보다 빠름
                                       (대개 전액 배당되지만 매각가가 낮으면 잔액 인수)
        주의(최선순위 불명)            최선순위 날짜를 못 읽었는데 전입한 임차인이 있음
        인수권리 있음                  '매각으로 소멸되지 아니하는 것' 란이 채워져 있음
        법정지상권                     '매각에 따라 설정된 것으로 보는 지상권' 란이 채워져 있음
        주의(명세서 비고)              비고란에 '인수' 가 있음
    권리확인          명세서 / 미검증 / 미조회 / 조회실패

명세서가 아직 없을 때 (권리확인 = 미검증)
    현황조사서의 전입일만 본다. 전입한 임차인이 있으면 (최선순위를 알면 그보다 빠른 전입만)
    인수위험 = '주의(현황조사 전입)'. 인수보증금은 비워 둔다. 현황조사서는 사건 단위라
    다른 물건번호의 임차인도 섞여 있을 수 있다 (보수적인 쪽).

TOP 20 밖 (권리확인 = 미조회)
    예전처럼 물건비고에서 뽑은 경고에 선순위·대항력·유치권·법정지상권이 있으면 그 단어를 인수위험에 넣는다.

조회 실패 (권리확인 = 조회실패)
    인수위험 = '권리 미확인(조회 실패)'. 온비드의 상세조회 실패와 같이 안전한 물건으로 섞이지 않게 한다.

캐시
----
    data/court_rights_cache.json   {"문서ID:작성일": {"점유자": [...], "used": "YYYY-MM-DD"}}
    같은 문서·작성일이면 PDF 를 다시 받지 않는다 (재작성되면 작성일이 바뀐다). 60일 넘게 안 쓴 항목은 지운다.
    상세 JSON 은 문서 ID·작성일을 알아야 해서 TOP 20 마다 매번 받는다.

실행
----
    python court_rights.py               가장 최근 날짜의 court CSV
    python court_rights.py 2026-10-20    특정 날짜
"""

import io
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pdfplumber
import requests

from court_collect import HEADERS as COURT_HEADERS
from onbid_telegram import INHERIT_RISK_WORDS, SECTIONS, warn

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

OUT_DIR = Path(__file__).parent / "data"
CACHE_PATH = OUT_DIR / "court_rights_cache.json"
CACHE_KEEP_DAYS = 60
TOP_N = 20
COURT_NAME_RE = re.compile(r"court_(commercial|residential)_(\d{4}-\d{2}-\d{2})\.csv")

CA = "https://www.courtauction.go.kr/pgj"
ECFS = "https://ecfs.scourt.go.kr/sgvo"
PVO = "https://pvo.scourt.go.kr/streamdocs/v4"
UA = COURT_HEADERS["User-Agent"]

CA_HEADERS = dict(COURT_HEADERS, **{"sc-userid": "NONUSER", "sc-pgmid": "PGJ15BM01"})
ECFS_HEADERS = {
    "User-Agent": UA,
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": ECFS + "/websquare/websquare.html?w2xPath=/sgvo/ui/sgvo200/SGVO201M01.xml",
    "sc-userid": "NONUSER",
    "sc-pgmid": "SGVO201",
}

DOC_VIEWER_PARAM_KEYS = [
    "cortCd", "csNo", "userId", "ticket", "docList", "encUserId", "userNm", "purposeCd", "docWriteNo",
    "rdngAplyTypCd", "rdngAplySeq", "pin", "fileEdmsDocId", "docNm", "comTaskTypCd", "aplyYmd", "pspTkn",
    "pspIp", "pspUrl", "lgnUrl", "mngrUserId",
]
GET_PDF_KEYS = {
    "dma_srchEdms": [
        "ecdocId", "ecdocDtlSeq", "ecdocFileSeq", "dcmevdSeq", "csNo", "rdngLimtFileYn", "extnlUserYn",
        "bubviewerYn", "jobKind", "edmsUsePurpDvsCd", "fileEdmsDocId", "vwrSoltnDocId", "rdngLimtScopDvsCd",
        "pin", "urlDvs", "searDvs", "docNm", "userId", "cortCd", "comTaskTypCd", "ecdocCrtHstDvsCd", "scPgmId",
        "docuStartPageNo", "docuLstPageNo", "docuPageNoYn", "csNoR", "cortCdR", "mngrUserId", "passFlag",
        "scinMode", "orglEcdocFileSeq",
    ],
    "dma_downloadOtpt": [
        "cortCd", "ecdocId", "ecdocDtlSeq", "fileEdmsDocId", "userId", "systmCd", "wmYn", "wmTyp", "wmTxt",
        "wmTxtLog", "wmAlignment", "wmTxtRgst", "wmShowWhenDisplayingYn", "wmShowWhenPrintingYn",
        "ecdocFileCnt", "qrYn", "qrParam", "docIssuNo", "fileNm", "invrcdCrrctExstcYn", "invrcdObjcExstcYn",
        "trnscrCrrctExstcYn", "trnscrObjcExstcYn", "dcmevdYn", "dcmevdSeq", "ecdocCrtHstDvsCd", "extnlUserYn",
        "readYn", "use",
    ],
    "dma_downloadOtptDcmevd": [
        "docuSeojWmText", "docuStartPageNo", "docuLstPageNo", "docuSeojWmTextYn", "docuSeojWmTextColor",
        "docuSeojWmImageYn", "docuSeojWmImageType",
    ],
}

# '2023. 6. 15.' / '2025년11월6일' / '2019.12.21.~'. 공백은 미리 지운다 (줄바꿈으로 날짜가 쪼개진 칸이 있다).
DATE_RE = re.compile(r"(\d{4})[.\-/년](\d{1,2})[.\-/월](\d{1,2})")
AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d{4,}")
NONE_TEXT_RE = re.compile(r"^(해당사항)?없음\.?$|^해당없음\.?$|^-$")
LESSEE_RE = re.compile(r"임차|전세")

# 점유자 표 머리글 → 쓰는 칸 (공백을 지운 머리글의 앞부분으로 찾는다)
COLUMN_PREFIXES = {
    "occupant": "점유자",
    "part": "점유부분",
    "source": "정보출처",
    "title": "점유의권원",
    "deposit": "보증금",
    "move_in": "전입",
    "fixed": "확정일자",
    "dividend": "배당",
}
TABLE_END_PREFIXES = ("<비고>", "※", "등기된부동산", "매각에따라", "비고란")
# 한 점유자의 행은 정보출처가 이 순서로 나온다. 순서가 되돌아가면 다음 점유자다.
SOURCE_ORDER = ("등기", "현황", "권리")

SPEC = "명세서"
UNVERIFIED = "미검증"
NOT_CHECKED = "미조회"
FAILED = "조회실패"


# ---------------------------------------------------------------------------
# 값 읽기
# ---------------------------------------------------------------------------

def squash(text):
    return re.sub(r"\s+", "", text or "")


def parse_dates(text):
    """칸 안의 날짜들을 YYYYMMDD 정수 목록으로."""
    found = []
    for y, m, d in DATE_RE.findall(squash(text)):
        try:
            found.append(int(date(int(y), int(m), int(d)).strftime("%Y%m%d")))
        except ValueError:
            continue
    return found


def parse_amount(text):
    """'(1)120,000,000(2)10,000,000' 처럼 여럿이면 더한다 (증액분까지 보수적으로)."""
    amounts = [int(a.replace(",", "")) for a in AMOUNT_RE.findall(squash(text))]
    return sum(amounts) if amounts else None


def has_content(text):
    """'해당사항 없음' 같은 빈 표기가 아니라 실제 내용이 있는지."""
    t = squash(text)
    return bool(t) and not NONE_TEXT_RE.match(t)


def ymd(text):
    """'20260807' 같은 8자리 문자열 → 정수. 없으면 None."""
    digits = squash(str(text or ""))
    return int(digits) if re.fullmatch(r"\d{8}", digits) else None


# ---------------------------------------------------------------------------
# 명세서 PDF → 점유자 목록 (이름 없이)
# ---------------------------------------------------------------------------

def header_ranges(cells):
    """점유자 표 머리글 행이면 {칸 이름: (x0, x1)}, 아니면 None. cells 는 [(bbox, text)]."""
    texts = [(bbox, squash(text)) for bbox, text in cells if bbox is not None]
    if not any(t.startswith("점유자") for _, t in texts) or not any("보증금" in t for _, t in texts):
        return None
    ranges = {}
    for key, prefix in COLUMN_PREFIXES.items():
        for bbox, t in texts:
            if t.startswith(prefix) or (key == "deposit" and prefix in t):
                ranges.setdefault(key, (bbox[0], bbox[2]))
    return ranges if len(ranges) == len(COLUMN_PREFIXES) else None


def table_rows(pdf):
    """모든 페이지의 표 행을 [(bbox, text)] 로. 병합돼 가려진 칸은 bbox 가 None 이다.

    다음 페이지로 이어지는 표는 마지막 행의 아래 테두리가 없어 pdfplumber 가 그 행을 표로 못 잡는다.
    그래서 페이지 마지막 표 아래의 글자들을 한 행으로 묶어 덧붙인다 (글자마다 bbox 를 단다).
    """
    for page in pdf.pages:
        tables = page.find_tables()
        for table in tables:
            for row, texts in zip(table.rows, table.extract()):
                yield list(zip(row.cells, texts))
        if tables:
            x0, _, x1, bottom = tables[-1].bbox
            words = [w for w in page.extract_words()
                     if w["top"] >= bottom - 1 and x0 <= (w["x0"] + w["x1"]) / 2 <= x1]
            if words:
                words.sort(key=lambda w: (round(w["top"]), w["x0"]))
                yield [((w["x0"], w["top"], w["x1"], w["bottom"]), w["text"]) for w in words]


def source_rank(text):
    t = squash(text)
    return next((i for i, prefix in enumerate(SOURCE_ORDER) if t.startswith(prefix)), None)


def unit_no(text):
    """점유부분의 호수 숫자 ('제301호' → '301'). 없으면 빈 문자열."""
    return "".join(re.findall(r"\d+", squash(text)))


def parse_spec_pdf(pdf_bytes):
    """명세서 PDF 의 점유자 표를 점유자 단위 dict 목록으로. 표를 못 찾으면 None.

    페이지를 넘어간 표는 머리글 없이 이어지고 칸 수도 달라서(첫 페이지 17칸, 다음 페이지 10칸)
    칸 번호가 아니라 머리글 칸의 x 범위로 값을 맞춘다.
    새 점유자의 시작은 셋 중 하나로 본다. 점유자 칸은 '내용이 있는지'만 보고 값은 읽지 않는다.
        - 점유자 칸에 내용이 있다
        - 정보출처 순서(등기→현황조사→권리신고)가 되돌아갔다
        - 점유부분의 호수가 바뀌었다
    페이지 경계에서는 점유자 칸이 빈 칸으로 이어지거나 아예 안 잡혀서 첫 조건만으로는 부족하다.
    """
    ranges, rows = None, []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for cells in table_rows(pdf):
            if ranges is None:
                ranges = header_ranges(cells)
                continue
            first = squash(next((t for b, t in cells if b is not None and t), ""))
            if first.startswith(TABLE_END_PREFIXES):
                break
            row = {}
            for bbox, text in cells:
                if bbox is None:
                    continue
                center = (bbox[0] + bbox[2]) / 2
                key = next((k for k, (x0, x1) in ranges.items() if x0 <= center <= x1), None)
                if key:
                    row[key] = f"{row.get(key, '')} {text or ''}"
            rows.append(row)
    if ranges is None:
        return None

    occupants = []
    prev_rank, prev_unit = None, ""
    for cell in rows:
        rank, unit = source_rank(cell.get("source")), unit_no(cell.get("part"))
        new = (
            not occupants
            or bool(squash(cell.get("occupant")))
            or (rank is not None and prev_rank is not None and rank <= prev_rank)
            or (unit and prev_unit and unit != prev_unit)
        )
        if new:
            occupants.append({"titles": [], "move_in": [], "fixed": [], "deposit": [], "dividend": []})
            prev_unit = ""
        prev_rank = rank if rank is not None else prev_rank
        prev_unit = unit or prev_unit
        occ = occupants[-1]
        occ["titles"].append(squash(cell.get("title")))
        occ["move_in"] += parse_dates(cell.get("move_in"))
        fixed = parse_dates(cell.get("fixed"))
        if fixed:
            occ["fixed"].append(fixed[0])
        amount = parse_amount(cell.get("deposit"))
        if amount:
            occ["deposit"].append(amount)
        occ["dividend"] += parse_dates(cell.get("dividend"))

    result = []
    for occ in occupants:
        has_values = occ["move_in"] or occ["deposit"]
        lessee = any(LESSEE_RE.search(t) for t in occ["titles"]) or (not any(occ["titles"]) and has_values)
        if not lessee or not has_values:
            continue        # 소유자·채무자 점유, '조사된 임차내역 없음' 같은 행
        result.append({
            "전입": min(occ["move_in"]) if occ["move_in"] else None,
            "확정": max(occ["fixed"]) if occ["fixed"] else None,
            "보증금": max(occ["deposit"]) if occ["deposit"] else None,
            "배당요구": min(occ["dividend"]) if occ["dividend"] else None,
        })
    return result


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------

def judge_spec(occupants, senior_date, deadline, info):
    """명세서 점유자 목록 + 머리 항목으로 (인수위험, 인수보증금, 선순위임차인수)."""
    risks = []
    deposit_sum, senior_n = 0, 0
    if senior_date is None:
        if any(o["전입"] for o in occupants):
            risks.append("주의(최선순위 불명)")
    else:
        takeover = caution = False
        for o in occupants:
            if o["전입"] is None or o["전입"] >= senior_date:
                continue
            senior_n += 1
            deposit_sum += o["보증금"] or 0
            claimed = o["배당요구"] is not None and (deadline is None or o["배당요구"] <= deadline)
            fixed_first = o["확정"] is not None and o["확정"] < senior_date
            if claimed and fixed_first:
                caution = True
            else:
                takeover = True
        if takeover:
            risks.append("선순위임차인 인수")
        elif caution:
            risks.append("주의(선순위임차인 배당요구)")

    if has_content(info.get("ndstrcRghCtt")):
        risks.append("인수권리 있음")
    if has_content(info.get("sprfcExstcDts")):
        risks.append("법정지상권")
    if "인수" in squash(info.get("gdsSpcfcRmk")):
        risks.append("주의(명세서 비고)")
    return " / ".join(risks), deposit_sum, senior_n


def judge_survey(survey, senior_date):
    """현황조사서 JSON 의 임차인 전입일만 본다. 이름 칸(intrpsNm)은 읽지 않는다."""
    move_ins = []
    for lessee in (survey or {}).get("dlt_ordTsLserLtn") or []:
        move_ins += parse_dates(lessee.get("mvinDtlCtt"))
    if senior_date is not None:
        move_ins = [d for d in move_ins if d < senior_date]
    return ("주의(현황조사 전입)" if move_ins else ""), len(move_ins)


# ---------------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------------

class CourtClient:
    RETRIES = 3

    def __init__(self):
        self.s = requests.Session()
        self.request("GET", f"{CA}/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml", headers={"User-Agent": UA})
        lst = self.ca_post("pgj002/selectCortOfcLst.on", {"cortExecrOfcDvsCd": "00079B"}, "")
        self.court_codes = {c["name"]: c["code"] for c in lst["cortOfcLst"]}

    def request(self, method, url, **kwargs):
        """서버가 연속 요청 중 연결을 끊는 일이 잦아서 연결 오류만 몇 번 다시 시도한다."""
        for attempt in range(1, self.RETRIES + 1):
            try:
                r = self.s.request(method, url, timeout=90, **kwargs)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self.RETRIES:
                    raise
                time.sleep(3 * attempt)

    def ca_post(self, path, body, submission):
        r = self.request("POST", f"{CA}/{path}", json=body, headers=dict(CA_HEADERS, submissionid=submission))
        data = r.json().get("data")
        if data is None:
            raise RuntimeError(f"{path} 응답에 data 가 없음")
        return data

    def detail(self, code, case_no, item_no):
        return self.ca_post("pgj15B/selectAuctnCsSrchRslt.on", {"dma_srchGdsDtlSrch": {
            "csNo": case_no, "cortOfcCd": code, "dspslGdsSeq": str(item_no), "pgmId": "PGJ151F01", "srchInfo": {},
        }}, "mf_wfm_mainFrame_sbm_selectGdsDtlSrchDtlInfo")["dma_result"]

    def survey(self, code, case_no):
        return self.ca_post("pgj15B/selectCurstExmndc.on", {"dma_srchCurstExmn": {
            "cortOfcCd": code, "csNo": case_no, "auctnInfOriginDvsCd": "2", "ordTsCnt": "",
        }}, "mf_wfm_mainFrame_sbm_selectCurstExmndc")

    def spec_pdf(self, code, item_no, info):
        """명세서 원본 PDF 바이트. 디스크에 쓰지 않는다."""
        spec = self.ca_post("pgj15B/insertDspslGdsSpecArtcWdrwInf.on", {"dma_dspslGdsSpecLog": {
            "cortOfcCd": code, "csNo": info["csNo"], "dspslGdsSeq": int(item_no), "orvParam": info["orvParam"],
            "dspslGdsSpcfcEcdocId": info["dspslGdsSpcfcEcdocId"], "cortAuctnMbrsId": "NONUSER", "docFlag": "1",
            "dspslDxdyPbancEcdocId": "",
        }}, "mf_wfm_mainFrame_sbm_insertDspslGdsSpecLogInfo")["dma_dspslSpcfcInfo"]

        parm = dict.fromkeys(DOC_VIEWER_PARAM_KEYS, "")
        r = self.request("POST", f"{ECFS}/sgvomain/selectDocVwrInf.on",
                         json={"dma_parm": {**parm, "encParam": spec["encParam"], "sidParam": "NA"}},
                         headers=dict(ECFS_HEADERS, submissionid="mf_sbm_docVwr"))
        docs = r.json()["data"]["dlt_rcrd"]
        rec = next((d for d in docs if d["ecdocId"] == info["dspslGdsSpcfcEcdocId"]), docs[0])

        body = {k: dict.fromkeys(keys, "") for k, keys in GET_PDF_KEYS.items()}
        body["dma_srchEdms"].update(
            ecdocId=rec["ecdocId"], ecdocDtlSeq=rec["ecdocDtlSeq"], csNo=info["csNo"], extnlUserYn="Y",
            bubviewerYn="N", jobKind="JH", rdngLimtScopDvsCd=rec["rdngLimtScopDvsCd"], docNm=rec["docNm"],
            userId="NONUSER", cortCd=code[1:],
        )
        body["dma_header"] = {"SC-Userid": "NONUSER", "SC-Pgmid": "SGVO201", "SC-Token": "NA", "LifeSpan": "", "SID": ""}
        body["dma_downloadOtpt"].update(systmCd="SGV", wmYn="Y", wmTyp="0", dcmevdYn="N", readYn="Y")
        r = self.request("POST", f"{ECFS}/sgvomain/getPdf.on", json=body,
                         headers=dict(ECFS_HEADERS, submissionid=f"mf_tac_pdf_contents_1_1_{rec['ecdocId']}_body_sbm_pdf"))
        pdf = r.json()["data"]

        r = self.request("GET", f"{PVO}/documents/{pdf['streamdocsId']}", headers={
            "User-Agent": UA, "Accept": "application/octet-stream",
            "Authorization": f"Access-Token {pdf['accessToken']}",
            "Referer": "https://pvo.scourt.go.kr/streamdocs/view/sd",
        })
        if not r.content.startswith(b"%PDF-"):
            raise RuntimeError("PDF 가 아닌 응답")
        return r.content


# ---------------------------------------------------------------------------
# 물건 하나
# ---------------------------------------------------------------------------

def check_item(client, row, cache, stamp):
    """TOP 물건 하나의 판정 컬럼 dict."""
    code = client.court_codes.get(str(row["법원"]).strip())
    if not code:
        raise RuntimeError("법원 코드를 찾지 못함")
    case_no, item_no = str(row["사건번호"]), int(row["물건번호"])
    detail = client.detail(code, case_no, item_no)
    info = detail["dspslGdsDxdyInfo"]

    senior_dates = parse_dates(info.get("tprtyRnkHypthcStngDts"))
    senior = max(senior_dates) if senior_dates else None
    deadlines = [ymd(d.get("dstrtDemnLstprdYmd")) for d in detail.get("dstrtDemnInfo") or []]
    deadline = min(filter(None, deadlines), default=None)
    out = {
        "등기최선순위일자": senior,
        "배분요구종기": f"{deadline // 10000}/{deadline // 100 % 100:02d}/{deadline % 100:02d}" if deadline else "",
        "명세서작성일": ymd(info.get("gdsSpcfcWrtYmd")),
    }

    doc_id = info.get("dspslGdsSpcfcEcdocId")
    if not doc_id or not out["명세서작성일"]:
        risk, n = judge_survey(client.survey(code, case_no), senior)
        return {**out, "권리확인": UNVERIFIED, "인수위험": risk, "인수보증금": None, "선순위임차인수": n}

    key = f"{doc_id}:{out['명세서작성일']}"
    if key not in cache:
        occupants = parse_spec_pdf(client.spec_pdf(code, item_no, info))
        if occupants is None:
            raise RuntimeError("명세서에서 점유자 표를 찾지 못함")
        cache[key] = {"점유자": occupants}
        time.sleep(0.5)
    cache[key]["used"] = stamp

    risk, deposit, n = judge_spec(cache[key]["점유자"], senior, deadline, info)
    return {**out, "권리확인": SPEC, "인수위험": risk, "인수보증금": deposit, "선순위임차인수": n}


# ---------------------------------------------------------------------------

def load_cache():
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(cache, stamp):
    cutoff = (datetime.strptime(stamp, "%Y-%m-%d") - timedelta(days=CACHE_KEEP_DAYS)).strftime("%Y-%m-%d")
    kept = {k: v for k, v in cache.items() if v.get("used", "") >= cutoff}
    CACHE_PATH.write_text(json.dumps(kept, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def latest_stamp():
    stamps = [m.group(2) for p in OUT_DIR.glob("court_*.csv") if (m := COURT_NAME_RE.fullmatch(p.name))]
    return max(stamps) if stamps else None


def legacy_risk(warning):
    """TOP 밖 물건: 물건비고에서 뽑은 경고의 권리 단어 (예전 판정과 같다)."""
    words = [w for w in INHERIT_RISK_WORDS if w in str(warning or "")]
    return "물건비고: " + " ".join(words) if words else ""


def process(path, client, cache, stamp, rank_col):
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"물건관리번호": str})
    print(f"\n대상 CSV: {path} ({len(df)}건)")
    if df.empty:
        return

    df["권리확인"] = NOT_CHECKED
    df["인수위험"] = df["경고"].map(legacy_risk)
    for col in ("인수보증금", "선순위임차인수", "등기최선순위일자", "명세서작성일"):
        df[col] = pd.NA
    df["배분요구종기"] = df["배분요구종기"].astype("object")

    top = df.sort_values(rank_col, na_position="last").head(TOP_N)
    for idx, row in top.iterrows():
        label = f"{row['법원']} {row['사건번호']}-{int(row['물건번호'])}"
        try:
            result = check_item(client, row, cache, stamp)
        except Exception as exc:  # 한 물건 실패로 나머지를 버리지 않는다
            warn(f"{label}: 권리 조회 실패 ({type(exc).__name__}: {str(exc)[:120]})")
            df.loc[idx, ["권리확인", "인수위험"]] = [FAILED, "권리 미확인(조회 실패)"]
            continue
        for col, value in result.items():
            df.at[idx, col] = value
        print(f"  {label}: {result['권리확인']} · 선순위 {result['선순위임차인수']}명 · {result['인수위험'] or '인수 요인 없음'}")
        time.sleep(0.5)

    df.to_csv(path, index=False, encoding="utf-8-sig")
    counts = df.loc[top.index, "권리확인"].value_counts().to_dict()
    risky = (df.loc[top.index, "인수위험"] != "").sum()
    print(f"  TOP {len(top)}: {counts} · 인수위험 {risky}건 → {path} 에 덮어썼습니다.")


def main():
    stamp = sys.argv[1] if len(sys.argv) > 1 else latest_stamp()
    if not stamp:
        warn("data/ 에 court_*.csv 가 없어 경매 권리분석을 건너뜁니다.")
        return

    paths = [(OUT_DIR / f"court_{slug}_{stamp}.csv", rank_col) for slug, _, rank_col, _ in SECTIONS]
    paths = [(p, c) for p, c in paths if p.exists()]
    if not paths:
        warn(f"{stamp} 의 court CSV 가 없어 경매 권리분석을 건너뜁니다.")
        return

    cache = load_cache()
    try:
        client = CourtClient()
    except Exception as exc:
        warn(f"법원경매 사이트 접속 실패로 권리분석을 건너뜁니다 ({type(exc).__name__}: {str(exc)[:120]})")
        return
    for path, rank_col in paths:
        process(path, client, cache, stamp, rank_col)
    save_cache(cache, stamp)


if __name__ == "__main__":
    main()
