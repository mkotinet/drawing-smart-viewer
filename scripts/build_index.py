#!/usr/bin/env python3
"""로컬 PC 1회 인덱싱 · WebP 타일링 · DS920+ 청크 동기화 도구.

사용 예:
    uv run --python 3.12 --no-project \
      --with pypdfium2 --with pillow --with pypdf --with openpyxl \
      python scripts/build_index.py --input raw_drawings --out data

    # 서버 동기화 (청크 단위 tar.gz 업로드)
    python scripts/build_index.py --sync-only --out data --url https://<도메인> --token <SYNC_TOKEN>
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.db import canonicalize_tag, extract_revision, init_drawings_db  # noqa: E402
from scripts.ocr_engine import is_ocr_available, needs_ocr, ocr_pil_image  # noqa: E402

KST = timezone(timedelta(hours=9), "KST")
PARSER_VERSION = 3

# ISA-5.1 + 공조/유틸리티/반도체 플랜트 계장 기능문자 허용 목록
INSTRUMENT_FUNCS = frozenset("""
FE FT FIT FI FIC FIR FIRC FR FRC FC FCV FV FY FQ FQI FQIC FQT FS FSH FSL FSHH FSLL FAH FAL FG FO FX FZ
PE PT PIT PI PIC PIR PR PRC PC PCV PV PY PS PSH PSL PSHH PSLL PAH PAL PAHH PALL PG PSV PSE PRV
PDT PDIT PDI PDIC PDS PDSH PDSL DPIC DPIT DPI DPT DPDT DPS DPSH DPSL DPG DPV
LE LT LIT LI LIC LIR LR LRC LC LCV LV LY LS LSH LSL LSHH LSLL LAH LAL LAHH LALL LG LO LX LZ
TE TW TT TIT TI TIC TIR TR TRC TC TCV TV TY TS TSH TSL TSHH TSLL TAH TAL TAHH TALL TG TX TDT TDI TDIC
AE AT AIT AI AIC AIR AR ARC AC AV AY AS ASH ASL AAH AAL AX ACV ACVI
BE BT BS EE ET EI ES IE IT II IIC IS JT JI JIC JS KS KI KT KC KY
HS HV HC HIC HCV HY HZ QE QT QI QIC QS QY RE RT RI RS
SE ST SIT SI SIC SC SS SSH SSL SAH SAL SY SV UA UY UX
VE VT VIT VI VS VSH VAH VY VX WE WT WIT WI WIC WS WSH WAL WY WQ YS YI YC
ZE ZT ZI ZIC ZS ZSO ZSC ZSH ZSL ZY ZV ZC ZX ZLO ZLC XV XY XS XI XC XL XZ XA
SDV BDV ESDV MOV ROV TOT OCR TCVI MVD MVI AFD FSD ZAHU ZEF
""".split())

VALVE_FUNCS = frozenset({
    "BFV", "BV", "GLV", "CHV", "GV", "NRV", "CKV", "PRV", "PSV", "SDV", "MOV", "ROV", "XV",
})

EQUIP_PREFIXES = frozenset({
    "AHU", "AH", "FCU", "EF", "SF", "CHW", "CWP", "CWWP", "CT", "HEX", "HX", "TK", "PU",
    "ICGA", "ICGV", "ICGP", "IWT", "BDF", "LACF", "OWET", "HET", "MCC", "ACB", "HRG", "TIE",
    "IOR", "MCP", "PLC", "DDC", "PNL", "JB", "FWP", "PUMP", "BLR", "CMP", "VCB", "VFD",
    "MVD", "AFD", "FSD", "OAC", "PAC", "SCR", "UPS", "TR",
})

_UNDERSCORE_BLOCK_PREFIX = frozenset({
    "REV", "OVB", "DWG", "NOTE", "PAGE", "FILE", "SHEET", "LINE", "ITEM", "DATA", "SPEC",
    "PCDP", "PRDP", "PUDP", "PQDP", "DPDF", "IMGD", "IMPR", "IMDG", "IMAF", "IMTG", "SF7544",
})

RE_PLANT_TAG = re.compile(r"\b(?P<func>[A-Z]{2,5})-(?P<unit>[A-Z0-9]{2,12})-(?P<loop>\d{2,5}[A-Z]?)\b")
RE_STD_INST = re.compile(r"\b(?:(?P<unit>[A-Z0-9]{1,4})-)?(?P<func>[A-Z]{2,5})-(?P<loop>\d{2,5}[A-Z]?)\b")
# 배관 라인 번호: 구경(50A/550A/1000A/2")-유체(CWR/IESL/PCW/CHW)-일련번호(3~7자리+선택접미사)[-스펙]
RE_LINE_NO = re.compile(
    r'(?<!\w)(?P<size>\d{1,4}(?:"|A))-(?P<fluid>[A-Z][A-Z0-9]{1,5})-(?P<lno>\d{3,7}[A-Z]?)(?:-(?P<spec>[A-Z0-9]{2,6}))?\b'
)
# 삼성/NRD-K2 표준 언더스코어 태그 (예: TIC_N2USHCW31, AHU_N2US0101, ICGP_N2GC0202B, MVD_N2USAHU0301AA)
RE_UNDERSCORE_TAG = re.compile(r"(?<!\w)\*?(?P<prefix>[A-Z]{2,6})_(?P<body>[A-Z0-9]{4,18})\b")
# 플랜트 하이픈 영숫자 태그 (예: ACB-NK2U1B4, MCC-NK2U1B4-A, IOR-N2US07J4-MECH, HET-N2US0501)
RE_HYPHEN_PLANT = re.compile(
    r"\b(?P<prefix>[A-Z]{2,5})-(?P<body>(?:N2|NK2)[A-Z0-9]{3,14}(?:-[A-Z0-9]{1,6})?)\b"
)
# 수동·인라인 밸브 태그 (예: *BFV-006R08, BV-006Q19, *GLV-006Q01)
RE_VALVE_TAG = re.compile(
    r"(?<!\w)\*?(?P<func>BFV|BV|GLV|CHV|GV|NRV|CKV|PRV|PSV|SDV|MOV|ROV|XV)-(?P<loop>\d{2,4}[A-Z]\d{2,3}[A-Z]?)\b"
)
RE_DWG_NO = re.compile(
    r"\b(?:[A-Z]{2}\d{4}-)?[A-Z0-9]{4}-(?:PID|PFD|HCD|WRP|EQP|MCW|SYS|APP|ROU|DEL|REQ|SYM)-[A-Z0-9]{4}-[A-Z0-9]{4,5}\b"
)
RE_DWG_ID = re.compile(r"\bK\d{7}_[A-Z0-9_-]{6,32}\b")
RE_DWG_ID_PARTIAL = re.compile(r"\b[0-9]{1,7}_(?:PRDP|PCDP|PUDP|PQDP|DPDF|IM[A-Z]{2})\d{3}_[A-Z0-9]{3,12}\b")
RE_EQUIP = re.compile(r"\b(?:AHU|FCU|EF|SF|CHW|CT|HX|TK|PU|P|E|V|C|B|T|MCP|PLC|DDC|MCC|PNL|JB)-\d{2,4}[A-Z]?\b")
RE_FLUID_CODE = re.compile(r"^[A-Z][A-Z0-9/]{1,9}$")

# 배관 호칭경(KS/JIS), 압력등급, 전원 표기, 프로젝트 코드 등 비계기 오탐 차단 목록
_PIPE_DN_SIZES = frozenset({
    "10A", "15A", "20A", "25A", "32A", "40A", "50A", "65A", "80A",
    "100A", "125A", "150A", "200A", "250A", "300A", "350A", "400A", "500A",
})
_SPEC_LOOPS = frozenset({"10K", "20K", "110V", "220V", "380V", "440V", "50HZ", "60HZ"})
_RE_PROJECT_CODE = re.compile(r"^(?:SF|PJ|PRJ|JOB)-\d{4}$")
_OCR_CONFUSABLE = frozenset({
    ("0", "O"), ("O", "0"), ("1", "I"), ("I", "1"), ("1", "L"), ("L", "1"),
    ("I", "L"), ("L", "I"), ("8", "B"), ("B", "8"), ("4", "A"), ("A", "4"),
    ("5", "S"), ("S", "5"), ("2", "Z"), ("Z", "2"), ("Q", "O"), ("O", "Q"),
    ("Q", "C"), ("C", "Q"), ("O", "C"), ("C", "O"), ("G", "6"), ("6", "G"),
    ("G", "C"), ("C", "G"), ("N", "I"), ("I", "N"), ("N", "M"), ("M", "N"),
})


def _ocr_edit_distance(a: str, b: str) -> float:
    """OCR 혼동 문자쌍(0↔O, 8↔B, 1↔I, Q↔C 등)에 저가중치를 둔 편집 거리."""
    if a == b:
        return 0.0
    m, n = len(a), len(b)
    if abs(m - n) > 4:
        return 99.0
    prev = [i * (0.4 if a[i - 1] == "_" else 1.0) for i in range(m + 1)]
    for j in range(1, n + 1):
        bj = b[j - 1]
        cur = [prev[0] + (0.4 if bj == "_" else 1.0)]
        for i in range(1, m + 1):
            ai = a[i - 1]
            if ai == bj:
                sub = 0.0
            elif (ai, bj) in _OCR_CONFUSABLE:
                sub = 0.5
            else:
                sub = 1.0
            cost_del = prev[i] + (0.4 if bj == "_" else 1.0)
            cost_ins = cur[i - 1] + (0.4 if ai == "_" else 1.0)
            cost_sub = prev[i - 1] + sub
            cur.append(min(cost_del, cost_ins, cost_sub))
        prev = cur
    return prev[m]


def snap_ocr_token(candidate: str, vocab: list[str], max_dist: float = 2.0) -> str | None:
    """동일 시트 확정 설비 사전과 비교해 유일한 최적 후보가 있을 때만 OCR 오타를 보정."""
    cand = re.sub(r"\s+", "_", (candidate or "").strip().upper())
    if not cand or not vocab or len(cand) < 6:
        return None
    if cand in vocab:
        return cand
    scored: list[tuple[float, str]] = []
    for ref in vocab:
        if abs(len(ref) - len(cand)) > 3:
            continue
        d = _ocr_edit_distance(cand, ref)
        if d <= max_dist:
            scored.append((d, ref))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    if len(scored) == 1 or (scored[1][0] - scored[0][0]) >= 0.5:
        return scored[0][1]
    return None


def is_valid_tag(tag: str, kind: str, func: str | None = None,
                 unit: str | None = None, loop: str | None = None) -> bool:
    """프로젝트 코드·배관 호칭경(15A/25A)·압력등급(10K)·전원(220V) 등 오탐 태그를 걸러낸다."""
    t = (tag or "").strip().upper()
    if not t:
        return False
    if _RE_PROJECT_CODE.match(t):
        return False
    if re.match(r"^K\d{7}_", t):
        return False
    parts = re.split(r"[-_]", t)
    tail = (loop or parts[-1]).upper()
    if kind != "line_no" and (tail in _PIPE_DN_SIZES or tail in _SPEC_LOOPS):
        return False
    # KS-KS-10K, AC-AC-220V 같은 기능문자 반복 노이즈 제거
    if func and unit and func.upper() == unit.upper():
        return False
    if len(parts) >= 2 and parts[0] == parts[1]:
        return False
    if kind == "instrument" and tail.endswith(("V", "K", "W", "HZ")):
        return False
    return True


def is_shx_vector_text(full_text: str) -> bool:
    """AutoCAD SHX 벡터 선분으로 변환되어 본문 검색 텍스트가 부족한 페이지를 판별."""
    return needs_ocr(full_text)


def _compute_loop_key(tag: str, kind: str, func: str | None, unit: str | None, loop: str | None) -> str:
    """동일 루프·배관 계통·판넬 그룹을 묶기 위한 정규 키 계산."""
    t = (tag or "").upper()
    if kind == "line_no" and func and loop:
        base_lno = re.sub(r"[A-Z]+$", "", loop.upper())
        return f"{func.upper()}-{base_lno or loop.upper()}"
    if loop:
        base = loop.upper().split("-")[0]
        if len(base) >= 4:
            return base
    if "_" in t:
        parts = t.split("_", 1)
        if len(parts[1]) >= 4:
            return parts[1]
    return canonicalize_tag(t)

TILE_SIZE = 1024
THUMB_W = 480
PREVIEW_W = 1600


def classify_path(rel_path: Path) -> tuple[str, str]:
    parts = rel_path.parts
    if not parts:
        return ("기타 도면", "기타")

    # 상위 다중 리비전 폴더 구조(예: 2_AFC_0/4_P&ID/..., 1_AFD_2026.09.11/...) 자동 인식
    rev_prefix = ""
    work_parts = parts
    if len(parts) >= 3 and any(k in parts[0].upper() for k in ("AFC", "AFD", "IFC", "REV", "R0", "R1", "R2")):
        rev_prefix = parts[0]
        work_parts = parts[1:]

    top = work_parts[0]
    top_upper = top.upper()
    full_upper = "/".join(parts).upper()
    name = rel_path.stem.upper()

    def _fmt(cat: str, sub: str) -> tuple[str, str]:
        if rev_prefix and rev_prefix not in sub:
            return (cat, f"{rev_prefix} · {sub}")
        return (cat, sub)

    if "P&ID" in top_upper or "P&ID" in name or "PID" in top_upper:
        if "UT" in name:
            return _fmt("P&ID", "UT공정 P&ID")
        if "GREEN" in name or "그린" in rel_path.name:
            return _fmt("P&ID", "그린공정 P&ID")
        if len(work_parts) > 2:
            return _fmt("P&ID", work_parts[1])
        return _fmt("P&ID", "공정 P&ID")

    if "SHOP" in top_upper:
        sub = work_parts[1] if len(work_parts) > 2 else "SHOP 기타"
        return _fmt("SHOP 시공도", sub)

    if "전력" in top or "POWER" in top_upper:
        if "DATASHEET" in name:
            return _fmt("전력제어", "데이터시트")
        if "-SYS-" in name:
            return _fmt("전력제어", "계통도(SYS)")
        if "-WRP-" in name:
            return _fmt("전력제어", "배선도(WRP)")
        if "-EQP-" in name or "-MCW-" in name:
            return _fmt("전력제어", "배치·주전원")
        return _fmt("전력제어", "범례·기타")

    if "제어" in top or "CONTROL" in top_upper or "AFC" in top_upper or "AFD" in top_upper or "GREEN" in full_upper:
        if "-HCD-" in name:
            return _fmt("제어 도면", "계기 결선도(HCD)")
        if "-WRP-" in name:
            return _fmt("제어 도면", "배선 평면도(WRP)")
        if "-EQP-" in name:
            return _fmt("제어 도면", "장비 배치도(EQP)")
        if "-APP-" in name:
            return _fmt("제어 도면", "부속 도면(APP)")
        if "-MCW-" in name:
            return _fmt("제어 도면", "주전원 결선(MCW)")
        if "GREEN" in full_upper or "그린" in top:
            return _fmt("제어 도면", "그린동 제어")
        return _fmt("제어 도면", "단위 설비 결선")

    return _fmt("기타 도면", top)


def safe_sheet_id(stem: str, page_no: int, total_pages: int, sha8: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not clean:
        clean = f"doc_{sha8}"
    if len(clean) > 64:
        clean = clean[:55] + "_" + sha8
    if total_pages > 1:
        return f"{clean}_{sha8[:6]}_p{page_no:03d}"
    return f"{clean}_{sha8[:6]}"


def extract_page_tokens(page) -> tuple[str, list[dict[str, Any]]]:
    """pypdfium2 텍스트 페이지에서 전체 텍스트와 정규화 좌표(0..1, 상단 원점) 포함 단어 목록을 추출."""
    pw, ph = page.get_size()
    if pw <= 0 or ph <= 0:
        return "", []
    tp = page.get_textpage()
    try:
        full_text = tp.get_text_bounded() or ""
        n_chars = tp.count_chars()
        if n_chars <= 0:
            return full_text, []
        # 본문이 비어 있는 SHX 벡터 페이지는 charbox 6만 회 조회 생략 (선택적 OCR 경로에서 좌표 복원)
        if needs_ocr(full_text):
            return full_text, []

        # 연속된 비공백 문자 묶음을 단어로 결합 + bbox 계산 (1회 벌크 텍스트 추출)
        words: list[dict[str, Any]] = []
        buf: list[str] = []
        x0, y0, x1, y1 = 1e9, 1e9, -1e9, -1e9

        def flush():
            nonlocal buf, x0, y0, x1, y1
            if not buf:
                return
            tok = "".join(buf).strip()
            if tok and x0 < x1 and y0 < y1:
                # PDF 좌표계(원점 좌하단) → 이미지 좌표계(원점 좌상단) 정규화
                nx0 = max(0.0, min(1.0, x0 / pw))
                nx1 = max(0.0, min(1.0, x1 / pw))
                ny0 = max(0.0, min(1.0, 1.0 - y1 / ph))
                ny1 = max(0.0, min(1.0, 1.0 - y0 / ph))
                if nx1 > nx0 and ny1 > ny0:
                    words.append({"text": tok, "x0": nx0, "y0": ny0, "x1": nx1, "y1": ny1})
            buf = []
            x0, y0, x1, y1 = 1e9, 1e9, -1e9, -1e9

        limit = min(n_chars, 60_000)
        bulk_chars = tp.get_text_range(0, limit)
        use_bulk = len(bulk_chars) == limit
        for idx in range(limit):
            ch = bulk_chars[idx] if use_bulk else tp.get_text_range(idx, 1)
            if not ch or ch.isspace():
                flush()
                continue
            try:
                cl, cb, cr, ct = tp.get_charbox(idx, loose=True)
            except Exception:
                continue
            if not (math.isfinite(cl) and math.isfinite(cb) and math.isfinite(cr) and math.isfinite(ct)):
                continue
            if buf and (abs(cl - x1) > max(pw * 0.025, 14.0) or abs(cb - y0) > max(ph * 0.02, 12.0)):
                flush()
            buf.append(ch)
            if cl < x0: x0 = cl
            if cb < y0: y0 = cb
            if cr > x1: x1 = cr
            if ct > y1: y1 = ct
        flush()
        return full_text, words
    finally:
        tp.close()


def extract_tags_from_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """단어 목록에서 계기(단일줄 + 버블 2~3줄 근접 클러스터)·배관 라인·밸브·설비 태그를 추출."""
    found: list[dict[str, Any]] = []
    seen_pos: set[tuple[str, int, int]] = set()
    vocab_set: set[str] = set()

    def add(tag: str, kind: str, func: str | None, unit: str | None, loop: str | None,
            x0: float, y0: float, x1: float, y1: float, conf: float, raw: str, src: str = "pdf_text"):
        clean_tag = tag.strip().upper().lstrip("*")
        if not is_valid_tag(clean_tag, kind, func, unit, loop):
            return
        canon = canonicalize_tag(clean_tag)
        if not canon:
            return
        key = (canon, round(x0 * 80), round(y0 * 80))
        if key in seen_pos:
            return
        seen_pos.add(key)
        lkey = _compute_loop_key(clean_tag, kind, func, unit, loop)
        found.append({
            "tag": clean_tag,
            "canonical_tag": canon,
            "loop_key": lkey,
            "kind": kind,
            "func": func.upper() if func else None,
            "unit": unit.upper() if unit else None,
            "loop": loop.upper() if loop else None,
            "x0": round(x0, 5), "y0": round(y0, 5), "x1": round(x1, 5), "y1": round(y1, 5),
            "confidence": round(conf, 3), "source": src, "raw_text": raw[:120],
        })
        if conf >= 0.93 and kind in ("equipment", "instrument") and len(clean_tag) >= 7:
            vocab_set.add(clean_tag)
            if loop and len(loop) >= 6:
                vocab_set.add(loop.upper())

    # 1) 단일 토큰 내 배관 라인 · 언더스코어 태그 · 플랜트 하이픈 태그 · 밸브 · 표준 ISA 계기 · 기기
    func_tokens: list[dict[str, Any]] = []
    for w in words:
        raw_txt = w["text"]
        t = raw_txt.upper().replace("–", "-").replace("—", "-").replace("−", "-")
        src = w.get("source", "pdf_text")
        wconf = float(w.get("confidence", 1.0))
        # 공백 포함 구문(OCR 풀라인)은 개별 토큰과 중복 방지 위해 긴 공백 라인은 라인번호만 검사
        is_phrase = " " in t.strip()
        matched = False

        if m := RE_LINE_NO.search(t):
            sz, fl, lno = m.group("size"), m.group("fluid"), m.group("lno")
            add(m.group(0), "line_no", fl, sz, lno,
                w["x0"], w["y0"], w["x1"], w["y1"], min(0.96, wconf), raw_txt, src)
            matched = True

        if not is_phrase:
            if not matched and (m := RE_UNDERSCORE_TAG.search(t)):
                pfx, body = m.group("prefix"), m.group("body")
                if any(ch.isdigit() for ch in body) and pfx not in _UNDERSCORE_BLOCK_PREFIX and not re.match(r"^K\d+$", pfx):
                    if pfx in INSTRUMENT_FUNCS:
                        add(f"{pfx}_{body}", "instrument", pfx, None, body,
                            w["x0"], w["y0"], w["x1"], w["y1"], min(0.97, wconf), raw_txt, src)
                        matched = True
                    elif pfx in EQUIP_PREFIXES or body.startswith(("N2", "NK2", "K2")):
                        add(f"{pfx}_{body}", "equipment", pfx, None, body,
                            w["x0"], w["y0"], w["x1"], w["y1"], min(0.95, wconf), raw_txt, src)
                        matched = True

            if not matched and (m := RE_VALVE_TAG.search(t)):
                fn, lp = m.group("func"), m.group("loop")
                add(f"{fn}-{lp}", "valve", fn, None, lp,
                    w["x0"], w["y0"], w["x1"], w["y1"], min(0.95, wconf), raw_txt, src)
                matched = True

            if not matched and (m := RE_HYPHEN_PLANT.search(t)):
                pfx, body = m.group("prefix"), m.group("body")
                if pfx not in _UNDERSCORE_BLOCK_PREFIX:
                    kind = "instrument" if pfx in INSTRUMENT_FUNCS else "equipment"
                    add(f"{pfx}-{body}", kind, pfx, None, body,
                        w["x0"], w["y0"], w["x1"], w["y1"], min(0.95, wconf), raw_txt, src)
                    matched = True

            if not matched and (m := RE_PLANT_TAG.search(t)):
                fn = m.group("func")
                if fn in INSTRUMENT_FUNCS:
                    add(m.group(0), "instrument", fn, m.group("unit"), m.group("loop"),
                        w["x0"], w["y0"], w["x1"], w["y1"], min(0.98, wconf), raw_txt, src)
                    matched = True

            if not matched and (m := RE_STD_INST.search(t)):
                fn = m.group("func")
                if fn in INSTRUMENT_FUNCS:
                    add(m.group(0), "instrument", fn, m.group("unit"), m.group("loop"),
                        w["x0"], w["y0"], w["x1"], w["y1"], min(0.95, wconf), raw_txt, src)
                    matched = True

            if not matched and (m := RE_EQUIP.search(t)):
                add(m.group(0), "equipment", None, None, None,
                    w["x0"], w["y0"], w["x1"], w["y1"], min(0.90, wconf), raw_txt, src)
                matched = True

            clean_single = t.strip("*()")
            if not matched and clean_single in INSTRUMENT_FUNCS:
                func_tokens.append(w)

    # 2) 원형·사각 버블 2줄(FUNC / LOOP 또는 FUNC / N2USCW31) 및 3줄 수직 근접 결합 + OCR 설비사전 보정
    vocab_list = sorted(vocab_set)
    for fw in func_tokens:
        fx = (fw["x0"] + fw["x1"]) / 2
        fy = (fw["y0"] + fw["y1"]) / 2
        fh = max(fw["y1"] - fw["y0"], 0.006)
        fn = fw["text"].upper().strip("*()")
        fsrc = fw.get("source", "pdf_text")
        below = [
            w for w in words
            if w is not fw
            and (" " not in w["text"].strip() or (w.get("source") == "ocr" and len(w["text"].strip()) <= 20))
            and abs(((w["x0"] + w["x1"]) / 2) - fx) <= max(fh * 3.8, 0.032)
            and 0 < ((w["y0"] + w["y1"]) / 2) - fy <= max(fh * 4.5, 0.038)
        ]
        # 중복 분할 토큰 제거(같은 y0/x0 근접 시 가장 긴 토큰 선택)
        below.sort(key=lambda w: (round(w["y0"], 3), -len(w["text"])))
        dedup_below: list[dict[str, Any]] = []
        for bw in below:
            if dedup_below and abs(bw["y0"] - dedup_below[-1]["y0"]) < 0.004:
                continue
            dedup_below.append(bw)

        if len(dedup_below) >= 2:
            u_txt = dedup_below[0]["text"].upper().strip()
            l_txt = dedup_below[1]["text"].upper().strip()
            if re.fullmatch(r"[A-Z0-9]{2,12}", u_txt) and re.fullmatch(r"\d{2,5}[A-Z]?", l_txt):
                tag = f"{fn}-{u_txt}-{l_txt}"
                bx0 = min(fw["x0"], dedup_below[0]["x0"], dedup_below[1]["x0"])
                by0 = min(fw["y0"], dedup_below[0]["y0"], dedup_below[1]["y0"])
                bx1 = max(fw["x1"], dedup_below[0]["x1"], dedup_below[1]["x1"])
                by1 = max(fw["y1"], dedup_below[0]["y1"], dedup_below[1]["y1"])
                add(tag, "instrument", fn, u_txt, l_txt, bx0, by0, bx1, by1, 0.90,
                    f"{fn} / {u_txt} / {l_txt}", fsrc)
                continue

        if len(dedup_below) >= 1:
            b0 = dedup_below[0]
            raw_b0 = b0["text"].upper().strip()
            l_txt = re.sub(r"\s+", "_", raw_b0)
            bx0 = min(fw["x0"], b0["x0"])
            by0 = min(fw["y0"], b0["y0"])
            bx1 = max(fw["x1"], b0["x1"])
            by1 = max(fw["y1"], b0["y1"])
            bconf = float(b0.get("confidence", 1.0))

            # 2a) 표준 숫자 루프 (예: TE / 101A → TE-101A)
            if re.fullmatch(r"\d{2,5}[A-Z]?", l_txt):
                add(f"{fn}-{l_txt}", "instrument", fn, None, l_txt, bx0, by0, bx1, by1,
                    min(0.88, bconf), f"{fn} / {raw_b0}", fsrc)
                continue

            # 2b) NRD-K2 / 플랜트 영숫자 버블 (예: TCV / N2USCW31, DPIC / N2USCW31, HS / ICGP_N2GC0202B)
            snapped = None
            if fsrc == "ocr" and bconf < 0.95 and vocab_list:
                snapped = snap_ocr_token(l_txt, vocab_list)
            effective_body = snapped or l_txt
            if (
                re.fullmatch(r"[A-Z0-9_]{5,18}", effective_body)
                and any(c.isdigit() for c in effective_body)
                and any(c.isalpha() for c in effective_body)
                and effective_body not in _PIPE_DN_SIZES
                and not effective_body.endswith(("KPA", "BAR", "MMH", "RPM", "CMH", "M3H"))
            ):
                sep = "_"
                tag = f"{fn}{sep}{effective_body}"
                raw_note = f"{fn} / {raw_b0}" + (f" → {effective_body}" if snapped and snapped != l_txt else "")
                conf = 0.92 if (snapped or fsrc == "pdf_text") else min(0.86, bconf)
                add(tag, "instrument", fn, None, effective_body, bx0, by0, bx1, by1, conf, raw_note, fsrc)

    return found


def extract_dwg_meta_and_continuations(
    words: list[dict[str, Any]],
    full_text: str,
    pdf_stem: str,
) -> tuple[str | None, str, list[dict[str, Any]]]:
    """시트 자체의 drawing_no · drawing_id와 Off-Page 도면 간 연결 화살표(FR/TO)를 추출."""
    # 1) 도면번호 (파일명 → 표제란 우하단 → 본문 순)
    dwg_no: str | None = None
    if m := RE_DWG_NO.search(pdf_stem.upper()):
        dwg_no = m.group(0)
    if not dwg_no:
        tb_words = [w for w in words if w["x0"] >= 0.48 and w["y0"] >= 0.72]
        for w in tb_words:
            if m := RE_DWG_NO.search(w["text"].upper()):
                dwg_no = m.group(0)
                break
    if not dwg_no:
        if m := RE_DWG_NO.search((full_text or "").upper()):
            dwg_no = m.group(0)

    # 2) 시트 고유 drawing_id (표제란 우하단 K0919000_... 우선)
    own_dwg_id = ""
    dwg_id_words: list[tuple[str, dict[str, Any]]] = []
    for w in words:
        ut = w["text"].upper()
        if m := RE_DWG_ID.search(ut):
            dwg_id_words.append((m.group(0), w))
        elif w["x0"] >= 0.50 and w["y0"] >= 0.74 and (m := RE_DWG_ID_PARTIAL.search(ut)):
            dwg_id_words.append((m.group(0), w))

    for did, w in dwg_id_words:
        if w["x0"] >= 0.50 and w["y0"] >= 0.74:
            own_dwg_id = did
            break
    if not own_dwg_id and len(dwg_id_words) == 1:
        own_dwg_id = dwg_id_words[0][0]

    # 3) Off-Page 도면 간 연결 화살표 추출 (도면 목록 테이블 DEL 제외: 주변 FR/TO 또는 가장자리 검증)
    continuations: list[dict[str, Any]] = []
    seen_c: set[tuple[str, int, int]] = set()
    is_dwg_list = "-DEL-" in (dwg_no or "") or "-DEL-" in pdf_stem.upper()
    if is_dwg_list:
        return dwg_no, own_dwg_id, []

    for did, dw in dwg_id_words:
        # 표제란 내부 제외, 자기 자신 도면 ID 제외
        if dw["x0"] >= 0.58 and dw["y0"] >= 0.75:
            continue
        if own_dwg_id and (did == own_dwg_id or did.endswith(own_dwg_id[-8:])):
            continue

        cx = (dw["x0"] + dw["x1"]) / 2
        cy = (dw["y0"] + dw["y1"]) / 2
        above = [
            w for w in words
            if w is not dw
            and abs(((w["x0"] + w["x1"]) / 2) - cx) <= 0.09
            and 0 < (cy - (w["y0"] + w["y1"]) / 2) <= 0.042
        ]
        below = [
            w for w in words
            if w is not dw
            and abs(((w["x0"] + w["x1"]) / 2) - cx) <= 0.11
            and 0 < ((w["y0"] + w["y1"]) / 2 - cy) <= 0.045
        ]
        above.sort(key=lambda w: cy - (w["y0"] + w["y1"]) / 2)
        below.sort(key=lambda w: (round(w["y0"], 3), w["x0"]))

        fluid = ""
        for aw in above:
            tok = aw["text"].strip().upper()
            if RE_FLUID_CODE.match(tok) and tok not in {"REV", "NO", "DWG", "NOTE"}:
                fluid = tok
                break

        direction = ""
        label_parts: list[str] = []
        if below:
            first_y = below[0]["y0"]
            same_line = [bw for bw in below if abs(bw["y0"] - first_y) <= 0.012]
            same_line.sort(key=lambda w: w["x0"])
            line_txt = " ".join(bw["text"].strip() for bw in same_line).strip()
            up_line = line_txt.upper()
            if up_line.startswith(("FR ", "FROM ")):
                direction = "FR"
                label_parts.append(line_txt)
            elif up_line.startswith("TO "):
                direction = "TO"
                label_parts.append(line_txt)
            elif line_txt:
                label_parts.append(line_txt)

        near_edge = dw["x0"] < 0.24 or dw["x1"] > 0.76 or dw["y0"] < 0.16 or dw["y1"] > 0.74
        if not direction and not (fluid and near_edge):
            continue

        ckey = (did, round(dw["x0"] * 40), round(dw["y0"] * 40))
        if ckey in seen_c:
            continue
        seen_c.add(ckey)

        bx0, by0, bx1, by1 = dw["x0"], dw["y0"], dw["x1"], dw["y1"]
        if above and fluid:
            bx0 = min(bx0, above[0]["x0"])
            by0 = min(by0, above[0]["y0"])
            bx1 = max(bx1, above[0]["x1"])
        if below and label_parts:
            bx0 = min(bx0, below[0]["x0"])
            bx1 = max(bx1, below[-1]["x1"])
            by1 = max(by1, below[0]["y1"])

        continuations.append({
            "direction": direction,
            "fluid": fluid,
            "target_dwg": did,
            "target_sheet_id": "",
            "label": " ".join(label_parts)[:120],
            "x0": round(bx0, 5),
            "y0": round(by0, 5),
            "x1": round(bx1, 5),
            "y1": round(by1, 5),
            "source": dw.get("source", "pdf_text"),
        })

    return dwg_no, own_dwg_id, continuations


def _run_ocr_subprocess(pdf_path: Path, page_indices: list[int], master_max_w: int) -> dict[int, dict[str, Any]]:
    """DS920+ 워커용 격리 서브프로세스 OCR 실행 (완료 즉시 ONNX Runtime RAM 100% OS 반환)."""
    if not page_indices:
        return {}
    with tempfile.NamedTemporaryFile(prefix="dsv_ocr_", suffix=".json", delete=False) as tf:
        out_json = Path(tf.name)
    try:
        cmd = [
            sys.executable, "-m", "scripts.ocr_engine",
            "--pdf", str(pdf_path),
            "--pages", ",".join(str(i) for i in page_indices),
            "--max-w", str(master_max_w),
            "--out-json", str(out_json),
        ]
        subprocess.run(cmd, check=True, cwd=str(ROOT), timeout=max(300, len(page_indices) * 180))
        raw = json.loads(out_json.read_text(encoding="utf-8"))
        return {int(k): v for k, v in raw.items()}
    except Exception as exc:
        print(f"  [OCR 경고] 서브프로세스 실패({pdf_path.name}): {exc}", flush=True)
        return {}
    finally:
        out_json.unlink(missing_ok=True)


def process_single_pdf(job: dict[str, Any]) -> list[dict[str, Any]]:
    """PDF 1개(단일/다중 페이지)를 렌더링·타일링·텍스트/선택적 OCR·태그·연결화살표 인덱싱."""
    pdf_path = Path(job["pdf_path"])
    rel_str = job["rel_str"]
    category = job["category"]
    subcategory = job["subcategory"]
    sha256 = job["sha256"]
    out_dir = Path(job["out_dir"])
    extra_cats = job["extra_categories"]
    enable_ocr = bool(job.get("enable_ocr", False))
    ocr_subprocess = bool(job.get("ocr_subprocess", False))
    reparse_only = bool(job.get("reparse_only", False))
    existing_by_page: dict[int, dict[str, Any]] = {
        int(s["page_no"]): s for s in (job.get("existing_sheets") or [])
    }

    results: list[dict[str, Any]] = []
    is_pid = category == "P&ID"
    master_max_w = 3400 if is_pid else 2600

    doc = pdfium.PdfDocument(str(pdf_path))
    total_pages = len(doc)
    reader = PdfReader(str(pdf_path)) if (total_pages > 1 and not reparse_only) else None

    # 1차 패스: 디지털 텍스트 추출 + OCR 필요 페이지 선별 (DS920+ 격리 서브프로세스용)
    page_texts: list[tuple[str, list[dict[str, Any]], float, float]] = []
    shx_indices: list[int] = []
    for idx in range(total_pages):
        page = doc[idx]
        pw, ph = page.get_size()
        if pw <= 0 or ph <= 0:
            page_texts.append(("", [], 0.0, 0.0))
            page.close()
            continue
        ft, words = extract_page_tokens(page)
        page_texts.append((ft, words, pw, ph))
        if needs_ocr(ft):
            shx_indices.append(idx)
        page.close()

    ocr_available = enable_ocr and bool(shx_indices) and is_ocr_available()
    subproc_ocr: dict[int, dict[str, Any]] = {}
    if ocr_available and ocr_subprocess:
        subproc_ocr = _run_ocr_subprocess(pdf_path, shx_indices, master_max_w)

    try:
        for idx in range(total_pages):
            page_no = idx + 1
            full_text, words, pw, ph = page_texts[idx]
            if pw <= 0 or ph <= 0:
                continue

            prev_sheet = existing_by_page.get(page_no) if reparse_only else None
            need_page_ocr = idx in shx_indices
            ocr_status = ""
            pil_img: Image.Image | None = None

            # 렌더링이 필요한 경우: 신규 PDF 타일링 OR 인프로세스 OCR
            need_render = (prev_sheet is None) or (need_page_ocr and ocr_available and not ocr_subprocess)
            if need_render:
                page = doc[idx]
                scale = max(0.6, min(master_max_w / max(pw, ph), 3.2))
                bitmap = page.render(scale=scale, rotation=0)
                pil_img = bitmap.to_pil().convert("RGB")
                page.close()

            if need_page_ocr:
                if ocr_available:
                    if ocr_subprocess:
                        ocr_res = subproc_ocr.get(idx)
                        if ocr_res:
                            ocr_txt = ocr_res.get("text", "")
                            ocr_words = ocr_res.get("words", [])
                            if ocr_txt:
                                full_text = (full_text + "\n" + ocr_txt).strip()
                            words = words + ocr_words
                            ocr_status = "ocr_done"
                        else:
                            ocr_status = "shx_pending"
                    elif pil_img is not None:
                        ocr_txt, ocr_words = ocr_pil_image(pil_img)
                        if ocr_txt:
                            full_text = (full_text + "\n" + ocr_txt).strip()
                        words = words + ocr_words
                        ocr_status = "ocr_done"
                else:
                    ocr_status = "shx_pending"

            tags = extract_tags_from_words(words)
            drawing_no, drawing_id, continuations = extract_dwg_meta_and_continuations(
                words, full_text, pdf_path.stem
            )

            if prev_sheet is not None:
                sheet_id = prev_sheet["sheet_id"]
                w_px = prev_sheet["width_px"]
                h_px = prev_sheet["height_px"]
                tile_cols = prev_sheet["tile_cols"]
                tile_rows = prev_sheet["tile_rows"]
                tile_size = prev_sheet["tile_size"]
                has_pdf = prev_sheet["has_pdf"]
                has_preview = prev_sheet["has_preview"]
                if pil_img is not None:
                    pil_img.close()
            else:
                assert pil_img is not None
                sheet_id = safe_sheet_id(pdf_path.stem, page_no, total_pages, sha256[:8])
                w_px, h_px = pil_img.size

                # 1) 썸네일 저장 (DS920+ 인코딩 최적화: BILINEAR + WebP method=2)
                thumb_dir = out_dir / "thumbs"
                thumb_dir.mkdir(parents=True, exist_ok=True)
                thumb = pil_img.copy()
                thumb.thumbnail((THUMB_W, THUMB_W), Image.Resampling.BILINEAR)
                thumb.save(thumb_dir / f"{sheet_id}.webp", "WEBP", quality=76, method=2)

                # 2) 프리뷰 이미지 + 타일 피라미드 저장
                s_tile_dir = out_dir / "tiles" / sheet_id
                s_tile_dir.mkdir(parents=True, exist_ok=True)
                prev = pil_img.copy()
                prev.thumbnail((PREVIEW_W, PREVIEW_W), Image.Resampling.BILINEAR)
                prev.save(s_tile_dir / "preview.webp", "WEBP", quality=82, method=2)

                tile_cols, tile_rows = 0, 0
                if w_px > PREVIEW_W or h_px > PREVIEW_W:
                    tile_cols = math.ceil(w_px / TILE_SIZE)
                    tile_rows = math.ceil(h_px / TILE_SIZE)
                    for r in range(tile_rows):
                        for c in range(tile_cols):
                            x0 = c * TILE_SIZE
                            y0 = r * TILE_SIZE
                            crop = pil_img.crop((x0, y0, min(x0 + TILE_SIZE, w_px), min(y0 + TILE_SIZE, h_px)))
                            crop.save(s_tile_dir / f"{c}_{r}.webp", "WEBP", quality=82, method=2)
                tile_size = TILE_SIZE if tile_cols else 0
                pil_img.close()

                # 3) 단일 페이지 PDF 저장 (원본 다운로드용)
                pdf_out_dir = out_dir / "pdfs"
                pdf_out_dir.mkdir(parents=True, exist_ok=True)
                pdf_dest = pdf_out_dir / f"{sheet_id}.pdf"
                has_pdf = 0
                has_preview = 1
                try:
                    if total_pages == 1:
                        if pdf_path.stat().st_size <= 12 * 1024 * 1024:
                            shutil.copy2(pdf_path, pdf_dest)
                            has_pdf = 1
                    elif reader is not None and idx < len(reader.pages):
                        writer = PdfWriter()
                        writer.add_page(reader.pages[idx])
                        with open(pdf_dest, "wb") as fh:
                            writer.write(fh)
                        has_pdf = 1
                except Exception:
                    has_pdf = 0

            title = pdf_path.stem if total_pages == 1 else f"{pdf_path.stem} ({page_no:02d})"
            if drawing_no and total_pages > 1:
                title = f"{drawing_no} · {pdf_path.stem[:28]} (p.{page_no})"

            shx_note = "SHX 벡터 도면" if (is_pid and ocr_status == "shx_pending" and not tags) else ""
            merged_extra = ", ".join(x for x in (extra_cats, shx_note) if x)
            results.append({
                "sheet_id": sheet_id,
                "category": category,
                "subcategory": subcategory,
                "revision": extract_revision(rel_str),
                "drawing_no": drawing_no,
                "drawing_id": drawing_id,
                "title": title,
                "source_file": rel_str,
                "file_type": "pdf",
                "page_no": page_no,
                "total_pages": total_pages,
                "width_px": w_px,
                "height_px": h_px,
                "tile_cols": tile_cols,
                "tile_rows": tile_rows,
                "tile_size": tile_size,
                "sha256": sha256,
                "has_pdf": has_pdf,
                "has_preview": has_preview,
                "extra_categories": merged_extra,
                "ocr_status": ocr_status,
                "full_text": full_text[:20_000],
                "tags": tags,
                "continuations": continuations,
            })
    finally:
        doc.close()

    return results


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_index(input_dir: Path, out_dir: Path, workers: int, project_name: str = "도면 프로젝트",
                enable_ocr: bool = False) -> None:
    started = time.perf_counter()
    pdfs = sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    print(f"[1/4] 발견된 PDF 파일: {len(pdfs)}개")

    # SHA-256 중복 제거 (동일 원본 PDF가 여러 폴더에 중복 배포된 경우 1회만 렌더링)
    by_hash: dict[str, dict[str, Any]] = {}
    for p in pdfs:
        rel = p.relative_to(input_dir)
        cat, sub = classify_path(rel)
        digest = sha256_file(p)
        if digest not in by_hash:
            by_hash[digest] = {
                "pdf_path": str(p),
                "rel_str": str(rel),
                "category": cat,
                "subcategory": sub,
                "sha256": digest,
                "out_dir": str(out_dir),
                "extra_categories": [],
                "enable_ocr": enable_ocr,
                "ocr_subprocess": False,
            }
        else:
            entry = by_hash[digest]
            label = f"{cat}/{sub}"
            if label not in entry["extra_categories"]:
                entry["extra_categories"].append(label)

    jobs = []
    for entry in by_hash.values():
        entry["extra_categories"] = ", ".join(entry["extra_categories"])
        jobs.append(entry)
    print(f"[2/4] SHA-256 중복 제거 후 고유 PDF: {len(jobs)}개 (중복 {len(pdfs) - len(jobs)}개 절감)")

    for sub in ("thumbs", "tiles", "pdfs"):
        shutil.rmtree(out_dir / sub, ignore_errors=True)
    db_path = out_dir / "drawings.db"
    for suffix in ("", "-wal", "-shm"):
        (out_dir / f"drawings.db{suffix}").unlink(missing_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_sheets: list[dict[str, Any]] = []
    failed: list[str] = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_single_pdf, j): j["rel_str"] for j in jobs}
        for fut in as_completed(futs):
            rel = futs[fut]
            done += 1
            try:
                sheets = fut.result()
                all_sheets.extend(sheets)
                if done % 25 == 0 or done == len(jobs):
                    print(f"  진행: {done}/{len(jobs)} PDF 완료 (누적 시트 {len(all_sheets)}장, {time.perf_counter()-started:.1f}s)")
            except Exception as exc:
                failed.append(f"{rel}: {exc}")
                print(f"  [경고] 건너뜀: {rel} — {exc}")

    print(f"[3/4] SQLite FTS5 + Trigram 인덱스 구축 중 ({len(all_sheets)}장)...")
    built_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    meta = {
        "project": project_name,
        "built_at_kst": built_at,
        "source_pdfs": str(len(pdfs)),
        "unique_pdfs": str(len(jobs)),
        "failed_count": str(len(failed)),
        "parser_version": str(PARSER_VERSION),
    }
    total_tags, unique_tags = _write_drawings_db_atomic(out_dir, all_sheets, meta, run_vacuum=True)
    elapsed = time.perf_counter() - started
    db_mb = db_path.stat().st_size / (1024 * 1024)
    print(f"[4/4] 로컬 인덱싱 완료 ({elapsed:.1f}s) — 시트 {len(all_sheets)}장, 태그 {total_tags}건(고유 {unique_tags}), DB {db_mb:.2f}MB, 실패 {len(failed)}건")


def _load_existing_by_sha(db_path: Path, out_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """기존 drawings.db에서 sha256별 시트·태그·연결화살표 목록을 읽어 재사용 가능한 항목만 반환."""
    if not db_path.is_file():
        return {}, {}
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta").fetchall()}
        sheet_rows = [dict(r) for r in conn.execute("SELECT * FROM sheets").fetchall()]
        fts_texts: dict[str, str] = {}
        try:
            for fr in conn.execute("SELECT sheet_id, full_text FROM sheets_fts").fetchall():
                if fr["full_text"]:
                    fts_texts[fr["sheet_id"]] = fr["full_text"]
        except sqlite3.Error:
            pass

        tcols = {r[1] for r in conn.execute("PRAGMA table_info(tags)").fetchall()}
        has_canon = "canonical_tag" in tcols and "loop_key" in tcols
        tags_by_sheet: dict[str, list[dict[str, Any]]] = {}
        q_tags = (
            "SELECT sheet_id, tag, canonical_tag, loop_key, kind, func, unit, loop, x0, y0, x1, y1, confidence, source, raw_text FROM tags"
            if has_canon else
            "SELECT sheet_id, tag, kind, func, unit, loop, x0, y0, x1, y1, confidence, source, raw_text FROM tags"
        )
        for tr in conn.execute(q_tags).fetchall():
            d = dict(tr)
            if not is_valid_tag(d["tag"], d["kind"], d["func"], d["unit"], d["loop"]):
                continue
            d.setdefault("canonical_tag", canonicalize_tag(d["tag"]))
            d.setdefault("loop_key", _compute_loop_key(d["tag"], d["kind"], d["func"], d["unit"], d["loop"]))
            tags_by_sheet.setdefault(d.pop("sheet_id"), []).append(d)

        conts_by_sheet: dict[str, list[dict[str, Any]]] = {}
        try:
            for cr in conn.execute(
                "SELECT sheet_id, direction, fluid, target_dwg, target_sheet_id, label, x0, y0, x1, y1, source FROM continuations"
            ).fetchall():
                d = dict(cr)
                conts_by_sheet.setdefault(d.pop("sheet_id"), []).append(d)
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return {}, {}
    finally:
        conn.close()

    by_sha: dict[str, list[dict[str, Any]]] = {}
    for s in sheet_rows:
        s["tags"] = tags_by_sheet.get(s["sheet_id"], [])
        s["continuations"] = conts_by_sheet.get(s["sheet_id"], [])
        if not s.get("full_text"):
            s["full_text"] = fts_texts.get(s["sheet_id"], "")
        if not s.get("revision"):
            s["revision"] = extract_revision(s.get("source_file", ""))
        s.setdefault("drawing_id", "")
        s.setdefault("ocr_status", "")
        by_sha.setdefault(s["sha256"], []).append(s)

    valid: dict[str, list[dict[str, Any]]] = {}
    for sha, slist in by_sha.items():
        ok = True
        for s in slist:
            sid = s["sheet_id"]
            if not (out_dir / "thumbs" / f"{sid}.webp").is_file():
                ok = False
                break
            if not (out_dir / "tiles" / sid / "preview.webp").is_file():
                ok = False
                break
        if ok:
            slist.sort(key=lambda x: x["page_no"])
            valid[sha] = slist
    return valid, meta


def _resolve_continuation_targets(all_sheets: list[dict[str, Any]]) -> None:
    """전체 시트의 drawing_id · drawing_no 매핑으로 continuations[].target_sheet_id를 자동 연결."""
    by_rev_id: dict[tuple[str, str], str] = {}
    by_id: dict[str, str] = {}
    by_rev_sfx: dict[tuple[str, str], str] = {}
    by_sfx: dict[str, str] = {}

    for s in all_sheets:
        sid = s["sheet_id"]
        rev = s.get("revision") or extract_revision(s.get("source_file", ""))
        did = (s.get("drawing_id") or "").strip().upper()
        dno = (s.get("drawing_no") or "").strip().upper()
        if did:
            by_rev_id.setdefault((rev, did), sid)
            by_id.setdefault(did, sid)
            tail = did.split("_")[-1]
            if len(tail) >= 3:
                by_rev_sfx.setdefault((rev, tail), sid)
                by_sfx.setdefault(tail, sid)
        if dno:
            tail_no = dno.split("-")[-1].lstrip("U")
            if len(tail_no) >= 3:
                by_rev_sfx.setdefault((rev, tail_no), sid)
                by_sfx.setdefault(tail_no, sid)

    for s in all_sheets:
        sid = s["sheet_id"]
        rev = s.get("revision") or extract_revision(s.get("source_file", ""))
        for c in s.get("continuations", []):
            td = (c.get("target_dwg") or "").strip().upper()
            if not td:
                continue
            target = (
                by_rev_id.get((rev, td))
                or by_id.get(td)
            )
            if not target:
                tail = td.split("_")[-1].split("-")[-1]
                target = by_rev_sfx.get((rev, tail)) or by_sfx.get(tail) or ""
            c["target_sheet_id"] = target if target and target != sid else ""


def _write_drawings_db_atomic(
    out_dir: Path,
    all_sheets: list[dict[str, Any]],
    meta: dict[str, str],
    run_vacuum: bool = True,
) -> tuple[int, int]:
    """임시 DB(.drawings.db.staging)에 전체 시트·태그·연결화살표·FTS5·Trigram 기록 후 원자 교체."""
    staging = out_dir / ".drawings.db.staging"
    for suffix in ("", "-wal", "-shm"):
        (out_dir / f".drawings.db.staging{suffix}").unlink(missing_ok=True)
    init_drawings_db(staging)

    # sheet_id 중복 방지 및 continuation 타겟 사전 연결
    seen_ids: set[str] = set()
    for s in all_sheets:
        sid = s["sheet_id"]
        if sid in seen_ids:
            n = 2
            while f"{sid}_{n}" in seen_ids:
                n += 1
            sid = f"{sid}_{n}"
            s["sheet_id"] = sid
        seen_ids.add(sid)
    _resolve_continuation_targets(all_sheets)

    conn = sqlite3.connect(staging, timeout=30)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=OFF")
    has_trigram = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheets_trigram'"
    ).fetchone() is not None

    total_tags = 0
    total_conts = 0
    ocr_sheets = 0
    unique_tags_set: set[str] = set()

    for s in all_sheets:
        sid = s["sheet_id"]
        tags = [t for t in s.get("tags", []) if is_valid_tag(t["tag"], t["kind"], t.get("func"), t.get("unit"), t.get("loop"))]
        conts = s.get("continuations", [])
        total_tags += len(tags)
        total_conts += len(conts)
        if s.get("ocr_status") == "ocr_done":
            ocr_sheets += 1
        for t in tags:
            unique_tags_set.add(t.get("canonical_tag") or canonicalize_tag(t["tag"]))
        tags_joined = " ".join(sorted({t["tag"] for t in tags}))
        canon_joined = " ".join(sorted({t.get("canonical_tag") or canonicalize_tag(t["tag"]) for t in tags}))
        rev = s.get("revision") or extract_revision(s.get("source_file", ""))
        dwgid = s.get("drawing_id", "") or ""
        ocr_st = s.get("ocr_status", "") or ""

        # sheets 본 테이블은 full_text 중복 저장을 생략(sheets_fts/sheets_trigram에만 저장)
        conn.execute(
            """
            INSERT INTO sheets(
                sheet_id, category, subcategory, revision, drawing_no, drawing_id, title, source_file, file_type,
                page_no, total_pages, width_px, height_px, tile_cols, tile_rows, tile_size,
                sha256, has_pdf, has_preview, tag_count, extra_categories, ocr_status, full_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (
                sid, s["category"], s["subcategory"], rev, s.get("drawing_no"), dwgid, s["title"],
                s["source_file"], s["file_type"], s["page_no"], s["total_pages"],
                s["width_px"], s["height_px"], s["tile_cols"], s["tile_rows"], s["tile_size"],
                s["sha256"], s["has_pdf"], s["has_preview"], len(tags),
                s.get("extra_categories", ""), ocr_st,
            ),
        )
        if tags:
            conn.executemany(
                """
                INSERT INTO tags(
                    sheet_id, tag, canonical_tag, loop_key, kind, func, unit, loop,
                    x0, y0, x1, y1, confidence, source, raw_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        sid, t["tag"],
                        t.get("canonical_tag") or canonicalize_tag(t["tag"]),
                        t.get("loop_key") or _compute_loop_key(t["tag"], t["kind"], t.get("func"), t.get("unit"), t.get("loop")),
                        t["kind"], t.get("func"), t.get("unit"), t.get("loop"),
                        t["x0"], t["y0"], t["x1"], t["y1"], t["confidence"], t["source"], t.get("raw_text", ""),
                    )
                    for t in tags
                ],
            )
        if conts:
            conn.executemany(
                """
                INSERT INTO continuations(
                    sheet_id, direction, fluid, target_dwg, target_sheet_id, label, x0, y0, x1, y1, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        sid, c.get("direction", ""), c.get("fluid", ""), c["target_dwg"],
                        c.get("target_sheet_id", ""), c.get("label", ""),
                        c["x0"], c["y0"], c["x1"], c["y1"], c.get("source", "pdf_text"),
                    )
                    for c in conts
                ],
            )
        ft = s.get("full_text", "")
        conn.execute(
            "INSERT INTO sheets_fts(sheet_id, drawing_no, title, category, subcategory, tags_joined, full_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, s.get("drawing_no") or "", s["title"], s["category"], s["subcategory"],
             tags_joined, ft),
        )
        if has_trigram:
            blob = " ".join(x for x in (s.get("drawing_no") or "", dwgid, s["title"], tags_joined, canon_joined, ft) if x)
            conn.execute(
                "INSERT INTO sheets_trigram(sheet_id, search_blob) VALUES (?, ?)",
                (sid, blob[:24_000]),
            )

    meta["total_sheets"] = str(len(all_sheets))
    meta["total_tags"] = str(total_tags)
    meta["total_continuations"] = str(total_conts)
    meta["ocr_sheets"] = str(ocr_sheets)
    meta["parser_version"] = str(PARSER_VERSION)
    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", list(meta.items()))
    conn.execute("INSERT INTO sheets_fts(sheets_fts) VALUES('optimize')")
    if has_trigram:
        conn.execute("INSERT INTO sheets_trigram(sheets_trigram) VALUES('optimize')")
    conn.commit()
    if run_vacuum:
        conn.execute("VACUUM")
    conn.close()

    for suffix in ("-wal", "-shm"):
        (out_dir / f"drawings.db{suffix}").unlink(missing_ok=True)
    staging.replace(out_dir / "drawings.db")
    return total_tags, len(unique_tags_set)


