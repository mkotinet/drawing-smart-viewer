#!/usr/bin/env python3
"""선택적 하이브리드 OCR 엔진 v2 (SHX 벡터 · 스캔 · 표제란 혼합 도면 전용, RapidOCR PP-OCRv5 ONNX).

- 2중 게이트 본문 전용 감지(needs_body_ocr): 표제란만 디지털이고 본문은 SHX/래스터인 도면까지 정밀 선별하고
  단순 디지털 표지(벡터 선분 < 200)는 0.1ms 만에 건너뛴다.
- 2-Pass Adaptive 400 DPI ROI 재OCR(refine_low_conf_rois): Pass 1(3400px) 저신뢰도 소형 버블만
  pypdfium2 네이티브 crop 렌더링으로 400 DPI 부분 재인식 후 개별 좌표를 페이지 좌표계로 역투영 교체한다.
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

OCR_ENGINE_VERSION = 2

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


def _page_vector_or_image_stats(page, path_cutoff: int = 200) -> tuple[int, float]:
    """PDFium 객체 순회로 벡터 선분(PATH=2) 개수와 래스터 이미지(IMAGE=3) 면적 비율을 고속 계측."""
    pw, ph = page.get_size()
    page_area = max(float(pw) * float(ph), 1.0)
    path_cnt = 0
    img_area = 0.0
    try:
        for obj in page.get_objects():
            otype = getattr(obj, "type", 0)
            if otype == 2:  # FPDF_PAGEOBJ_PATH
                path_cnt += 1
                if path_cnt >= path_cutoff:
                    return path_cnt, min(1.0, img_area / page_area)
            elif otype == 3:  # FPDF_PAGEOBJ_IMAGE
                try:
                    left, bottom, right, top = obj.get_bounds()
                    img_area += max(0.0, (right - left) * (top - bottom))
                except Exception:
                    pass
    except Exception:
        return 0, 0.0
    return path_cnt, min(1.0, img_area / page_area)


def needs_body_ocr(page, words: list[dict[str, Any]], full_text: str) -> bool:
    """2중 게이트 하이브리드 OCR 판별: 표제란 제외 본문 텍스트 부족 + 벡터/이미지 존재 검증.

    - 단순 디지털 표지(벡터 선분 < 200, 이미지 < 25%)는 건너뛰고,
    - 표제란만 디지털이고 본문은 SHX 벡터/스캔인 도면(도면목록, Hook-up, 일부 P&ID)은 정확히 선별한다.
    """
    if needs_ocr(full_text):
        return True

    body_meaningful = 0
    for w in words:
        # 우하단 표제란 영역 제외
        if w["x0"] >= 0.48 and w["y0"] >= 0.72:
            continue
        for tok in _SPLIT_RE.split((w.get("text") or "").upper()):
            if len(tok) >= 2 and not (set(tok) <= {"M", "*"}) and tok not in _TITLEBLOCK_BOILERPLATE:
                body_meaningful += 1
                if body_meaningful >= 12:
                    return False

    path_cnt, img_ratio = _page_vector_or_image_stats(page, path_cutoff=200)
    return path_cnt >= 200 or img_ratio >= 0.25


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
    """오버랩 창 경계에서 중복·절단된 OCR 박스를 통합(비절단·고신뢰도·긴 문자열 우선)."""
    ranked = sorted(
        dets,
        key=lambda d: (
            0 if not d.get("near_seam") else 1,
            0 if d.get("from_roi") else 1,
            -round(float(d.get("confidence", 0.0)), 2),
            -len(d.get("text", "")),
        ),
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


def merge_digital_and_ocr_words(
    digital_words: list[dict[str, Any]],
    ocr_words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """하이브리드 페이지에서 디지털 표제란 단어와 겹치는 OCR 중복 박스를 제거하고 안전 병합."""
    if not digital_words:
        return list(ocr_words)
    if not ocr_words:
        return list(digital_words)

    kept_ocr: list[dict[str, Any]] = []
    for ow in ocr_words:
        ocx = (ow["x0"] + ow["x1"]) / 2
        ocy = (ow["y0"] + ow["y1"]) / 2
        oa = re.sub(r"\s+", "", ow.get("text", "")).upper()
        overlap = False
        for dw in digital_words:
            if _iou(ow, dw) >= 0.35:
                overlap = True
                break
            dcx = (dw["x0"] + dw["x1"]) / 2
            dcy = (dw["y0"] + dw["y1"]) / 2
            if abs(ocx - dcx) < 0.015 and abs(ocy - dcy) < 0.012:
                da = re.sub(r"\s+", "", dw.get("text", "")).upper()
                if oa and da and (oa == da or oa in da or da in oa):
                    overlap = True
                    break
        if not overlap:
            kept_ocr.append(ow)
    return list(digital_words) + kept_ocr


_ROI_TAG_HINT_RE = re.compile(r"^[A-Z]{2,5}$")


def _is_roi_tag_candidate(text: str) -> bool:
    """Pass 2 400 DPI 재OCR 대상을 영숫자 태그·배관·계기 버블 후보로 제한 (일반 주석 낭비 차단)."""
    t = (text or "").strip().upper()
    if not (2 <= len(t) <= 32):
        return False
    has_alpha = any("A" <= ch <= "Z" for ch in t)
    has_digit = any("0" <= ch <= "9" for ch in t)
    if has_alpha and has_digit:
        return True
    # 2줄 버블 상단 계기 기능문자(HS, FT, FIT, FIC, TCV, DPIC 등) 근처
    if _ROI_TAG_HINT_RE.match(t) and t not in _TITLEBLOCK_BOILERPLATE:
        return True
    return False


def _cluster_low_conf_rois(
    dets: list[dict[str, Any]],
    conf_threshold: float = 0.86,
    max_rois: int = 14,
) -> list[tuple[float, float, float, float]]:
    """Pass 1 저신뢰도 소형 태그 버블만 인접 영역끼리 병합해 ROI 목록 생성 (일반 주석 스킵으로 7배 가속)."""
    cands = [
        d for d in dets
        if float(d.get("confidence", 1.0)) < conf_threshold
        and _is_roi_tag_candidate(d.get("text") or "")
        and (d["x1"] - d["x0"]) < 0.25
        and (d["y1"] - d["y0"]) < 0.14
    ]
    cands.sort(key=lambda d: float(d.get("confidence", 1.0)))
    rois: list[list[float]] = []
    pad_x, pad_y = 0.012, 0.014
    for d in cands:
        rx0 = max(0.0, d["x0"] - pad_x)
        ry0 = max(0.0, d["y0"] - pad_y)
        rx1 = min(1.0, d["x1"] + pad_x)
        ry1 = min(1.0, d["y1"] + pad_y)
        merged = False
        for r in rois:
            # 기존 ROI와 교차하거나 인접한 경우 하나로 합침 (단, 합친 크기가 너무 커지지 않도록 상한 제한)
            nx0, ny0 = min(r[0], rx0), min(r[1], ry0)
            nx1, ny1 = max(r[2], rx1), max(r[3], ry1)
            if (
                rx0 <= r[2] + 0.008 and rx1 >= r[0] - 0.008
                and ry0 <= r[3] + 0.010 and ry1 >= r[1] - 0.010
                and (nx1 - nx0) <= 0.22 and (ny1 - ny0) <= 0.16
            ):
                r[0], r[1], r[2], r[3] = nx0, ny0, nx1, ny1
                merged = True
                break
        if not merged:
            rois.append([rx0, ry0, rx1, ry1])
            if len(rois) >= max_rois:
                break
    return [(r[0], r[1], r[2], r[3]) for r in rois]


def refine_low_conf_rois(
    pdf_page,
    dets: list[dict[str, Any]],
    conf_threshold: float = 0.86,
    max_rois: int = 14,
    min_roi_score: float = 0.62,
) -> list[dict[str, Any]]:
    """Pass 2 Adaptive 400 DPI ROI 재OCR: 개별 크롭 박스를 페이지 정규화 좌표로 역투영 교체.

    - pypdfium2 네이티브 crop=(left, bottom, right, top)으로 작은 영역만 ~400 DPI 렌더링 (<2MB RAM).
    - 크롭 결과를 문자열 합치기(join)하지 않고 개별 박스로 역투영하여 인접 버블·노이즈 오염을 차단한다.
    - RapidOCR 내장 방향 분류기(cls)가 90°/270°를 자동 처리하므로 불필요한 수동 회전 반복을 생략한다.
    """
    import numpy as np

    rois = _cluster_low_conf_rois(dets, conf_threshold=conf_threshold, max_rois=max_rois)
    if not rois:
        return dets

    pw, ph = pdf_page.get_size()
    if pw <= 0 or ph <= 0:
        return dets

    eng = get_ocr_engine()
    roi_new_dets: list[dict[str, Any]] = []
    replaced_indices: set[int] = set()

    for rx0, ry0, rx1, ry1 in rois:
        span_x = rx1 - rx0
        span_y = ry1 - ry0
        if span_x <= 1e-4 or span_y <= 1e-4:
            continue
        # A1(1191pt) 기준 scale=5.5 ≈ 396 DPI, A0 등 초대형 도면에서도 크롭 최대 변 960px(<2.7MB) 캡
        max_pt = max(span_x * pw, span_y * ph, 1.0)
        roi_scale = max(2.5, min(5.5, 960.0 / max_pt))
        c_left = rx0 * pw
        c_right = (1.0 - rx1) * pw
        c_top = ry0 * ph
        c_bottom = (1.0 - ry1) * ph
        try:
            bmp = pdf_page.render(scale=roi_scale, rotation=0, crop=(c_left, c_bottom, c_right, c_top))
            pil = bmp.to_pil().convert("RGB")
        except Exception:
            continue

        try:
            cw, ch = pil.size
            if cw < 12 or ch < 12:
                continue
            out = eng(np.array(pil))
        finally:
            pil.close()

        if out is None or not getattr(out, "txts", None):
            continue

        local_reproj: list[dict[str, Any]] = []
        edge_margin = 4
        for box, txt, sc in zip(out.boxes, out.txts, out.scores):
            score = float(sc)
            clean = (txt or "").strip()
            if not clean or score < min_roi_score:
                continue
            bx0 = float(min(p[0] for p in box))
            by0 = float(min(p[1] for p in box))
            bx1 = float(max(p[0] for p in box))
            by1 = float(max(p[1] for p in box))
            if bx1 <= bx0 or by1 <= by0:
                continue
            # 크롭 가장자리에 잘려 걸린 1~2글자 잔상 제외
            touches_edge = (
                bx0 <= edge_margin or by0 <= edge_margin
                or bx1 >= cw - edge_margin or by1 >= ch - edge_margin
            )
            if touches_edge and (len(clean) <= 2 or score < 0.85):
                continue

            nx0 = max(0.0, min(1.0, rx0 + (bx0 / cw) * span_x))
            ny0 = max(0.0, min(1.0, ry0 + (by0 / ch) * span_y))
            nx1 = max(0.0, min(1.0, rx0 + (bx1 / cw) * span_x))
            ny1 = max(0.0, min(1.0, ry0 + (by1 / ch) * span_y))
            local_reproj.append({
                "text": clean,
                "x0": nx0,
                "y0": ny0,
                "x1": nx1,
                "y1": ny1,
                "confidence": round(score, 3),
                "source": "ocr",
                "rotated": (by1 - by0) > (bx1 - bx0) * 1.8,
                "near_seam": False,
                "from_roi": True,
            })

        if not local_reproj:
            continue

        # ROI 내부의 기존 저신뢰도 Pass-1 박스를 더 고신뢰도의 400 DPI 박스로 교체
        improved_any = False
        for i, old in enumerate(dets):
            if i in replaced_indices:
                continue
            if float(old.get("confidence", 1.0)) >= conf_threshold:
                continue
            ocx = (old["x0"] + old["x1"]) / 2
            ocy = (old["y0"] + old["y1"]) / 2
            if not (rx0 <= ocx <= rx1 and ry0 <= ocy <= ry1):
                continue
            for cand in local_reproj:
                if _iou(old, cand) >= 0.22 or (
                    abs(((cand["x0"] + cand["x1"]) / 2) - ocx) < 0.016
                    and abs(((cand["y0"] + cand["y1"]) / 2) - ocy) < 0.014
                ):
                    if cand["confidence"] >= float(old["confidence"]) + 0.03:
                        replaced_indices.add(i)
                        improved_any = True
                        break

        # ROI 내부 중심부에 새로 확보된 고신뢰도 토큰(예: FT 옆 FE, HS-01 대신 IWWA_NK2GC06) 반영
        if improved_any or any(c["confidence"] >= 0.85 for c in local_reproj):
            core_x0, core_y0 = rx0 + 0.003, ry0 + 0.003
            core_x1, core_y1 = rx1 - 0.003, ry1 - 0.003
            for c in local_reproj:
                ccx = (c["x0"] + c["x1"]) / 2
                ccy = (c["y0"] + c["y1"]) / 2
                if core_x0 <= ccx <= core_x1 and core_y0 <= ccy <= core_y1:
                    roi_new_dets.append(c)

    if not roi_new_dets:
        return dets

    merged = [d for i, d in enumerate(dets) if i not in replaced_indices] + roi_new_dets
    return _dedup_detections(merged)


def _expand_ocr_words(deduped: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """정규화 OCR 검출 목록에서 전체 텍스트와 가로/세로(Y축) 비례 분할 하위 토큰을 생성."""
    words: list[dict[str, Any]] = []
    text_lines: list[str] = []
    for d in deduped:
        d.pop("near_seam", None)
        d.pop("from_roi", None)
        text_lines.append(d["text"])
        words.append(d)
        # 공백으로 구분된 복수 토큰은 가로(X축) 또는 세로 회전(Y축) 비례 분할 추가
        parts = d["text"].split()
        if len(parts) >= 2:
            total_len = max(len(d["text"]), 1)
            cursor = 0
            if not d.get("rotated"):
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
            else:
                span_y = d["y1"] - d["y0"]
                for p in parts:
                    pos = d["text"].find(p, cursor)
                    if pos < 0:
                        pos = cursor
                    cursor = pos + len(p)
                    py0 = d["y0"] + span_y * (pos / total_len)
                    py1 = d["y0"] + span_y * (cursor / total_len)
                    if py1 > py0:
                        words.append({
                            "text": p,
                            "x0": d["x0"],
                            "y0": py0,
                            "x1": d["x1"],
                            "y1": py1,
                            "confidence": d["confidence"],
                            "source": "ocr",
                            "rotated": True,
                        })

    return "\n".join(text_lines), words


def ocr_pil_image(
    pil_img: Image.Image,
    window_size: int = 1280,
    overlap: int = 180,
    min_score: float = 0.45,
    pdf_page=None,
) -> tuple[str, list[dict[str, Any]]]:
    """고해상도 도면 PIL 이미지를 오버랩 슬라이딩 창으로 OCR (+ pdf_page 제공 시 Pass 2 400 DPI ROI 정밀 보정)."""
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
    if pdf_page is not None:
        deduped = refine_low_conf_rois(pdf_page, deduped)
    return _expand_ocr_words(deduped)


def run_ocr_for_pdf_pages(
    pdf_path: Path,
    page_indices: list[int],
    master_max_w: int = 3400,
) -> dict[int, dict[str, Any]]:
    """지정 PDF의 SHX/하이브리드 페이지만 Pass 1(3400px) + Pass 2(400 DPI ROI) OCR하여 반환."""
    import pypdfium2 as pdfium

    out: dict[int, dict[str, Any]] = {}
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        for idx in page_indices:
            if idx < 0 or idx >= len(doc):
                continue
            page = doc[idx]
            try:
                pw, ph = page.get_size()
                if pw <= 0 or ph <= 0:
                    continue
                scale = max(0.6, min(master_max_w / max(pw, ph), 3.2))
                bmp = page.render(scale=scale, rotation=0)
                pil_img = bmp.to_pil().convert("RGB")
                try:
                    text, words = ocr_pil_image(pil_img, pdf_page=page)
                    out[idx] = {"text": text, "words": words}
                finally:
                    pil_img.close()
            finally:
                page.close()
    finally:
        doc.close()
    return out


def main() -> int:
    """DS920+ 워커 메모리 격리 실행용 CLI (종료 즉시 ONNX Runtime RAM 100% OS 반환)."""
    ap = argparse.ArgumentParser(description="선택적 RapidOCR v2 단발 서브프로세스 (400 DPI ROI 포함)")
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
