#!/usr/bin/env python3
"""선택적 하이브리드 OCR 엔진 (SHX 벡터 · 스캔 도면 전용, RapidOCR PP-OCRv5 ONNX).

- 디지털 텍스트가 정상 존재하는 83% 도면은 즉시 건너뛰고(needs_ocr=False),
  표제란 상용구/'M' 더미만 남은 SHX/스캔 도면(17%)에만 오버랩 슬라이딩 OCR을 수행한다.
- DS920+ 워커 메모리 보호를 위해 서브프로세스 단발 실행 CLI(--pdf --pages)를 함께 제공한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from PIL import Image

# 표제란 공통 영어 라벨·고정 상용구 (본문 텍스트 유무 판정 시 제외)
_TITLEBLOCK_BOILERPLATE = frozenset({
    "PROJECT", "TITLE", "CODE", "DRAWING", "MANAGER", "PAPER", "SIZE", "BUILDING",
    "ZONE", "SYSTEM", "REV", "NO", "APPROVED", "BY", "DATE", "SCALE", "FLOOR", "ROOM",
    "SUBSYSTEM", "OWNER", "CONTRACTOR", "CONS", "DESCRIPTION", "DRN", "CHK", "APP", "PM",
    "SEC", "NRD", "K2", "L2392", "26", "0431", "ABCDEF", "FEDCBA", "12345678",
    "NONE", "SHEET", "OF", "DWG", "FILE", "NAME", "LOCATION", "CUSTOMER", "JOB",
    "PREPARED", "MADE", "CHECKED", "PIPING", "INSTRUMENT", "DIAGRAM", "TP",
})

_SPLIT_RE = re.compile(r"[\s:/().,-]+")
_ENGINE = None


def needs_ocr(full_text: str) -> bool:
    """PDF 텍스트 레이어에 표제란 상용구·'M' 더미 외 실제 도면 본문 글자가 부족한지 판별."""
    if not full_text or len(full_text.strip()) < 60:
        return True
    raw_tokens = [w for w in _SPLIT_RE.split(full_text.upper()) if w]
    if not raw_tokens:
        return True
    m_dummy = sum(1 for w in raw_tokens if set(w) <= {"M", "*"})
    if m_dummy >= 10 and (m_dummy / len(raw_tokens)) >= 0.25:
        return True
    meaningful = [
        w for w in raw_tokens
        if len(w) >= 2 and not (set(w) <= {"M", "*"}) and w not in _TITLEBLOCK_BOILERPLATE
    ]
    return len(meaningful) < 8


def is_ocr_available() -> bool:
    try:
        import rapidocr  # noqa: F401
        return True
    except Exception:
        return False


def get_ocr_engine():
    """프로세스당 1회 RapidOCR 한국어+영문 PP-OCRv5 경량 ONNX 엔진 초기화."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    threads = max(1, min(int(os.environ.get("OCR_THREADS", "2")), 4))
    _ENGINE = RapidOCR(params={
        "Rec.lang_type": LangRec.KOREAN,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "Det.ocr_version": OCRVersion.PPOCRV4,
        "Det.model_type": ModelType.MOBILE,
        "Det.limit_side_len": 1280,
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })
    return _ENGINE


def warmup_models() -> None:
    """Docker 빌드 시 모델 ONNX 파일을 미리 내려받아 런타임 오프라인 실행 보장."""
    eng = get_ocr_engine()
    blank = Image.new("RGB", (64, 64), (255, 255, 255))
    eng(blank)


def _window_has_ink(crop: Image.Image) -> bool:
    """빈 여백 창을 1ms 미만으로 건너뛰기 위한 저해상도 잉크 픽셀 비율 검사."""
    small = crop.convert("L").resize((96, 96), Image.Resampling.NEAREST)
    dark = sum(1 for px in small.getdata() if px < 225)
    return (dark / (96 * 96)) >= 0.0012


