"""도면 스마트 뷰어 백엔드 통합 테스트 (인증 · FTS5 검색 · 교차탐색 · 동기화 보안 · PWA 헤더)."""
from __future__ import annotations

import io
import sqlite3
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import init_drawings_db, init_state_db
from backend.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.db import invalidate_session_cache
    from backend.main import _login_fails, _user_fails
    _login_fails.clear()
    _user_fails.clear()
    invalidate_session_cache()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
    monkeypatch.setenv("SYNC_TOKEN", "sync-secret-xyz")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    init_state_db()
    init_drawings_db()
    with TestClient(app) as c:
        yield c, tmp_path
    _login_fails.clear()
    _user_fails.clear()
    invalidate_session_cache()


def _make_tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_pwa_headers_and_healthz(client):
    c, _ = client
    assert c.get("/api/healthz").status_code == 200
    m = c.get("/manifest.json")
    assert m.status_code == 200
    assert "no-store" in m.headers["cache-control"]
    assert c.head("/manifest.json").status_code == 200

    sw = c.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers.get("service-worker-allowed") == "/"
    assert c.head("/sw.js").status_code == 200


def test_auth_protection_login_audit_and_rbac(client):
    c, _ = client
    assert c.get("/api/stats").status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401

    r = c.post("/api/auth/login", json={"username": "admin", "password": "testpass123"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["is_admin"] is True

    logs = c.get("/api/admin/audit-logs", headers={"Authorization": f"Bearer {token}"}).json()["items"]
    actions = [x["action"] for x in logs]
    assert "login_success" in actions and "login_failed" in actions


def test_sync_upload_rejects_path_traversal_and_serves_indexed_drawings(client, tmp_path):
    c, data_dir = client
    # 1) 경로 이탈 시도 차단
    bad_tar = _make_tar({"../evil.txt": b"hack"})
    r_bad = c.post(
        "/api/admin/sync",
        headers={"X-Sync-Token": "sync-secret-xyz"},
        files={"bundle": ("bad.tar.gz", bad_tar, "application/gzip")},
    )
    assert r_bad.status_code == 400

    # 2) 정상 DB + 타일 번들 동기화
    stage_db = tmp_path / "src_drawings.db"
    init_drawings_db(stage_db)
    conn = sqlite3.connect(stage_db)
    conn.execute(
        "INSERT INTO sheets(sheet_id, category, subcategory, drawing_no, title, source_file, file_type, "
        "width_px, height_px, sha256, has_preview, tag_count) "
        "VALUES ('HCD_001', '제어 도면', '계기 결선도(HCD)', 'DW0001-U01-HCD-A01-0001', 'HCD-0001', "
        "'a.pdf', 'pdf', 1600, 1130, 'deadbeef00', 1, 1)"
    )
    conn.execute(
        "INSERT INTO tags(sheet_id, tag, kind, func, unit, loop, x0, y0, x1, y1) "
        "VALUES ('HCD_001', 'TE-AHU01-101C', 'instrument', 'TE', 'AHU01', '101C', 0.1, 0.2, 0.2, 0.25)"
    )
    conn.execute(
        "INSERT INTO sheets_fts(sheet_id, drawing_no, title, category, subcategory, tags_joined, full_text) "
        "VALUES ('HCD_001', 'DW0001-U01-HCD-A01-0001', 'HCD-0001', '제어 도면', '계기 결선도(HCD)', 'TE-AHU01-101C', 'AHU')"
    )
    conn.commit()
    conn.close()

    good_tar = _make_tar({
        "drawings.db": stage_db.read_bytes(),
        "thumbs/HCD_001.webp": b"RIFFxxxxWEBP",
        "tiles/HCD_001/preview.webp": b"RIFFxxxxWEBP",
    })
    r_ok = c.post(
        "/api/admin/sync",
        headers={"X-Sync-Token": "sync-secret-xyz"},
        files={"bundle": ("good.tar.gz", good_tar, "application/gzip")},
    )
    assert r_ok.status_code == 200 and r_ok.json()["extracted_files"] == 3

    # 3) 로그인 후 검색 · 교차참조 · 불변 캐시 헤더 확인
    tok = c.post("/api/auth/login", json={"username": "admin", "password": "testpass123"}).json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}

    res = c.get("/api/drawings?q=AHU01", headers=hdr).json()
    assert res["total"] == 1 and res["items"][0]["sheet_id"] == "HCD_001"

    xref = c.get("/api/tags/TE-AHU01-101C/sheets", headers=hdr).json()
    assert xref["count"] == 1 and xref["occurrences"][0]["sheet_id"] == "HCD_001"

    tile_resp = c.get("/api/sheets/HCD_001/preview.webp", headers=hdr)
    assert tile_resp.status_code == 200
    assert "immutable" in tile_resp.headers["cache-control"]


def test_admin_env_password_sync_and_user_management(client, monkeypatch):
    c, _ = client
    # 환경변수 비밀번호 변경 후 재초기화 시 기존 관리자 비밀번호 자동 동기화
    monkeypatch.setenv("ADMIN_PASSWORD", "rotatedPass99!")
    init_state_db()
    assert c.post("/api/auth/login", json={"username": "admin", "password": "testpass123"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": "rotatedPass99!"})
    assert r.status_code == 200
    hdr = {"Authorization": f"Bearer {r.json()['token']}"}

    # 계정 생성 · 조회 · 본인 삭제 차단 · 타 계정 삭제 검증
    assert c.post("/api/admin/users", json={"username": "eng1", "password": "user1234", "role": "engineer"}, headers=hdr).status_code == 200
    users = [u["username"] for u in c.get("/api/admin/users", headers=hdr).json()["items"]]
    assert "admin" in users and "eng1" in users
    assert c.delete("/api/admin/users/admin", headers=hdr).status_code == 400
    assert c.delete("/api/admin/users/eng1", headers=hdr).status_code == 200
    assert c.post("/api/auth/login", json={"username": "eng1", "password": "user1234"}).status_code == 401


def test_sync_status_and_incremental_classify_path(client):
    c, data_dir = client
    from scripts.build_index import classify_path

    # 다중 리비전 경로 분류 검증 (상위 폴더 감시 대응)
    assert classify_path(Path("2_AFC_0/4_P&ID/sample_UT.pdf")) == ("P&ID", "2_AFC_0 · UT공정 P&ID")
    assert classify_path(Path("1_AFD_2026.09.11/2_제어/DW0001-U01-HCD-0001.PDF")) == ("제어 도면", "1_AFD_2026.09.11 · 계기 결선도(HCD)")
    assert classify_path(Path("2_AFC_0/3_제어_AFC(그린동)/GREEN/sheet01.PDF")) == ("제어 도면", "2_AFC_0 · 그린동 제어")

    # 워커 미연결 상태에서도 관리자 동기화 상태 API가 안전 응답(200) 반환
    tok = c.post("/api/auth/login", json={"username": "admin", "password": "testpass123"}).json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}
    (data_dir / "sync_status.json").write_text('{"state":"idle","last_checked_kst":"2026-11-24 23:00:00 KST"}', encoding="utf-8")
    st = c.get("/api/admin/sync/status", headers=hdr)
    assert st.status_code == 200
    assert st.json()["worker_enabled"] is False
    assert st.json()["last_checked_kst"] == "2026-11-24 23:00:00 KST"
    # 워커 미연결 시 trigger는 503 반환
    assert c.post("/api/admin/sync/trigger", headers=hdr).status_code == 503


def test_incremental_index_reuse_add_and_orphan_cleanup(tmp_path):
    from pypdf import PdfWriter
    from scripts.build_index import build_index_incremental

    raw_dir = tmp_path / "raw" / "2_AFC_0" / "4_P&ID"
    raw_dir.mkdir(parents=True)
    out_dir = tmp_path / "out"

    def _make_blank_pdf(path: Path, width: int = 400, height: int = 300):
        w = PdfWriter()
        w.add_blank_page(width=width, height=height)
        with open(path, "wb") as f:
            w.write(f)

    pdf1 = raw_dir / "sample_A.PDF"
    _make_blank_pdf(pdf1, 400, 300)

    # 1차 실행: 신규 1장 추가
    r1 = build_index_incremental(tmp_path / "raw", out_dir, workers=1, project_name="테스트")
    assert r1["status"] == "updated" and r1["added_pdfs"] == 1 and r1["total_sheets"] == 1
    thumb_files = list((out_dir / "thumbs").glob("*.webp"))
    assert len(thumb_files) == 1

    # 2차 실행(변경 없음): 렌더링 0회, no_changes
    r2 = build_index_incremental(tmp_path / "raw", out_dir, workers=1, project_name="테스트")
    assert r2["status"] == "no_changes" and r2["added_pdfs"] == 0 and r2["total_sheets"] == 1

    # 3차 실행(신규 1개 추가 + 기존 1개 삭제): 증분 교체 + 고아 썸네일 삭제
    pdf2 = raw_dir / "sample_B.pdf"
    _make_blank_pdf(pdf2, 420, 300)
    pdf1.unlink()
    r3 = build_index_incremental(tmp_path / "raw", out_dir, workers=1, project_name="테스트")
    assert r3["status"] == "updated" and r3["added_pdfs"] == 1 and r3["removed_pdfs"] == 1 and r3["total_sheets"] == 1
    assert not thumb_files[0].exists()
    assert len(list((out_dir / "thumbs").glob("*.webp"))) == 1


def test_security_headers_head_root_and_login_rate_limit(client):
    c, _ = client
    # 1) HEAD / 200 OK + 보안 응답 헤더 검증
    head_r = c.head("/")
    assert head_r.status_code == 200
    assert head_r.headers.get("x-content-type-options") == "nosniff"
    assert head_r.headers.get("x-frame-options") == "DENY"
    assert "default-src 'self'" in head_r.headers.get("content-security-policy", "")

    # 2) 로그인 레이트 리밋 (10회 실패 후 11회째 429 차단)
    for _ in range(10):
        assert c.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 429


def test_false_positive_tag_filter_and_sqlite_query_plans(tmp_path):
    from scripts.build_index import is_shx_vector_text, is_valid_tag

    # 오탐 차단 확인
    assert not is_valid_tag("SF-7544", "equipment")
    assert not is_valid_tag("VT-15A", "instrument", "VT", None, "15A")
    assert not is_valid_tag("PT-25A", "instrument", "PT", None, "25A")
    assert not is_valid_tag("KS-KS-10K", "instrument", "KS", "KS", "10K")
    assert not is_valid_tag("AC-AC-220V", "instrument", "AC", "AC", "220V")
    # 정상 계기/기기/라인 허용 확인
    assert is_valid_tag("TE-101", "instrument", "TE", None, "101")
    assert is_valid_tag("TE-AHU01-101C", "instrument", "TE", "AHU01", "101C")
    assert is_valid_tag("PG-11", "instrument", "PG", None, "11")
    assert is_valid_tag("RS-485", "instrument", "RS", None, "485")
    assert is_valid_tag("50A-CHW-1001", "line_no")
    assert is_shx_vector_text("TITLE\n" + " ".join(["M"] * 20))
    assert not is_shx_vector_text("PIPING AND INSTRUMENT DIAGRAM AHU-01 SUPPLY FAN TE-101 TIC-102 FLOW CONTROL")

    # EXPLAIN QUERY PLAN 검증: 3개 핵심 쿼리 모두 풀스캔/임시정렬 제거
    db_path = tmp_path / "plan.db"
    init_drawings_db(db_path)
    conn = sqlite3.connect(db_path)
    p_xref = str(conn.execute(
        "EXPLAIN QUERY PLAN SELECT t.id FROM tags t WHERE t.tag = ? COLLATE NOCASE", ("RS-485",)
    ).fetchall())
    p_sib = str(conn.execute(
        "EXPLAIN QUERY PLAN SELECT sheet_id FROM sheets WHERE source_file = ? ORDER BY page_no", ("a.pdf",)
    ).fetchall())
    p_list = str(conn.execute(
        "EXPLAIN QUERY PLAN SELECT sheet_id FROM sheets ORDER BY category, subcategory, title, page_no LIMIT 40"
    ).fetchall())
    conn.close()
    assert "idx_tags_tag_nocase" in p_xref and "SEARCH" in p_xref
    assert "idx_sheets_source" in p_sib and "TEMP B-TREE" not in p_sib
    assert "idx_sheets_cat_order" in p_list and "TEMP B-TREE" not in p_list


def test_revision_filter_search_snippet_and_field_pins_idor(client):
    c, tmp_path = client
    tok_admin = c.post("/api/auth/login", json={"username": "admin", "password": "testpass123"}).json()["token"]
    hdr_admin = {"Authorization": f"Bearer {tok_admin}"}

    # 엔지니어 일반 계정 추가
    assert c.post("/api/admin/users", json={"username": "eng1", "password": "engpass123", "role": "engineer"}, headers=hdr_admin).status_code == 200
    tok_eng = c.post("/api/auth/login", json={"username": "eng1", "password": "engpass123"}).json()["token"]
    hdr_eng = {"Authorization": f"Bearer {tok_eng}"}

    # 2개 리비전 시트 시드
    stage = tmp_path / "rev.db"
    init_drawings_db(stage)
    conn = sqlite3.connect(stage)
    conn.execute(
        "INSERT INTO sheets(sheet_id, category, subcategory, revision, drawing_no, title, source_file, file_type, "
        "width_px, height_px, sha256, has_preview, tag_count) "
        "VALUES ('S1', 'P&ID', '2_AFC_0 · 공정', '2_AFC_0', 'DW-1', 'S1', '2_AFC_0/a.pdf', 'pdf', 1600, 1130, 'aaaa1111', 1, 0)"
    )
    conn.execute(
        "INSERT INTO sheets_fts(sheet_id, drawing_no, title, category, subcategory, tags_joined, full_text) "
        "VALUES ('S1', 'DW-1', 'S1', 'P&ID', '2_AFC_0 · 공정', '', 'SUPPLY CHILLED WATER VALVE AHU-09 CONTROL')"
    )
    conn.execute(
        "INSERT INTO sheets(sheet_id, category, subcategory, revision, drawing_no, title, source_file, file_type, "
        "width_px, height_px, sha256, has_preview, tag_count) "
        "VALUES ('S2', 'P&ID', '1_AFD · 공정', '1_AFD', 'DW-2', 'S2', '1_AFD/b.pdf', 'pdf', 1600, 1130, 'bbbb2222', 1, 0)"
    )
    conn.commit()
    conn.close()
    bundle = _make_tar({"drawings.db": stage.read_bytes()})
    assert c.post("/api/admin/sync", headers={"X-Sync-Token": "sync-secret-xyz"},
                  files={"bundle": ("rev.tar.gz", bundle, "application/gzip")}).status_code == 200

    # 리비전 필터 + FTS5 스니펫 검증
    st = c.get("/api/stats", headers=hdr_admin).json()
    assert {r["name"] for r in st["revisions"]} == {"2_AFC_0", "1_AFD"}
    res = c.get("/api/drawings?revision=2_AFC_0&q=AHU-09", headers=hdr_admin).json()
    assert res["total"] == 1 and "[[H]]" in res["items"][0].get("snippet", "")

    # 현장 메모 핀 CRUD + IDOR 방어 (admin 핀을 eng1이 삭제 불가)
    pin = c.post("/api/sheets/S1/pins", json={"x": 0.42, "y": 0.55, "note": "밸브 간섭 확인"}, headers=hdr_admin)
    assert pin.status_code == 200
    pin_id = pin.json()["id"]
    assert c.delete(f"/api/pins/{pin_id}", headers=hdr_eng).status_code == 403
    assert len(c.get("/api/drawings/S1", headers=hdr_eng).json()["pins"]) == 1
    assert c.delete(f"/api/pins/{pin_id}", headers=hdr_admin).status_code == 200
    assert len(c.get("/api/sheets/S1/pins", headers=hdr_admin).json()["items"]) == 0


def test_v130_plant_parser_trigram_substring_lineage_and_continuations(client, tmp_path):
    from scripts.build_index import (
        _write_drawings_db_atomic,
        extract_dwg_meta_and_continuations,
        extract_tags_from_words,
        snap_ocr_token,
    )
    from scripts.ocr_engine import needs_ocr

    # 1) 선택적 OCR 판별 (표제란만 있는 SHX vs 디지털 본문)
    shx_text = "PROJECT TITLE PROJECT CODE DRAWING TITLE OWNER CONTRACTOR REV DATE 12345678 ABCDEF M M M"
    digital_text = (
        "COOLING WATER SUPPLY SYSTEM 550A-CWR-061213-A1X TCV N2USCW31 DPIC N2USCW31 "
        "BFV-006R08 BV-006R07 HEX_N2US0501 CWP_N2US0701 AHU_N2US0101 CONTROL VALVE NOTES"
    )
    assert needs_ocr(shx_text) is True
    assert needs_ocr(digital_text) is False

    # 2) NRD-K2 언더스코어·6자리 배관·수동밸브·2줄 영숫자 버블·OCR 퍼지 보정·연결 화살표 파싱 검증
    words = [
        {"text": "550A-CWR-061213-A1X", "x0": 0.20, "y0": 0.20, "x1": 0.35, "y1": 0.22, "confidence": 1.0, "source": "pdf_text"},
        {"text": "110A-IESL-070602B-A1Y", "x0": 0.20, "y0": 0.26, "x1": 0.36, "y1": 0.28, "confidence": 0.99, "source": "ocr"},
        {"text": "TIC_N2USHCW31", "x0": 0.40, "y0": 0.30, "x1": 0.50, "y1": 0.32, "confidence": 1.0, "source": "pdf_text"},
        {"text": "ICGP_N2GC0202A", "x0": 0.12, "y0": 0.40, "x1": 0.24, "y1": 0.42, "confidence": 0.99, "source": "ocr"},
        {"text": "ICGP_N2GC0202B", "x0": 0.12, "y0": 0.55, "x1": 0.24, "y1": 0.57, "confidence": 1.00, "source": "ocr"},
        {"text": "*BFV-006R08", "x0": 0.55, "y0": 0.22, "x1": 0.63, "y1": 0.24, "confidence": 1.0, "source": "pdf_text"},
        {"text": "MCC-NK2U1B4-A", "x0": 0.60, "y0": 0.30, "x1": 0.72, "y1": 0.32, "confidence": 1.0, "source": "pdf_text"},
        # 2줄 버블 (TCV / N2USCW31, DPIC / N2USCW31)
        {"text": "TCV", "x0": 0.30, "y0": 0.48, "x1": 0.33, "y1": 0.495, "confidence": 1.0, "source": "pdf_text"},
        {"text": "N2USCW31", "x0": 0.29, "y0": 0.50, "x1": 0.35, "y1": 0.515, "confidence": 1.0, "source": "pdf_text"},
        {"text": "DPIC", "x0": 0.42, "y0": 0.48, "x1": 0.45, "y1": 0.495, "confidence": 1.0, "source": "pdf_text"},
        {"text": "N2USCW31", "x0": 0.41, "y0": 0.50, "x1": 0.47, "y1": 0.515, "confidence": 1.0, "source": "pdf_text"},
        # 사각 DCS 버블 OCR 오타 보정 (KC / NQGP_N2GC0202A → KC_ICGP_N2GC0202A)
        {"text": "KC", "x0": 0.28, "y0": 0.40, "x1": 0.31, "y1": 0.415, "confidence": 1.0, "source": "ocr"},
        {"text": "NQGP_N2GC0202A", "x0": 0.27, "y0": 0.42, "x1": 0.33, "y1": 0.435, "confidence": 0.83, "source": "ocr"},
        # 도면 간 연결 화살표 (좌측 경계) + 자체 표제란 ID (우하단)
        {"text": "CWR", "x0": 0.05, "y0": 0.14, "x1": 0.09, "y1": 0.155, "confidence": 1.0, "source": "pdf_text"},
        {"text": "K0919000_PCDP001_006A", "x0": 0.04, "y0": 0.16, "x1": 0.16, "y1": 0.175, "confidence": 1.0, "source": "pdf_text"},
        {"text": "FR", "x0": 0.05, "y0": 0.18, "x1": 0.07, "y1": 0.195, "confidence": 1.0, "source": "pdf_text"},
        {"text": "HEX_N2US0501", "x0": 0.08, "y0": 0.18, "x1": 0.17, "y1": 0.195, "confidence": 1.0, "source": "pdf_text"},
        {"text": "K0919000_PCDP001_006R", "x0": 0.72, "y0": 0.88, "x1": 0.88, "y1": 0.90, "confidence": 1.0, "source": "pdf_text"},
        {"text": "SF7544-ES0D-PID-F11U-U006R", "x0": 0.72, "y0": 0.84, "x1": 0.90, "y1": 0.86, "confidence": 1.0, "source": "pdf_text"},
    ]
    tags = extract_tags_from_words(words)
    by_tag = {t["tag"]: t for t in tags}
    assert "550A-CWR-061213-A1X" in by_tag and by_tag["550A-CWR-061213-A1X"]["kind"] == "line_no"
    assert by_tag["550A-CWR-061213-A1X"]["loop_key"] == "CWR-061213"
    assert "110A-IESL-070602B-A1Y" in by_tag and by_tag["110A-IESL-070602B-A1Y"]["loop_key"] == "IESL-070602"
    assert "TIC_N2USHCW31" in by_tag and by_tag["TIC_N2USHCW31"]["kind"] == "instrument"
    assert "BFV-006R08" in by_tag and by_tag["BFV-006R08"]["kind"] == "valve"
    assert "MCC-NK2U1B4-A" in by_tag and by_tag["MCC-NK2U1B4-A"]["loop_key"] == "NK2U1B4"
    assert "TCV_N2USCW31" in by_tag and "DPIC_N2USCW31" in by_tag
    assert "KC_ICGP_N2GC0202A" in by_tag
    assert snap_ocr_token("XXXXXXXXXXXXXX", ["ICGP_N2GC0202A"]) is None

    dwg_no, dwg_id, conts = extract_dwg_meta_and_continuations(words, " ".join(w["text"] for w in words), "UT_PID")
    assert dwg_no == "SF7544-ES0D-PID-F11U-U006R"
    assert dwg_id == "K0919000_PCDP001_006R"
    assert len(conts) == 1 and conts[0]["direction"] == "FR" and conts[0]["fluid"] == "CWR"
    assert conts[0]["target_dwg"] == "K0919000_PCDP001_006A"

    # 3) 원자 DB 적재 + Trigram 부분검색 + 구분자 무시 추적 + 루프 패밀리 + 연결 화살표 타겟 해결 E2E
    c, data_dir = client
    stage_dir = tmp_path / "stage_v130"
    stage_dir.mkdir()
    sheets = [
        {
            "sheet_id": "PID_006R", "category": "P&ID", "subcategory": "2_AFC_0 · UT공정 P&ID",
            "revision": "2_AFC_0", "drawing_no": dwg_no, "drawing_id": dwg_id, "title": "UT P&ID U006R",
            "source_file": "2_AFC_0/4_P&ID/UT.pdf", "file_type": "pdf", "page_no": 1, "total_pages": 2,
            "width_px": 3400, "height_px": 2400, "tile_cols": 4, "tile_rows": 3, "tile_size": 1024,
            "sha256": "abcd1111", "has_pdf": 1, "has_preview": 1, "extra_categories": "", "ocr_status": "ocr_done",
            "full_text": "무기통합2차폐수 MVD_N2USAHU0301AA 550A-CWR-061213-A1X TIC_N2USHCW31",
            "tags": tags, "continuations": conts,
        },
        {
            "sheet_id": "PID_006A", "category": "P&ID", "subcategory": "2_AFC_0 · UT공정 P&ID",
            "revision": "2_AFC_0", "drawing_no": "SF7544-ES0D-PID-F11U-U006A", "drawing_id": "K0919000_PCDP001_006A",
            "title": "UT P&ID U006A", "source_file": "2_AFC_0/4_P&ID/UT.pdf", "file_type": "pdf",
            "page_no": 2, "total_pages": 2, "width_px": 3400, "height_px": 2400, "tile_cols": 4, "tile_rows": 3,
            "tile_size": 1024, "sha256": "abcd1111", "has_pdf": 1, "has_preview": 1, "extra_categories": "",
            "ocr_status": "", "full_text": "HEX_N2US0501 550A-CWR-061213-A1X",
            "tags": [
                {"tag": "TIC-N2USHCW31", "kind": "instrument", "func": "TIC", "unit": None, "loop": "N2USHCW31",
                 "x0": 0.2, "y0": 0.2, "x1": 0.3, "y1": 0.25, "confidence": 1.0, "source": "pdf_text", "raw_text": "TIC-N2USHCW31"},
            ],
            "continuations": [],
        },
        {
            "sheet_id": "HCD_001", "category": "제어 도면", "subcategory": "2_AFC_0 · 계기 결선도(HCD)",
            "revision": "2_AFC_0", "drawing_no": "SF7544-EK0D-HCD-F11U-0001", "drawing_id": "",
            "title": "HCD-0001", "source_file": "2_AFC_0/2_제어/HCD.pdf", "file_type": "pdf",
            "page_no": 1, "total_pages": 1, "width_px": 1600, "height_px": 1130, "tile_cols": 0, "tile_rows": 0,
            "tile_size": 0, "sha256": "efgh2222", "has_pdf": 1, "has_preview": 1, "extra_categories": "",
            "ocr_status": "", "full_text": "TIC_N2USHCW31 WIRING",
            "tags": [
                {"tag": "TIC_N2USHCW31", "kind": "instrument", "func": "TIC", "unit": None, "loop": "N2USHCW31",
                 "x0": 0.3, "y0": 0.3, "x1": 0.4, "y1": 0.35, "confidence": 1.0, "source": "pdf_text", "raw_text": "TIC_N2USHCW31"},
            ],
            "continuations": [],
        },
    ]
    _write_drawings_db_atomic(stage_dir, sheets, {"project": "NRD-K2"}, run_vacuum=False)
    bundle = _make_tar({"drawings.db": (stage_dir / "drawings.db").read_bytes()})
    assert c.post("/api/admin/sync", headers={"X-Sync-Token": "sync-secret-xyz"},
                  files={"bundle": ("v130.tar.gz", bundle, "application/gzip")}).status_code == 200

    tok = c.post("/api/auth/login", json={"username": "admin", "password": "testpass123"}).json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}

    # Trigram 부분 문자열 검색: '61213', 'AHU0301', '무기통합'
    for subq in ("61213", "AHU0301", "무기통합"):
        r = c.get(f"/api/drawings?q={subq}", headers=hdr).json()
        assert r["total"] >= 1 and any(it["sheet_id"] == "PID_006R" for it in r["items"])
        hit = next(it for it in r["items"] if it["sheet_id"] == "PID_006R")
        assert "[[H]]" in hit.get("snippet", "")

    # 구분자 무시(_ ↔ -) 공종별 추적 순서(P&ID 먼저, 제어 도면 다음)
    xref = c.get("/api/tags/TIC-N2USHCW31/sheets", headers=hdr).json()
    assert xref["count"] == 3
    assert xref["occurrences"][0]["category"] == "P&ID"
    assert xref["occurrences"][-1]["category"] == "제어 도면"

    # 루프 패밀리 검증 (TCV_N2USCW31 → DPIC_N2USCW31 자동 연결)
    fam = c.get("/api/tags/TCV_N2USCW31/sheets", headers=hdr).json()
    assert fam["loop_key"] == "N2USCW31"
    assert any(f["tag"] == "DPIC_N2USCW31" for f in fam["family"])

    # 배관 라인 유형 필터 검증
    lines = c.get("/api/tags?kind=line_no&q=061213", headers=hdr).json()
    assert len(lines["items"]) == 1 and lines["items"][0]["tag"] == "550A-CWR-061213-A1X"
    assert lines["kind_counts"]["line_no"] >= 2

    # 도면 간 연결 화살표(나가는/유입) 자동 해결 검증
    det_r = c.get("/api/drawings/PID_006R", headers=hdr).json()
    assert det_r["ocr_status"] == "ocr_done"
    assert len(det_r["continuations"]) == 1
    assert det_r["continuations"][0]["target_sheet_id"] == "PID_006A"

    det_a = c.get("/api/drawings/PID_006A", headers=hdr).json()
    assert len(det_a["incoming_continuations"]) == 1
    assert det_a["incoming_continuations"][0]["from_sheet_id"] == "PID_006R"


def test_v131_security_hardening_session_hash_cache_and_xff_spoof_block(client, monkeypatch):
    import hashlib
    import backend.main as main_mod
    from backend.config import get_auth_db_path, now_kst_iso

    c, _ = client

    # 1) 존재하지 않는 계정 로그인 시에도 더미 PBKDF2 수행 (타이밍 계정 열거 방어)
    calls = {"n": 0}
    orig_verify = main_mod.verify_password

    def _spy_verify(pw, stored):
        calls["n"] += 1
        return orig_verify(pw, stored)

    monkeypatch.setattr(main_mod, "verify_password", _spy_verify)
    monkeypatch.setattr(main_mod, "USER_FAIL_SLOWDOWN_S", 0.0)
    r_ghost = c.post("/api/auth/login", json={"username": "no_such_user_xyz", "password": "wrong"})
    assert r_ghost.status_code == 401
    assert calls["n"] == 1
    monkeypatch.setattr(main_mod, "verify_password", orig_verify)

    # 2) 타 IP에서 admin 12회 실패해도 정상 IP(testclient) 로그인은 잠기지 않음 (계정 잠금 DoS 방지)
    for _ in range(12):
        main_mod._record_login_failure("203.0.113.77", "admin")
    r_ok = c.post("/api/auth/login", json={"username": "admin", "password": "testpass123"})
    assert r_ok.status_code == 200
    admin_tok = r_ok.json()["token"]
    admin_hdr = {"Authorization": f"Bearer {admin_tok}"}

    # 3) DB에는 평문 토큰 대신 SHA-256 해시만 저장
    sconn = sqlite3.connect(get_auth_db_path())
    stored_tokens = {r[0] for r in sconn.execute("SELECT token FROM sessions").fetchall()}
    assert admin_tok not in stored_tokens
    assert hashlib.sha256(admin_tok.encode("utf-8")).hexdigest() in stored_tokens

    # 4) 구버전 평문 세션 무중단 호환 + 첫 조회 시 해시 자동 승격
    legacy_tok = "legacy-plain-session-token-001"
    now_s = now_kst_iso()
    sconn.execute(
        "INSERT INTO sessions(token, username, role, user_agent, ip, created_at, last_used_at) "
        "VALUES (?, 'admin', 'admin', 'mobile', '127.0.0.1', ?, ?)",
        (legacy_tok, now_s, now_s),
    )
    sconn.commit()
    sconn.close()
    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {legacy_tok}"}).status_code == 200
    sconn = sqlite3.connect(get_auth_db_path())
    upgraded_tokens = {r[0] for r in sconn.execute("SELECT token FROM sessions").fetchall()}
    sconn.close()
    assert legacy_tok not in upgraded_tokens
    assert hashlib.sha256(legacy_tok.encode("utf-8")).hexdigest() in upgraded_tokens

    # 5) 60초 세션 캐시 적중 상태에서 계정 삭제·로그아웃 즉시 무효화
    assert c.post(
        "/api/admin/users",
        json={"username": "eng_cache", "password": "user1234", "role": "engineer"},
        headers=admin_hdr,
    ).status_code == 200
    eng_tok = c.post("/api/auth/login", json={"username": "eng_cache", "password": "user1234"}).json()["token"]
    eng_hdr = {"Authorization": f"Bearer {eng_tok}"}
    assert c.get("/api/auth/me", headers=eng_hdr).status_code == 200  # 캐시 채움
    assert c.delete("/api/admin/users/eng_cache", headers=admin_hdr).status_code == 200
    assert c.get("/api/auth/me", headers=eng_hdr).status_code == 401  # 즉시 무효화 확인

    assert c.post("/api/auth/logout", headers={"Authorization": f"Bearer {legacy_tok}"}).status_code == 200
    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {legacy_tok}"}).status_code == 401

    # 6) X-Forwarded-For 위조 헤더를 매번 바꿔도 레이트 리밋 우회 불가
    main_mod._login_fails.clear()
    for i in range(10):
        resp = c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "bad"},
            headers={"X-Forwarded-For": f"198.51.100.{i + 1}"},
        )
        assert resp.status_code == 401
    blocked = c.post(
        "/api/auth/login",
        json={"username": "admin", "password": "bad"},
        headers={"X-Forwarded-For": "198.51.100.250"},
    )
    assert blocked.status_code == 429

    # 7) 레이트 리밋 딕셔너리 메모리 상한 캡 검증
    main_mod._login_fails.clear()
    for i in range(main_mod.MAX_RATE_LIMIT_KEYS + 200):
        main_mod._record_login_failure(f"10.0.{i // 256}.{i % 256}")
    assert len(main_mod._login_fails) <= main_mod.MAX_RATE_LIMIT_KEYS + 1


