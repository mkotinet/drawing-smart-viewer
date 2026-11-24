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
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.db import init_drawings_db  # noqa: E402

KST = timezone(timedelta(hours=9), "KST")

# ISA-5.1 + 공조/유틸리티 계장 관용 기능문자 허용 목록
INSTRUMENT_FUNCS = frozenset("""
FE FT FIT FI FIC FIR FIRC FR FRC FC FCV FV FY FQ FQI FQIC FQT FS FSH FSL FSHH FSLL FAH FAL FG FO FX FZ
PE PT PIT PI PIC PIR PR PRC PC PCV PV PY PS PSH PSL PSHH PSLL PAH PAL PAHH PALL PG PSV PSE PRV PDT PDIT PDI PDIC PDS PDSH PDSL
LE LT LIT LI LIC LIR LR LRC LC LCV LV LY LS LSH LSL LSHH LSLL LAH LAL LAHH LALL LG LO LX LZ
TE TW TT TIT TI TIC TIR TR TRC TC TCV TV TY TS TSH TSL TSHH TSLL TAH TAL TAHH TALL TG TX TDT TDI TDIC
AE AT AIT AI AIC AIR AR ARC AC AV AY AS ASH ASL AAH AAL AX
BE BT BS EE ET EI ES IE IT II IIC IS JT JI JIC JS KS KI KT KC KY
HS HV HC HIC HCV HY HZ QE QT QI QIC QS QY RE RT RI RS
SE ST SIT SI SIC SC SS SSH SSL SAH SAL SY SV UA UY UX
VE VT VIT VI VS VSH VAH VY VX WE WT WIT WI WIC WS WSH WAL WY WQ YS YI YC
ZE ZT ZI ZIC ZS ZSO ZSC ZSH ZSL ZY ZV ZC ZX ZLO ZLC XV XY XS XI XC XL XZ XA
SDV BDV ESDV MOV ROV TOT OCR TCVI MVD MVI ZAHU ZEF
""".split())

RE_PLANT_TAG = re.compile(r"\b(?P<func>[A-Z]{2,5})-(?P<unit>[A-Z0-9]{2,12})-(?P<loop>\d{2,5}[A-Z]?)\b")
RE_STD_INST = re.compile(r"\b(?:(?P<unit>[A-Z0-9]{1,4})-)?(?P<func>[A-Z]{2,5})-(?P<loop>\d{2,5}[A-Z]?)\b")
RE_LINE_NO = re.compile(r'\b\d{1,3}(?:"|A)-[A-Z]{2,5}-\d{3,5}(?:-[A-Z0-9]{2,6})?\b')
RE_DWG_NO = re.compile(r"\b[A-Z]{2}\d{4}-[A-Z0-9]{4}-[A-Z0-9]{3,4}-[A-Z0-9]{4}-[A-Z0-9]{4,5}\b")
RE_EQUIP = re.compile(r"\b(?:AHU|FCU|EF|SF|CHW|CT|HX|TK|PU|P|E|V|C|B|T|MCP|PLC|DDC|MCC|PNL|JB)-\d{2,4}[A-Z]?\b")

TILE_SIZE = 1024
THUMB_W = 480
PREVIEW_W = 1600


def classify_path(rel_path: Path) -> tuple[str, str]:
    parts = rel_path.parts
    top = parts[0] if parts else "기타"
    top_upper = top.upper()
    name = rel_path.stem.upper()

    if "P&ID" in top_upper or "P&ID" in name or "PID" in top_upper:
        if "UT" in name:
            return ("P&ID", "UT공정 P&ID")
        if len(parts) > 2:
            return ("P&ID", parts[1])
        return ("P&ID", "공정 P&ID")

    if "SHOP" in top_upper:
        sub = parts[1] if len(parts) > 2 else "SHOP 기타"
        return ("SHOP 시공도", sub)

    if "전력" in top or "POWER" in top_upper:
        if "DATASHEET" in name:
            return ("전력제어", "데이터시트")
        if "-SYS-" in name:
            return ("전력제어", "계통도(SYS)")
        if "-WRP-" in name:
            return ("전력제어", "배선도(WRP)")
        if "-EQP-" in name or "-MCW-" in name:
            return ("전력제어", "배치·주전원")
        return ("전력제어", "범례·기타")

    if "제어" in top or "CONTROL" in top_upper or "AFC" in top_upper:
        if "-HCD-" in name:
            return ("제어 도면", "계기 결선도(HCD)")
        if "-WRP-" in name:
            return ("제어 도면", "배선 평면도(WRP)")
        if "-EQP-" in name:
            return ("제어 도면", "장비 배치도(EQP)")
        if "-APP-" in name:
            return ("제어 도면", "부속 도면(APP)")
        if "-MCW-" in name:
            return ("제어 도면", "주전원 결선(MCW)")
        return ("제어 도면", "단위 설비 결선")

    return ("기타 도면", top)