def _iou(a: dict[str, Any], b: dict[str, Any]) -> float:
    ix0 = max(a["x0"], b["x0"])
    iy0 = max(a["y0"], b["y0"])
    ix1 = min(a["x1"], b["x1"])
    iy1 = min(a["y1"], b["y1"])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max((a["x1"] - a["x0"]) * (a["y1"] - a["y0"]), 1e-9)
    area_b = max((b["x1"] - b["x0"]) * (b["y1"] - b["y0"]), 1e-9)
    return inter / max(area_a + area_b - inter, 1e-9)


def _dedup_detections(dets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """오버랩 창 경계에서 중복·절단된 OCR 박스를 통합(비절단·긴 문자열·고신뢰도 우선)."""
    # 우선순위: 경계 비접촉(0 > 1) → 문자열 길이 → 신뢰도
    ranked = sorted(
        dets,
        key=lambda d: (0 if not d["near_seam"] else 1, -len(d["text"]), -d["confidence"]),
    )
    kept: list[dict[str, Any]] = []
    for cand in ranked:
        cx = (cand["x0"] + cand["x1"]) / 2
        cy = (cand["y0"] + cand["y1"]) / 2
        dup = False
        for prev in kept:
            if _iou(cand, prev) >= 0.35:
                dup = True
                break
            px = (prev["x0"] + prev["x1"]) / 2
            py = (prev["y0"] + prev["y1"]) / 2
            if abs(cx - px) < 0.014 and abs(cy - py) < 0.011:
                a = re.sub(r"\s+", "", cand["text"]).upper()
                b = re.sub(r"\s+", "", prev["text"]).upper()
                if a == b or a in b or b in a:
                    dup = True
                    break
        if not dup:
            kept.append(cand)
    kept.sort(key=lambda d: (round(d["y0"], 3), round(d["x0"], 3)))
    return kept


def ocr_pil_image(
    pil_img: Image.Image,
    window_size: int = 1280,
    overlap: int = 180,
    min_score: float = 0.45,
) -> tuple[str, list[dict[str, Any]]]:
    """고해상도 도면 PIL 이미지를 오버랩 슬라이딩 창으로 OCR하여 전체 텍스트와 정규화 단어 목록 반환."""
    import numpy as np

    eng = get_ocr_engine()
    w, h = pil_img.size
    if w <= 0 or h <= 0:
        return "", []

    stride = max(256, window_size - overlap)
    xs = list(range(0, max(1, w - overlap), stride))
    ys = list(range(0, max(1, h - overlap), stride))
    if not xs:
        xs = [0]
    if not ys:
        ys = [0]

    raw_dets: list[dict[str, Any]] = []
    seam_margin = 10

    for wy in ys:
        y1_px = min(wy + window_size, h)
        y0_px = max(0, y1_px - window_size) if (y1_px == h and h > window_size) else wy
        for wx in xs:
            x1_px = min(wx + window_size, w)
            x0_px = max(0, x1_px - window_size) if (x1_px == w and w > window_size) else wx
            crop = pil_img.crop((x0_px, y0_px, x1_px, y1_px))
            if not _window_has_ink(crop):
                continue

            arr = np.array(crop)
            out = eng(arr)
            if out is None or not getattr(out, "txts", None):
                continue
            boxes = out.boxes
            txts = out.txts
            scores = out.scores
            cw = x1_px - x0_px
            ch = y1_px - y0_px

            for box, txt, sc in zip(boxes, txts, scores):
                score = float(sc)
                clean = (txt or "").strip()
                if not clean or score < min_score:
                    continue
                bx0 = float(min(p[0] for p in box))
                by0 = float(min(p[1] for p in box))
                bx1 = float(max(p[0] for p in box))
                by1 = float(max(p[1] for p in box))
                if bx1 <= bx0 or by1 <= by0:
                    continue
                near_seam = (
                    (x0_px > 0 and bx0 <= seam_margin)
                    or (x1_px < w and bx1 >= cw - seam_margin)
                    or (y0_px > 0 and by0 <= seam_margin)
                    or (y1_px < h and by1 >= ch - seam_margin)
                )
                nx0 = max(0.0, min(1.0, (x0_px + bx0) / w))
                ny0 = max(0.0, min(1.0, (y0_px + by0) / h))
                nx1 = max(0.0, min(1.0, (x0_px + bx1) / w))
                ny1 = max(0.0, min(1.0, (y0_px + by1) / h))
                raw_dets.append({
                    "text": clean,
                    "x0": nx0,
                    "y0": ny0,
                    "x1": nx1,
                    "y1": ny1,
                    "confidence": round(score, 3),
                    "source": "ocr",
                    "rotated": (by1 - by0) > (bx1 - bx0) * 1.8,
                    "near_seam": near_seam,
                })

    deduped = _dedup_detections(raw_dets)
    words: list[dict[str, Any]] = []
    text_lines: list[str] = []
    for d in deduped:
        d.pop("near_seam", None)
        text_lines.append(d["text"])
        words.append(d)
        # 공백으로 구분된 복수 토큰(예: "FR HEX_N2US0501", "110A 110A-IESL-...")은
        # 구문 자체를 유지하면서 개별 태그·좌표 매칭용 하위 토큰도 비례 분할 추가
        parts = d["text"].split()
        if len(parts) >= 2 and not d.get("rotated"):
            total_len = max(len(d["text"]), 1)
            cursor = 0
            span_x = d["x1"] - d["x0"]
            for p in parts:
                pos = d["text"].find(p, cursor)
                if pos < 0:
                    pos = cursor
                cursor = pos + len(p)
                px0 = d["x0"] + span_x * (pos / total_len)
                px1 = d["x0"] + span_x * (cursor / total_len)
                if px1 > px0:
                    words.append({
                        "text": p,
                        "x0": px0,
                        "y0": d["y0"],
                        "x1": px1,
                        "y1": d["y1"],
                        "confidence": d["confidence"],
                        "source": "ocr",
                        "rotated": False,
                    })

    return "\n".join(text_lines), words


def run_ocr_for_pdf_pages(
    pdf_path: Path,
    page_indices: list[int],
    master_max_w: int = 3400,
) -> dict[int, dict[str, Any]]:
    """지정 PDF의 SHX 페이지만 렌더링·OCR하여 {page_idx: {text, words}} 딕셔너리 반환."""
    import pypdfium2 as pdfium

    out: dict[int, dict[str, Any]] = {}
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        for idx in page_indices:
            if idx < 0 or idx >= len(doc):
                continue
            page = doc[idx]
            pw, ph = page.get_size()
            if pw <= 0 or ph <= 0:
                page.close()
                continue
            scale = max(0.6, min(master_max_w / max(pw, ph), 3.2))
            bmp = page.render(scale=scale, rotation=0)
            pil_img = bmp.to_pil().convert("RGB")
            page.close()
            try:
                text, words = ocr_pil_image(pil_img)
                out[idx] = {"text": text, "words": words}
            finally:
                pil_img.close()
    finally:
        doc.close()
    return out


def main() -> int:
    """DS920+ 워커 메모리 격리 실행용 CLI (종료 즉시 ONNX Runtime RAM 100% OS 반환)."""
    ap = argparse.ArgumentParser(description="선택적 RapidOCR 단발 서브프로세스")
    ap.add_argument("--warmup", action="store_true", help="ONNX 모델 사전 다운로드")
    ap.add_argument("--pdf", help="대상 PDF 경로")
    ap.add_argument("--pages", default="", help="0-based 페이지 인덱스 콤마 목록 (예: 9,49,99)")
    ap.add_argument("--max-w", type=int, default=3400)
    ap.add_argument("--out-json", help="결과 JSON 저장 경로")
    args = ap.parse_args()

    if args.warmup:
        warmup_models()
        print("OCR_WARMUP_OK")
        return 0
    if not args.pdf or not args.pages:
        print("오류: --pdf 와 --pages 가 필요합니다", file=sys.stderr)
        return 2
    indices = [int(x) for x in args.pages.split(",") if x.strip().isdigit()]
    res = run_ocr_for_pdf_pages(Path(args.pdf), indices, master_max_w=args.max_w)
    payload = json.dumps({str(k): v for k, v in res.items()}, ensure_ascii=False)
    if args.out_json:
        Path(args.out_json).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