def test_v131_sync_2phase_atomicity_dotslash_db_ro_mode_and_like_escape(client, tmp_path):
    c, data_dir = client

    # 1) 2단계 사전 검증: 정상 파일 뒤에 경로 이탈 항목이 있어도 선행 파일이 디스크에 남지 않음
    poison_tar = _make_tar({
        "thumbs/SHOULD_NOT_BE_WRITTEN.webp": b"RIFFxxxxWEBP",
        "../escape.txt": b"evil",
    })
    r_bad = c.post(
        "/api/admin/sync",
        headers={"X-Sync-Token": "sync-secret-xyz"},
        files={"bundle": ("poison.tar.gz", poison_tar, "application/gzip")},
    )
    assert r_bad.status_code == 400
    assert not (data_dir / "thumbs" / "SHOULD_NOT_BE_WRITTEN.webp").exists()

    # 2) ./drawings.db 정규화 + 기존 -wal/-shm 정리 + 읽기 전용 mode=ro에서 DELETE 저널 유지
    stage_db = tmp_path / "dotslash.db"
    init_drawings_db(stage_db)
    conn = sqlite3.connect(stage_db)
    conn.execute(
        "INSERT INTO sheets(sheet_id, category, subcategory, drawing_no, title, source_file, file_type, "
        "width_px, height_px, sha256, has_preview, tag_count) "
        "VALUES ('ESC_1', '제어 도면', 'HCD', 'DW-1', 'UNIT_A_B', 'a.pdf', 'pdf', 1000, 800, '1111aaaa', 1, 0)"
    )
    conn.execute(
        "INSERT INTO sheets(sheet_id, category, subcategory, drawing_no, title, source_file, file_type, "
        "width_px, height_px, sha256, has_preview, tag_count) "
        "VALUES ('ESC_2', '제어 도면', 'HCD', 'DW-2', 'UNIT_AXB', 'b.pdf', 'pdf', 1000, 800, '2222bbbb', 1, 0)"
    )
    conn.commit()
    conn.close()

    (data_dir / "drawings.db-wal").write_bytes(b"stale-wal")
    (data_dir / "drawings.db-shm").write_bytes(b"stale-shm")
    dotslash_tar = _make_tar({"./drawings.db": stage_db.read_bytes()})
    r_ok = c.post(
        "/api/admin/sync",
        headers={"X-Sync-Token": "sync-secret-xyz"},
        files={"bundle": ("dotslash.tar.gz", dotslash_tar, "application/gzip")},
    )
    assert r_ok.status_code == 200 and r_ok.json()["extracted_files"] == 1
    assert not (data_dir / "drawings.db-wal").exists()
    assert not (data_dir / "drawings.db-shm").exists()

    # 읽기 API 호출 후에도 -wal/-shm이 재생성되지 않으며 DELETE 저널 유지
    tok = c.post("/api/auth/login", json={"username": "admin", "password": "testpass123"}).json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}
    st = c.get("/api/stats", headers=hdr)
    assert st.status_code == 200 and st.json()["total_sheets"] == 2
    assert not (data_dir / "drawings.db-wal").exists()
    assert not (data_dir / "drawings.db-shm").exists()
    chk = sqlite3.connect(data_dir / "drawings.db")
    assert chk.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    chk.close()

    # 3) LIKE 와일드카드('_') 이스케이프: 'UNIT_A_B' 검색 시 'UNIT_AXB' 오매칭 차단
    res = c.get("/api/drawings?q=UNIT_A_B", headers=hdr).json()
    assert [it["sheet_id"] for it in res["items"]] == ["ESC_1"]


