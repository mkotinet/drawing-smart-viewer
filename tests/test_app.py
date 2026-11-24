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