def build_index_incremental(
    input_dir: Path,
    out_dir: Path,
    workers: int = 1,
    project_name: str = "도면 프로젝트",
    batch_commit: int = 20,
    sheet_sleep: float = 0.0,
    enable_ocr: bool = False,
    ocr_subprocess: bool = False,
    reparse_all: bool = False,
    max_delete_ratio: float = 0.30,
    min_delete_guard_pdfs: int = 10,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """SHA-256 비교 기반 증분 인덱싱·타일링·선택적 OCR (Mac 고속 백필 + DS920+ 무인 자동 동기화 공용).

    - 이미 인덱싱된 동일 SHA-256 PDF는 WebP 타일을 100% 재사용한다.
    - 파서 업그레이드(reparse_all) 또는 미처리 SHX 시트(ocr_status='shx_pending' & enable_ocr=True)는
      기존 타일/sheet_id를 유지한 채 텍스트·OCR·태그·연결화살표만 고속 재추출한다.
    """
    started = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "drawings.db"

    pdfs = sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    by_hash: dict[str, dict[str, Any]] = {}
    revisions_set: set[str] = set()
    for p in pdfs:
        rel = p.relative_to(input_dir)
        if len(rel.parts) >= 2:
            revisions_set.add(rel.parts[0])
        cat, sub = classify_path(rel)
        digest = sha256_file(p)
        if digest not in by_hash:
            by_hash[digest] = {
                "pdf_path": str(p),
                "rel_str": str(rel),
                "category": cat,
                "subcategory": sub,
                "sha256": digest,
                "out_dir": str(out_dir),
                "extra_categories": [],
                "enable_ocr": enable_ocr,
                "ocr_subprocess": ocr_subprocess,
                "reparse_only": False,
                "existing_sheets": None,
            }
        else:
            entry = by_hash[digest]
            label = f"{cat}/{sub}"
            if label not in entry["extra_categories"]:
                entry["extra_categories"].append(label)

    for entry in by_hash.values():
        entry["extra_categories"] = ", ".join(entry["extra_categories"])

    existing_by_sha, existing_meta = _load_existing_by_sha(db_path, out_dir)
    effective_project = project_name if project_name != "도면 프로젝트" else existing_meta.get("project", project_name)
    prev_parser_ver = int(existing_meta.get("parser_version", "0") or 0)
    ocr_ready = enable_ocr and is_ocr_available()

    reused_sheets: list[dict[str, Any]] = []
    new_jobs: list[dict[str, Any]] = []
    reparsed_count = 0
    meta_changed = False

    for sha, entry in by_hash.items():
        if sha in existing_by_sha:
            slist = existing_by_sha[sha]
            has_pending_ocr = ocr_ready and any(s.get("ocr_status") == "shx_pending" for s in slist)
            need_reparse = reparse_all or (prev_parser_ver < PARSER_VERSION) or has_pending_ocr
            if need_reparse:
                entry["reparse_only"] = True
                entry["existing_sheets"] = slist
                new_jobs.append(entry)
                reparsed_count += 1
                continue

            rev = extract_revision(entry["rel_str"])
            for s in slist:
                prev_shx = "SHX 벡터 도면" if "SHX 벡터 도면" in s.get("extra_categories", "") else ""
                want_extra = ", ".join(x for x in (entry["extra_categories"], prev_shx) if x)
                if (
                    s["category"] != entry["category"]
                    or s["subcategory"] != entry["subcategory"]
                    or s.get("revision", "") != rev
                    or s["source_file"] != entry["rel_str"]
                    or s.get("extra_categories", "") != want_extra
                ):
                    meta_changed = True
                    s["category"] = entry["category"]
                    s["subcategory"] = entry["subcategory"]
                    s["revision"] = rev
                    s["source_file"] = entry["rel_str"]
                    s["extra_categories"] = want_extra
                reused_sheets.append(s)
        else:
            new_jobs.append(entry)

    removed_shas = [sha for sha in existing_by_sha if sha not in by_hash]
    removed_sheets = [s for sha in removed_shas for s in existing_by_sha[sha]]

    # 대량 삭제 안전장치: 드라이브 마운트 오류·권한 회수 등으로 대량 파일이 갑자기 사라진 경우 파괴적 삭제 차단
    if (
        max_delete_ratio > 0
        and len(existing_by_sha) >= min_delete_guard_pdfs
        and (len(removed_shas) / len(existing_by_sha)) > max_delete_ratio
    ):
        pct = (len(removed_shas) / len(existing_by_sha)) * 100.0
        raise RuntimeError(
            f"대량 삭제 안전장치 작동: 기존 {len(existing_by_sha)}개 PDF 중 {len(removed_shas)}개({pct:.1f}%) 삭제 감지 "
            f"(허용 상한 {max_delete_ratio * 100:.0f}%). 구글 드라이브 폴더·권한 상태를 확인하세요."
        )

    if not new_jobs and not removed_shas and not meta_changed and db_path.is_file():
        elapsed = round(time.perf_counter() - started, 2)
        return {
            "status": "no_changes",
            "at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            "source_pdfs": len(pdfs),
            "unique_pdfs": len(by_hash),
            "added_pdfs": 0,
            "removed_pdfs": 0,
            "added_sheets": 0,
            "removed_sheets": 0,
            "reparsed_pdfs": 0,
            "failed_count": 0,
            "total_sheets": len(reused_sheets),
            "total_tags": sum(len(s.get("tags", [])) for s in reused_sheets),
            "unique_tags": len({t.get("canonical_tag") or canonicalize_tag(t["tag"]) for s in reused_sheets for t in s.get("tags", [])}),
            "elapsed_sec": elapsed,
            "added_files": [],
            "removed_files": [],
            "revisions": sorted(revisions_set),
        }

    added_sheets: list[dict[str, Any]] = []
    added_files: list[str] = []
    failed: list[str] = []

    def _emit_progress(done_n: int) -> None:
        if progress_cb:
            progress_cb({
                "processed_pdfs": done_n,
                "pending_pdfs": len(new_jobs),
                "added_sheets_so_far": len(added_sheets),
                "failed_so_far": len(failed),
            })

    def _checkpoint(final: bool = False) -> tuple[int, int]:
        meta = {
            "project": effective_project,
            "built_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            "source_pdfs": str(len(pdfs)),
            "unique_pdfs": str(len(by_hash)),
            "failed_count": str(len(failed)),
        }
        return _write_drawings_db_atomic(out_dir, reused_sheets + added_sheets, meta, run_vacuum=final)

    _emit_progress(0)
    if workers <= 1:
        for idx, job in enumerate(new_jobs, 1):
            try:
                sheets = process_single_pdf(job)
                added_sheets.extend(sheets)
                if not job.get("reparse_only"):
                    added_files.append(job["rel_str"])
            except Exception as exc:
                failed.append(f"{job['rel_str']}: {exc}")
                print(f"  [경고] 증분 건너뜀: {job['rel_str']} — {exc}", flush=True)
            gc.collect()
            _emit_progress(idx)
            if batch_commit > 0 and idx % batch_commit == 0 and idx < len(new_jobs):
                _checkpoint(final=False)
            if sheet_sleep > 0:
                time.sleep(sheet_sleep)
    else:
        done = 0
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(process_single_pdf, j): j for j in new_jobs}
            for fut in as_completed(futs):
                job = futs[fut]
                done += 1
                try:
                    sheets = fut.result()
                    added_sheets.extend(sheets)
                    if not job.get("reparse_only"):
                        added_files.append(job["rel_str"])
                except Exception as exc:
                    failed.append(f"{job['rel_str']}: {exc}")
                    print(f"  [경고] 증분 건너뜀: {job['rel_str']} — {exc}", flush=True)
                _emit_progress(done)

    total_tags, unique_tags = _checkpoint(final=True)

    # 최종 원자 교체 완료 후 삭제된 시트의 고아 파일 안전 정리
    active_ids = {s["sheet_id"] for s in (reused_sheets + added_sheets)}
    removed_files = sorted({s["source_file"] for s in removed_sheets})
    for rs in removed_sheets:
        sid = rs["sheet_id"]
        if sid in active_ids:
            continue
        (out_dir / "thumbs" / f"{sid}.webp").unlink(missing_ok=True)
        shutil.rmtree(out_dir / "tiles" / sid, ignore_errors=True)
        (out_dir / "pdfs" / f"{sid}.pdf").unlink(missing_ok=True)

    elapsed = round(time.perf_counter() - started, 2)
    status = "updated" if (added_files or removed_shas or meta_changed or reparsed_count > 0) else "no_changes"
    return {
        "status": status,
        "at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "source_pdfs": len(pdfs),
        "unique_pdfs": len(by_hash),
        "added_pdfs": len(added_files),
        "removed_pdfs": len(removed_shas),
        "reparsed_pdfs": reparsed_count,
        "added_sheets": len(added_sheets),
        "removed_sheets": len(removed_sheets),
        "failed_count": len(failed),
        "failed_samples": failed[:10],
        "total_sheets": len(reused_sheets) + len(added_sheets),
        "total_tags": total_tags,
        "unique_tags": unique_tags,
        "elapsed_sec": elapsed,
        "added_files": added_files[:30],
        "removed_files": removed_files[:30],
        "revisions": sorted(revisions_set),
    }


