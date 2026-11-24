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
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
    monkeypatch.setenv("SYNC_TOKEN", "sync-secret-xyz")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    init_state_db()
    init_drawings_db()
    with TestClient(app) as c:
        yield c, tmp_path


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