def test_v131_mass_delete_safety_guard_and_mobile_sidebar_css(tmp_path):
    from pypdf import PdfWriter
    from scripts.build_index import build_index_incremental

    raw_dir = tmp_path / "raw" / "2_AFC_0" / "4_P&ID"
    raw_dir.mkdir(parents=True)
    out_dir = tmp_path / "out"

    pdfs: list[Path] = []
    for i in range(10):
        p = raw_dir / f"sheet_{i:02d}.pdf"
        w = PdfWriter()
        w.add_blank_page(width=400 + i * 10, height=300)
        with open(p, "wb") as f:
            w.write(f)
        pdfs.append(p)

    r1 = build_index_incremental(tmp_path / "raw", out_dir, workers=1, project_name="안전장치테스트")
    assert r1["total_sheets"] == 10
    assert len(list((out_dir / "thumbs").glob("*.webp"))) == 10

    # 10개 중 5개(50% > 30% 상한) 갑작스런 삭제 시 예외로 차단 및 기존 데이터 100% 보존
    for p in pdfs[:5]:
        p.unlink()
    with pytest.raises(RuntimeError, match="대량 삭제 안전장치"):
        build_index_incremental(tmp_path / "raw", out_dir, workers=1, project_name="안전장치테스트")
    assert len(list((out_dir / "thumbs").glob("*.webp"))) == 10

    # 모바일 좌측 사이드바 단일 스크롤 CSS 규칙 존재 확인
    css = (Path(__file__).resolve().parents[1] / "frontend" / "static" / "style.css").read_text(encoding="utf-8")
    mobile_section = css.split("@media (max-width: 900px)")[-1]
    assert "overflow-y: auto;" in mobile_section
    assert "overscroll-behavior: contain;" in mobile_section
    assert "position: sticky;" in mobile_section
    assert "overflow-y: visible;" in mobile_section