def _upload_tar_bytes(url: str, token: str, payload: bytes, name: str) -> dict[str, Any]:
    boundary = "----DrawingSyncBoundary98765"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="bundle"; filename="{name}"\r\n'
        f"Content-Type: application/gzip\r\n\r\n"
    ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")

    endpoint = url.rstrip("/") + "/api/admin/sync"
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "X-Sync-Token": token,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sync_to_server(data_dir: Path, url: str, token: str, chunk_mb: int = 18,
                   include_pdfs: bool = True, db_only: bool = False) -> None:
    """data/ 내 thumbs, tiles, (선택) pdfs, drawings.db를 청크 tar.gz로 분할해 DS920+에 안전 업로드."""
    max_bytes = chunk_mb * 1024 * 1024
    files: list[Path] = []
    if not db_only:
        for folder in ("thumbs", "tiles", *(["pdfs"] if include_pdfs else [])):
            base = data_dir / folder
            if base.is_dir():
                files.extend(sorted(p for p in base.rglob("*") if p.is_file()))

    total_files = len(files) + 1
    print(f"[Sync] 대상 파일 {total_files}개 → {url} (청크 {chunk_mb}MB)")

    batch: list[Path] = []
    batch_sz = 0
    chunk_no = 0
    uploaded = 0

    def send_batch(paths: list[Path], label: str):
        nonlocal chunk_no, uploaded
        if not paths:
            return
        chunk_no += 1
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=3) as tf:
            for p in paths:
                tf.add(p, arcname=str(p.relative_to(data_dir)))
        raw = buf.getvalue()
        res = _upload_tar_bytes(url, token, raw, f"chunk_{chunk_no:03d}_{label}.tar.gz")
        uploaded += res.get("extracted_files", 0)
        print(f"  청크 #{chunk_no} ({label}): {len(paths)}개 파일, {len(raw)/(1024*1024):.1f}MB → OK (누적 {uploaded}/{total_files})")

    for p in files:
        sz = p.stat().st_size
        if batch and batch_sz + sz > max_bytes:
            send_batch(batch, "assets")
            batch, batch_sz = [], 0
        batch.append(p)
        batch_sz += sz
    send_batch(batch, "assets")

    db_file = data_dir / "drawings.db"
    if db_file.is_file():
        send_batch([db_file], "db")
    print(f"[Sync] 전체 동기화 완료 ({uploaded}개 파일 반영)")