def safe_sheet_id(stem: str, page_no: int, total_pages: int, sha8: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not clean:
        clean = f"doc_{sha8}"
    if len(clean) > 64:
        clean = clean[:55] + "_" + sha8
    if total_pages > 1:
        return f"{clean}_p{page_no:03d}"
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

        # 연속된 비공백 문자 묶음을 단어로 결합 + bbox 계산
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

        # 문자수가 극단적으로 큰 경우 상한 보호
        limit = min(n_chars, 60_000)
        for idx in range(limit):
            ch = tp.get_text_range(idx, 1)
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
    """단어 목록에서 계기(단일줄 + 버블 2~3줄 근접 클러스터)·라인번호·기기 태그를 추출."""
    found: list[dict[str, Any]] = []
    seen_pos: set[tuple[str, int, int]] = set()

    def add(tag: str, kind: str, func: str | None, unit: str | None, loop: str | None,
            x0: float, y0: float, x1: float, y1: float, conf: float, raw: str):
        key = (tag, round(x0 * 100), round(y0 * 100))
        if key in seen_pos:
            return
        seen_pos.add(key)
        found.append({
            "tag": tag, "kind": kind, "func": func, "unit": unit, "loop": loop,
            "x0": round(x0, 5), "y0": round(y0, 5), "x1": round(x1, 5), "y1": round(y1, 5),
            "confidence": conf, "source": "pdf_text", "raw_text": raw[:120],
        })

    # 1) 단일 토큰 내 표준 태그 · 라인번호 · 기기 태그
    func_tokens: list[dict[str, Any]] = []
    for w in words:
        t = w["text"].upper().replace("–", "-").replace("—", "-")
        matched = False
        if m := RE_PLANT_TAG.search(t):
            fn = m.group("func")
            if fn in INSTRUMENT_FUNCS:
                add(m.group(0), "instrument", fn, m.group("unit"), m.group("loop"),
                    w["x0"], w["y0"], w["x1"], w["y1"], 0.98, w["text"])
                matched = True
        if not matched and (m := RE_STD_INST.search(t)):
            fn = m.group("func")
            if fn in INSTRUMENT_FUNCS:
                add(m.group(0), "instrument", fn, m.group("unit"), m.group("loop"),
                    w["x0"], w["y0"], w["x1"], w["y1"], 0.95, w["text"])
                matched = True
        if not matched and (m := RE_LINE_NO.search(t)):
            add(m.group(0), "line_no", None, None, None,
                w["x0"], w["y0"], w["x1"], w["y1"], 0.92, w["text"])
            matched = True
        if not matched and (m := RE_EQUIP.search(t)):
            add(m.group(0), "equipment", None, None, None,
                w["x0"], w["y0"], w["x1"], w["y1"], 0.90, w["text"])
            matched = True
        if not matched and t in INSTRUMENT_FUNCS:
            func_tokens.append(w)

    # 2) 버블 2줄(FUNC / LOOP) 및 3줄(FUNC / UNIT / LOOP) 수직 근접 결합
    for fw in func_tokens:
        fx = (fw["x0"] + fw["x1"]) / 2
        fy = (fw["y0"] + fw["y1"]) / 2
        fh = max(fw["y1"] - fw["y0"], 0.006)
        below = [
            w for w in words
            if w is not fw
            and abs(((w["x0"] + w["x1"]) / 2) - fx) <= max(fh * 3.2, 0.024)
            and 0 < ((w["y0"] + w["y1"]) / 2) - fy <= max(fh * 4.5, 0.036)
        ]
        below.sort(key=lambda w: w["y0"])
        if len(below) >= 2:
            u_txt = below[0]["text"].upper()
            l_txt = below[1]["text"].upper()
            if re.fullmatch(r"[A-Z0-9]{2,12}", u_txt) and re.fullmatch(r"\d{2,5}[A-Z]?", l_txt):
                fn = fw["text"].upper()
                tag = f"{fn}-{u_txt}-{l_txt}"
                bx0 = min(fw["x0"], below[0]["x0"], below[1]["x0"])
                by0 = min(fw["y0"], below[0]["y0"], below[1]["y0"])
                bx1 = max(fw["x1"], below[0]["x1"], below[1]["x1"])
                by1 = max(fw["y1"], below[0]["y1"], below[1]["y1"])
                add(tag, "instrument", fn, u_txt, l_txt, bx0, by0, bx1, by1, 0.90,
                    f"{fn} / {u_txt} / {l_txt}")
                continue
        if len(below) >= 1:
            l_txt = below[0]["text"].upper()
            if re.fullmatch(r"\d{2,5}[A-Z]?", l_txt):
                fn = fw["text"].upper()
                tag = f"{fn}-{l_txt}"
                bx0 = min(fw["x0"], below[0]["x0"])
                by0 = min(fw["y0"], below[0]["y0"])
                bx1 = max(fw["x1"], below[0]["x1"])
                by1 = max(fw["y1"], below[0]["y1"])
                add(tag, "instrument", fn, None, l_txt, bx0, by0, bx1, by1, 0.88,
                    f"{fn} / {l_txt}")

    return found


def process_single_pdf(job: dict[str, Any]) -> list[dict[str, Any]]:
    """PDF 1개(단일/다중 페이지)를 워커 프로세스에서 렌더링·타일링·텍스트 인덱싱."""
    pdf_path = Path(job["pdf_path"])
    rel_str = job["rel_str"]
    category = job["category"]
    subcategory = job["subcategory"]
    sha256 = job["sha256"]
    out_dir = Path(job["out_dir"])
    extra_cats = job["extra_categories"]

    results: list[dict[str, Any]] = []
    is_pid = category == "P&ID"
    master_max_w = 3400 if is_pid else 2600

    doc = pdfium.PdfDocument(str(pdf_path))
    total_pages = len(doc)
    reader = PdfReader(str(pdf_path)) if total_pages > 1 else None

    try:
        for idx in range(total_pages):
            page_no = idx + 1
            page = doc[idx]
            pw, ph = page.get_size()
            if pw <= 0 or ph <= 0:
                page.close()
                continue

            full_text, words = extract_page_tokens(page)
            tags = extract_tags_from_words(words)

            # 도면번호 추출 (파일명 → 본문 순)
            dwg_m = RE_DWG_NO.search(pdf_path.stem.upper()) or RE_DWG_NO.search(full_text.upper())
            drawing_no = dwg_m.group(0) if dwg_m else None

            sheet_id = safe_sheet_id(pdf_path.stem, page_no, total_pages, sha256[:8])
            title = pdf_path.stem if total_pages == 1 else f"{pdf_path.stem} ({page_no:02d})"
            if drawing_no and total_pages > 1:
                title = f"{drawing_no} · {pdf_path.stem[:28]} (p.{page_no})"

            # 고해상도 마스터 렌더링 (A0 메모리 상한 보호)
            scale = min(master_max_w / max(pw, ph), 3.2)
            scale = max(scale, 0.6)
            bitmap = page.render(scale=scale, rotation=0)
            pil_img: Image.Image = bitmap.to_pil().convert("RGB")
            w_px, h_px = pil_img.size

            # 1) 썸네일 저장
            thumb_dir = out_dir / "thumbs"
            thumb_dir.mkdir(parents=True, exist_ok=True)
            thumb = pil_img.copy()
            thumb.thumbnail((THUMB_W, THUMB_W), Image.Resampling.LANCZOS)
            thumb.save(thumb_dir / f"{sheet_id}.webp", "WEBP", quality=76, method=4)

            # 2) 프리뷰 이미지 + 타일 피라미드 저장
            s_tile_dir = out_dir / "tiles" / sheet_id
            s_tile_dir.mkdir(parents=True, exist_ok=True)
            prev = pil_img.copy()
            prev.thumbnail((PREVIEW_W, PREVIEW_W), Image.Resampling.LANCZOS)
            prev.save(s_tile_dir / "preview.webp", "WEBP", quality=82, method=4)

            tile_cols, tile_rows = 0, 0
            if w_px > PREVIEW_W or h_px > PREVIEW_W:
                tile_cols = math.ceil(w_px / TILE_SIZE)
                tile_rows = math.ceil(h_px / TILE_SIZE)
                for r in range(tile_rows):
                    for c in range(tile_cols):
                        x0 = c * TILE_SIZE
                        y0 = r * TILE_SIZE
                        crop = pil_img.crop((x0, y0, min(x0 + TILE_SIZE, w_px), min(y0 + TILE_SIZE, h_px)))
                        crop.save(s_tile_dir / f"{c}_{r}.webp", "WEBP", quality=82, method=4)

            pil_img.close()
            page.close()

            # 3) 단일 페이지 PDF 저장 (원본 다운로드용)
            pdf_out_dir = out_dir / "pdfs"
            pdf_out_dir.mkdir(parents=True, exist_ok=True)
            pdf_dest = pdf_out_dir / f"{sheet_id}.pdf"
            has_pdf = 0
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

            results.append({
                "sheet_id": sheet_id,
                "category": category,
                "subcategory": subcategory,
                "drawing_no": drawing_no,
                "title": title,
                "source_file": rel_str,
                "file_type": "pdf",
                "page_no": page_no,
                "total_pages": total_pages,
                "width_px": w_px,
                "height_px": h_px,
                "tile_cols": tile_cols,
                "tile_rows": tile_rows,
                "tile_size": TILE_SIZE if tile_cols else 0,
                "sha256": sha256,
                "has_pdf": has_pdf,
                "has_preview": 1,
                "extra_categories": extra_cats,
                "full_text": full_text[:20_000],
                "tags": tags,
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


def build_index(input_dir: Path, out_dir: Path, workers: int, project_name: str = "도면 프로젝트") -> None:
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
            }
        else:
            # 우선순위가 더 명확한 카테고리(예: SHOP 세부 분류) 기록
            entry = by_hash[digest]
            label = f"{cat}/{sub}"
            if label not in entry["extra_categories"]:
                entry["extra_categories"].append(label)

    jobs = []
    for entry in by_hash.values():
        entry["extra_categories"] = ", ".join(entry["extra_categories"])
        jobs.append(entry)
    print(f"[2/4] SHA-256 중복 제거 후 고유 PDF: {len(jobs)}개 (중복 {len(pdfs) - len(jobs)}개 절감)")

    # 기존 출력 초기화
    for sub in ("thumbs", "tiles", "pdfs"):
        shutil.rmtree(out_dir / sub, ignore_errors=True)
    db_path = out_dir / "drawings.db"
    for suffix in ("", "-wal", "-shm"):
        (out_dir / f"drawings.db{suffix}").unlink(missing_ok=True)
    init_drawings_db(db_path)

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

    # 3) SQLite FTS5 적재
    print(f"[3/4] SQLite FTS5 인덱스 구축 중 ({len(all_sheets)}장)...")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=OFF")
    seen_ids: set[str] = set()
    total_tags = 0

    for s in all_sheets:
        sid = s["sheet_id"]
        if sid in seen_ids:
            n = 2
            while f"{sid}_{n}" in seen_ids:
                n += 1
            sid = f"{sid}_{n}"
            s["sheet_id"] = sid
        seen_ids.add(sid)

        tags = s["tags"]
        total_tags += len(tags)
        tags_joined = " ".join(sorted({t["tag"] for t in tags}))

        conn.execute(
            """
            INSERT INTO sheets(
                sheet_id, category, subcategory, drawing_no, title, source_file, file_type,
                page_no, total_pages, width_px, height_px, tile_cols, tile_rows, tile_size,
                sha256, has_pdf, has_preview, tag_count, extra_categories, full_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid, s["category"], s["subcategory"], s["drawing_no"], s["title"],
                s["source_file"], s["file_type"], s["page_no"], s["total_pages"],
                s["width_px"], s["height_px"], s["tile_cols"], s["tile_rows"], s["tile_size"],
                s["sha256"], s["has_pdf"], s["has_preview"], len(tags),
                s["extra_categories"], s["full_text"],
            ),
        )
        conn.executemany(
            """
            INSERT INTO tags(
                sheet_id, tag, kind, func, unit, loop, x0, y0, x1, y1, confidence, source, raw_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (sid, t["tag"], t["kind"], t["func"], t["unit"], t["loop"],
                 t["x0"], t["y0"], t["x1"], t["y1"], t["confidence"], t["source"], t["raw_text"])
                for t in tags
            ],
        )
        conn.execute(
            "INSERT INTO sheets_fts(sheet_id, drawing_no, title, category, subcategory, tags_joined, full_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, s["drawing_no"] or "", s["title"], s["category"], s["subcategory"],
             tags_joined, s["full_text"]),
        )

    built_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    meta = {
        "project": project_name,
        "built_at_kst": built_at,
        "source_pdfs": str(len(pdfs)),
        "unique_pdfs": str(len(jobs)),
        "total_sheets": str(len(all_sheets)),
        "total_tags": str(total_tags),
        "failed_count": str(len(failed)),
    }
    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", list(meta.items()))
    conn.execute("INSERT INTO sheets_fts(sheets_fts) VALUES('optimize')")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    elapsed = time.perf_counter() - started
    db_mb = db_path.stat().st_size / (1024 * 1024)
    print(f"[4/4] 로컬 인덱싱·타일링 완료 ({elapsed:.1f}s) — 시트 {len(all_sheets)}장, 태그 {total_tags}건, DB {db_mb:.2f}MB, 실패 {len(failed)}건")


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
                   include_pdfs: bool = True) -> None:
    """data/ 내 thumbs, tiles, (선택) pdfs, drawings.db를 청크 tar.gz로 분할해 DS920+에 안전 업로드."""
    max_bytes = chunk_mb * 1024 * 1024
    files: list[Path] = []
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

    # 마지막에 drawings.db를 원자적 교체로 업로드
    db_file = data_dir / "drawings.db"
    if db_file.is_file():
        send_batch([db_file], "db")
    print(f"[Sync] 전체 동기화 완료 ({uploaded}개 파일 반영)")


def main() -> int:
    ap = argparse.ArgumentParser(description="도면 로컬 인덱싱·타일링 및 초경량 뷰어 서버 동기화")
    ap.add_argument("--input", default=str(ROOT / "raw_drawings"))
    ap.add_argument("--out", default=str(ROOT / "data"))
    ap.add_argument("--project", default="도면 프로젝트", help="인덱스 메타데이터 프로젝트명")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    ap.add_argument("--sync-only", action="store_true")
    ap.add_argument("--url", help="동기화 대상 서버 URL (예: https://drawing.example.com)")
    ap.add_argument("--token", default=os.environ.get("SYNC_TOKEN", ""))
    ap.add_argument("--no-pdfs", action="store_true", help="원본 PDF 제외하고 WebP 타일/DB만 동기화")
    args = ap.parse_args()

    out_dir = Path(args.out)
    if not args.sync_only:
        build_index(Path(args.input), out_dir, args.workers, project_name=args.project)
    if args.url:
        if not args.token:
            print("오류: --token 또는 SYNC_TOKEN 환경변수가 필요합니다", file=sys.stderr)
            return 2
        sync_to_server(out_dir, args.url, args.token, include_pdfs=not args.no_pdfs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