def test_v140_hybrid_gate_rotated_split_safe_global_snap_cache_and_row_text(tmp_path):
    from scripts.build_index import (
        _load_ocr_cache,
        _save_ocr_cache,
        normalize_ocr_tail_digits,
        reconstruct_row_text,
        snap_global_ocr_tags,
    )
    from scripts.ocr_engine import (
        _expand_ocr_words,
        merge_digital_and_ocr_words,
        needs_body_ocr,
    )

    # 1) 2중 게이트 본문 전용 OCR 판별: 표제란만 디지털 + 벡터 선분 다수 → True / 단순 디지털 표지 → False
    class _FakeObj:
        def __init__(self, t, bounds=(0, 0, 10, 10)):
            self.type = t
            self._b = bounds

        def get_bounds(self):
            return self._b

    class _FakePage:
        def __init__(self, n_paths=0, img_bounds=None):
            self._objs = [_FakeObj(2) for _ in range(n_paths)]
            if img_bounds:
                self._objs.append(_FakeObj(3, img_bounds))

        def get_size(self):
            return (1000.0, 800.0)

        def get_objects(self):
            return iter(self._objs)

    tb_text = "REV DATE DESCRIPTION DRN CHK APP PM 장태영 방종철 전영민 김성훈 박기철 HOOK-UP DRAWING SF7544-EK0L"
    tb_words = [
        {"text": tok, "x0": 0.60, "y0": 0.80, "x1": 0.70, "y1": 0.82}
        for tok in tb_text.split()
    ]
    assert needs_body_ocr(_FakePage(n_paths=570), tb_words, tb_text) is True     # 숨은 SHX 본문 감지
    assert needs_body_ocr(_FakePage(n_paths=2), tb_words, tb_text) is False      # 단순 디지털 표지 스킵
    assert needs_body_ocr(_FakePage(n_paths=5, img_bounds=(50, 50, 850, 650)), tb_words, tb_text) is True  # 대형 스캔 이미지 감지
    body_words = tb_words + [
        {"text": f"TAGVAL{i:02d}", "x0": 0.1 + i * 0.03, "y0": 0.2, "x1": 0.12 + i * 0.03, "y1": 0.22}
        for i in range(14)
    ]
    assert needs_body_ocr(_FakePage(n_paths=900), body_words, tb_text) is False  # 디지털 본문 충분

    # 2) 세로 회전 텍스트 Y축 비례 분할 + 디지털/OCR 표제란 중복 제거
    _, rot_words = _expand_ocr_words([{
        "text": "550A-CWR 061213",
        "x0": 0.20, "y0": 0.20, "x1": 0.22, "y1": 0.60,
        "confidence": 0.96, "source": "ocr", "rotated": True,
    }])
    sub_toks = [w for w in rot_words if w["text"] in ("550A-CWR", "061213")]
    assert len(sub_toks) == 2 and sub_toks[0]["y1"] < sub_toks[1]["y1"]

    merged = merge_digital_and_ocr_words(
        [{"text": "SF7544-EK0L", "x0": 0.60, "y0": 0.80, "x1": 0.75, "y1": 0.83, "source": "pdf_text"}],
        [
            {"text": "SF7544-EK0L", "x0": 0.60, "y0": 0.80, "x1": 0.75, "y1": 0.83, "source": "ocr"},
            {"text": "FIT_NK2GC01", "x0": 0.20, "y0": 0.30, "x1": 0.30, "y1": 0.33, "source": "ocr"},
        ],
    )
    assert len(merged) == 2 and {w["text"] for w in merged} == {"SF7544-EK0L", "FIT_NK2GC01"}

    # 3) 말미 숫자 슬롯 정규화 (화학식 CO2 보호 + O1→01 교정)
    assert normalize_ocr_tail_digits("FCV_N2GASWWO2") == "FCV_N2GASWW02"
    assert normalize_ocr_tail_digits("AS_N2GATWO1A") == "AS_N2GATW01A"
    assert normalize_ocr_tail_digits("AT_N2GACO2") == "AT_N2GACO2"

    # 4) 전역 사전 3대 불변 제약 검증 (위험 변조 0건, 실제 시각 혼동 오타만 정밀 교정)
    sheets = [
        {
            "sheet_id": "DIG_1",
            "tags": [
                {"tag": "DPT_N2GABWW01", "kind": "instrument", "func": "DPT", "source": "pdf_text", "confidence": 1.0},
                {"tag": "DPT_N2GCODOR03", "kind": "instrument", "func": "DPT", "source": "pdf_text", "confidence": 1.0},
                {"tag": "CWWP_NK2GB02B", "kind": "equipment", "func": "CWWP", "source": "pdf_text", "confidence": 1.0},
                {"tag": "CWWP_NK2GB02A", "kind": "equipment", "func": "CWWP", "source": "pdf_text", "confidence": 1.0},
                {"tag": "BLB_N2GD040", "kind": "equipment", "func": "BLB", "source": "pdf_text", "confidence": 1.0},
                {"tag": "TIC_N2USHCW32", "kind": "instrument", "func": "TIC", "source": "pdf_text", "confidence": 1.0},
                {"tag": "FT_NK2GIWWFRB1", "kind": "instrument", "func": "FT", "source": "pdf_text", "confidence": 1.0},
                {"tag": "HS_SMP_N2GA0101B", "kind": "instrument", "func": "HS", "source": "pdf_text", "confidence": 1.0},
            ],
        },
        {
            "sheet_id": "OCR_1",
            "tags": [
                # 절대 변조되면 안 되는 5건 (기능문자·동/존·길이·숫자↔숫자)
                {"tag": "DPIT_N2GABWW01", "kind": "instrument", "func": "DPIT", "source": "ocr", "confidence": 0.84, "raw_text": ""},
                {"tag": "DPI_N2GCODOR03", "kind": "instrument", "func": "DPI", "source": "ocr", "confidence": 0.84, "raw_text": ""},
                {"tag": "CWWP_NK2GA02B", "kind": "equipment", "func": "CWWP", "source": "ocr", "confidence": 0.84, "raw_text": ""},
                {"tag": "BLB_N2GD04", "kind": "equipment", "func": "BLB", "source": "ocr", "confidence": 0.84, "raw_text": ""},
                {"tag": "TIC_N2USHCW31", "kind": "instrument", "func": "TIC", "source": "ocr", "confidence": 0.84, "raw_text": ""},
                # 반드시 교정되어야 하는 3건 (시각 혼동쌍 + O1→01)
                {"tag": "CWNP_NK2GB02A", "kind": "equipment", "func": "CWNP", "source": "ocr", "confidence": 0.86, "raw_text": ""},
                {"tag": "FT_MK2GIWWFRB1", "kind": "instrument", "func": "FT", "source": "ocr", "confidence": 0.82, "raw_text": ""},
                {"tag": "HS_SMP_NUGAO101B", "kind": "instrument", "func": "HS", "source": "ocr", "confidence": 0.65, "raw_text": ""},
            ],
        },
    ]
    fixed = snap_global_ocr_tags(sheets)
    ocr_out = [t["tag"] for t in sheets[1]["tags"]]
    assert fixed >= 3
    assert ocr_out[:5] == [
        "DPIT_N2GABWW01", "DPI_N2GCODOR03", "CWWP_NK2GA02B", "BLB_N2GD04", "TIC_N2USHCW31"
    ]
    assert ocr_out[5:] == ["CWWP_NK2GB02A", "FT_NK2GIWWFRB1", "HS_SMP_N2GA0101B"]

    # 5) 좌표 기반 Y-행 클러스터링 표 문맥 복원 + OCR JSON 캐시 라운드트립
    tbl_words = [
        {"text": "Tag", "x0": 0.10, "y0": 0.10, "x1": 0.14, "y1": 0.12},
        {"text": "TE-101", "x0": 0.10, "y0": 0.15, "x1": 0.16, "y1": 0.17},
        {"text": "Range", "x0": 0.40, "y0": 0.101, "x1": 0.46, "y1": 0.121},
        {"text": "0~100C", "x0": 0.40, "y0": 0.151, "x1": 0.47, "y1": 0.171},
        {"text": "Spec", "x0": 0.25, "y0": 0.10, "x1": 0.30, "y1": 0.12},
        {"text": "PT100", "x0": 0.25, "y0": 0.15, "x1": 0.31, "y1": 0.17},
    ]
    row_txt = reconstruct_row_text(tbl_words)
    assert row_txt == "Tag | Spec | Range\nTE-101 | PT100 | 0~100C"

    cdir = tmp_path / "ocr_cache"
    _save_ocr_cache(cdir, "deadbeef1234567890", 3, "HELLO", [{"text": "HELLO", "x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2}])
    loaded = _load_ocr_cache(cdir, "deadbeef1234567890", 3)
    assert loaded is not None and loaded["text"] == "HELLO" and len(loaded["words"]) == 1