def main() -> int:
    ap = argparse.ArgumentParser(description="도면 로컬 인덱싱·타일링·선택적 OCR 및 초경량 뷰어 서버 동기화")
    ap.add_argument("--input", default=str(ROOT / "raw_drawings"))
    ap.add_argument("--out", default=str(ROOT / "data"))
    ap.add_argument("--project", default="도면 프로젝트", help="인덱스 메타데이터 프로젝트명")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    ap.add_argument("--incremental", action="store_true", help="기존 DB와 SHA-256 비교하여 변경분만 증분 인덱싱")
    ap.add_argument("--ocr", action="store_true", help="SHX 벡터/스캔 도면에 선택적 RapidOCR 실행")
    ap.add_argument("--reparse", action="store_true", help="기존 WebP 타일/sheet_id를 유지한 채 텍스트·OCR·태그만 고속 재파싱")
    ap.add_argument("--sync-only", action="store_true")
    ap.add_argument("--db-only", action="store_true", help="drawings.db만 서버에 원자 교체 업로드")
    ap.add_argument("--url", help="동기화 대상 서버 URL (예: https://drawing.example.com)")
    ap.add_argument("--token", default=os.environ.get("SYNC_TOKEN", ""))
    ap.add_argument("--no-pdfs", action="store_true", help="원본 PDF 제외하고 WebP 타일/DB만 동기화")
    args = ap.parse_args()

    out_dir = Path(args.out)
    if not args.sync_only:
        if args.incremental or args.reparse:
            summary = build_index_incremental(
                Path(args.input), out_dir, workers=args.workers,
                project_name=args.project, enable_ocr=args.ocr, reparse_all=args.reparse,
            )
            print(json.dumps(summary, ensure_ascii=False))
        else:
            build_index(Path(args.input), out_dir, args.workers, project_name=args.project, enable_ocr=args.ocr)
    if args.url:
        if not args.token:
            print("오류: --token 또는 SYNC_TOKEN 환경변수가 필요합니다", file=sys.stderr)
            return 2
        sync_to_server(out_dir, args.url, args.token, include_pdfs=not args.no_pdfs, db_only=args.db_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
