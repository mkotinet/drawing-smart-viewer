"""도면 스마트 뷰어 — 초경량 FastAPI PWA 백엔드."""
from __future__ import annotations

import hmac
import io
import json
import re
import shutil
import sqlite3
import tarfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import (
    FRONTEND_DIR,
    STATIC_DIR,
    VERSION_FILE,
    get_admin_user,
    get_data_dir,
    get_db_path,
    get_sync_token,
    get_worker_url,
    is_cookie_secure,
    kst_date_str,
    now_kst_iso,
)
from backend.db import (
    create_session,
    delete_session,
    drawings_conn,
    ensure_drawings_migrations,
    get_session,
    hash_password,
    init_drawings_db,
    init_state_db,
    record_audit,
    state_conn,
    verify_password,
)

SESSION_COOKIE = "dsv_session"
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
MAX_SYNC_UPLOAD_BYTES = 64 * 1024 * 1024     # 단일 청크 최대 64MB
MAX_SYNC_EXTRACT_BYTES = 192 * 1024 * 1024   # 압축 해제 누적 최대 192MB (Tar Bomb 방어)
LOGIN_RATE_LIMIT_MAX = 10
LOGIN_RATE_LIMIT_WINDOW_S = 900              # 15분당 IP별 실패 10회 상한
_login_fail_lock = threading.Lock()
_login_fails: dict[str, deque[float]] = {}


def _check_login_rate_limit(ip: str) -> bool:
    """허용 시 True, 차단 시 False."""
    now = time.monotonic()
    cutoff = now - LOGIN_RATE_LIMIT_WINDOW_S
    with _login_fail_lock:
        q = _login_fails.get(ip)
        if not q:
            return True
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            _login_fails.pop(ip, None)
            return True
        return len(q) < LOGIN_RATE_LIMIT_MAX


def _record_login_failure(ip: str) -> None:
    now = time.monotonic()
    cutoff = now - LOGIN_RATE_LIMIT_WINDOW_S
    with _login_fail_lock:
        q = _login_fails.setdefault(ip, deque())
        while q and q[0] < cutoff:
            q.popleft()
        q.append(now)


def _clear_login_failures(ip: str) -> None:
    with _login_fail_lock:
        _login_fails.pop(ip, None)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_state_db()
    if not get_db_path().is_file():
        init_drawings_db()
    else:
        with drawings_conn() as conn:
            ensure_drawings_migrations(conn)
            conn.commit()
    yield


app = FastAPI(title="도면 스마트 뷰어", version="1.2.0", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    return resp


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _extract_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(SESSION_COOKIE, "").strip()


def require_user(request: Request) -> dict[str, Any]:
    token = _extract_token(request)
    sess = get_session(token)
    if sess is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return sess


def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")
    return user


def _validate_sheet_id(sheet_id: str) -> str:
    if not SAFE_ID_RE.match(sheet_id) or ".." in sheet_id:
        raise HTTPException(status_code=400, detail="유효하지 않은 시트 ID입니다")
    return sheet_id


def _fts_query(raw: str) -> str:
    tokens = [re.sub(r'["\'\*\(\):^]', "", tok).strip() for tok in raw.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return ""
    return " AND ".join(f'"{t}"*' for t in tokens)


# ── PWA 루트 & 헬스체크 ─────────────────────────────────────────────────────

@app.get("/api/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "kst": now_kst_iso(), "date": kst_date_str()}


@app.get("/api/version")
def get_version() -> JSONResponse:
    data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    data["server_kst"] = now_kst_iso()
    data["server_date"] = kst_date_str()
    return JSONResponse(
        content=data,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.api_route("/manifest.json", methods=["GET", "HEAD"])
def serve_manifest() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.json",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.api_route("/sw.js", methods=["GET", "HEAD"])
def serve_sw() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
        },
    )


# ── 인증 & 세션 (모바일 영구 로그인 + 감사 로깅) ─────────────────────────────

class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserCreateBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=4, max_length=128)
    role: str = Field(default="engineer", pattern="^(admin|engineer)$")


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    username = body.username.strip()
    if not _check_login_rate_limit(ip):
        record_audit(username, "login_rate_limited", ip=ip, user_agent=ua)
        raise HTTPException(status_code=429, detail="로그인 시도가 너무 많습니다. 15분 후 다시 시도해 주세요")

    with state_conn() as conn:
        row = conn.execute(
            "SELECT username, password_hash, role FROM users WHERE username = ?", (username,)
        ).fetchone()
    if row is None or not verify_password(body.password, row["password_hash"]):
        _record_login_failure(ip)
        record_audit(username, "login_failed", ip=ip, user_agent=ua)
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 일치하지 않습니다")

    _clear_login_failures(ip)
    token = create_session(row["username"], row["role"], ip, ua)
    record_audit(row["username"], "login_success", ip=ip, user_agent=ua)
    is_https = (
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"
        or request.url.scheme == "https"
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=is_https and is_cookie_secure(),
        max_age=60 * 60 * 24 * 90,
        path="/",
    )
    return {
        "token": token,
        "username": row["username"],
        "role": row["role"],
        "server_date": kst_date_str(),
    }


@app.get("/api/auth/me")
def auth_me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {
        "username": user["username"],
        "role": user["role"],
        "is_admin": user["role"] == "admin",
        "server_date": kst_date_str(),
        "server_kst": now_kst_iso(),
    }


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    token = _extract_token(request)
    sess = get_session(token)
    if sess:
        delete_session(token)
        record_audit(sess["username"], "logout", ip=_client_ip(request),
                     user_agent=request.headers.get("user-agent", ""))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


# ── 도면 · 카테고리 · 태그 통합 검색 API ────────────────────────────────────

@app.get("/api/stats")
def get_stats(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    with drawings_conn() as conn:
        total_sheets = conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0]
        total_tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        unique_tags = conn.execute("SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0]
        meta_rows = conn.execute("SELECT key, value FROM meta").fetchall()
        meta = {r["key"]: r["value"] for r in meta_rows}
        cats = conn.execute(
            "SELECT category, subcategory, COUNT(*) AS cnt, SUM(tag_count) AS tags "
            "FROM sheets GROUP BY category, subcategory ORDER BY category, subcategory"
        ).fetchall()
        revs = conn.execute(
            "SELECT revision, COUNT(*) AS cnt FROM sheets "
            "WHERE revision != '' GROUP BY revision ORDER BY revision DESC"
        ).fetchall()
    with state_conn() as sconn:
        fav_cnt = sconn.execute(
            "SELECT COUNT(*) FROM favorites WHERE username = ?", (user["username"],)
        ).fetchone()[0]

    grouped: dict[str, dict[str, Any]] = {}
    for r in cats:
        cat = grouped.setdefault(r["category"], {"category": r["category"], "count": 0, "tags": 0, "subcategories": []})
        cat["count"] += r["cnt"]
        cat["tags"] += r["tags"] or 0
        cat["subcategories"].append({
            "name": r["subcategory"],
            "count": r["cnt"],
            "tags": r["tags"] or 0,
        })
    return {
        "total_sheets": total_sheets,
        "total_tags": total_tags,
        "unique_tags": unique_tags,
        "favorites_count": fav_cnt,
        "meta": meta,
        "categories": list(grouped.values()),
        "revisions": [{"name": r["revision"], "count": r["cnt"]} for r in revs],
    }


@app.get("/api/drawings")
def list_drawings(
    q: str = Query(default="", max_length=120),
    revision: str = Query(default="", max_length=120),
    category: str = Query(default="", max_length=80),
    subcategory: str = Query(default="", max_length=120),
    favorites_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=40, ge=1, le=120),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    with state_conn() as sconn:
        fav_ids = {
            r["sheet_id"]
            for r in sconn.execute("SELECT sheet_id FROM favorites WHERE username = ?", (user["username"],)).fetchall()
        }

    q_clean = q.strip()
    offset = (page - 1) * limit
    where: list[str] = []
    params: list[Any] = []

    if revision:
        where.append("s.revision = ?")
        params.append(revision)
    if category:
        where.append("s.category = ?")
        params.append(category)
    if subcategory:
        where.append("s.subcategory = ?")
        params.append(subcategory)
    if favorites_only:
        if not fav_ids:
            return {"total": 0, "page": page, "limit": limit, "items": []}
        placeholders = ",".join("?" for _ in fav_ids)
        where.append(f"s.sheet_id IN ({placeholders})")
        params.extend(sorted(fav_ids))

    snippets_map: dict[str, str] = {}
    with drawings_conn() as conn:
        if q_clean:
            fts = _fts_query(q_clean)
            like = f"%{q_clean}%"
            where_sql = (" AND " + " AND ".join(where)) if where else ""
            try:
                total = conn.execute(
                    f"""
                    SELECT COUNT(*) FROM sheets s
                    WHERE (s.sheet_id IN (SELECT sheet_id FROM sheets_fts WHERE sheets_fts MATCH ?)
                           OR s.title LIKE ? OR s.drawing_no LIKE ?)
                    {where_sql}
                    """,
                    [fts, like, like, *params],
                ).fetchone()[0]
                rows = conn.execute(
                    f"""
                    SELECT s.sheet_id, s.category, s.subcategory, s.revision, s.drawing_no, s.title,
                           s.file_type, s.page_no, s.total_pages, s.width_px, s.height_px,
                           s.tag_count, s.has_pdf, s.has_preview, s.sha256
                    FROM sheets s
                    WHERE (s.sheet_id IN (SELECT sheet_id FROM sheets_fts WHERE sheets_fts MATCH ?)
                           OR s.title LIKE ? OR s.drawing_no LIKE ?)
                    {where_sql}
                    ORDER BY
                        CASE WHEN UPPER(COALESCE(s.drawing_no, '')) LIKE UPPER(?) THEN 0
                             WHEN UPPER(s.title) LIKE UPPER(?) THEN 1 ELSE 2 END,
                        s.category, s.title, s.page_no
                    LIMIT ? OFFSET ?
                    """,
                    [fts, like, like, *params, like, like, limit, offset],
                ).fetchall()
                if rows and fts:
                    sids = [r["sheet_id"] for r in rows]
                    ph = ",".join("?" for _ in sids)
                    for sr in conn.execute(
                        f"SELECT sheet_id, snippet(sheets_fts, 6, '[[H]]', '[[/H]]', '…', 14) AS snip "
                        f"FROM sheets_fts WHERE sheets_fts MATCH ? AND sheet_id IN ({ph})",
                        [fts, *sids],
                    ).fetchall():
                        if sr["snip"]:
                            snippets_map[sr["sheet_id"]] = sr["snip"].replace("\r", " ").replace("\n", " ")
            except sqlite3.OperationalError:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM sheets s WHERE (s.title LIKE ? OR s.drawing_no LIKE ? OR s.full_text LIKE ?){where_sql}",
                    [like, like, like, *params],
                ).fetchone()[0]
                rows = conn.execute(
                    f"""
                    SELECT s.sheet_id, s.category, s.subcategory, s.revision, s.drawing_no, s.title,
                           s.file_type, s.page_no, s.total_pages, s.width_px, s.height_px,
                           s.tag_count, s.has_pdf, s.has_preview, s.sha256
                    FROM sheets s
                    WHERE (s.title LIKE ? OR s.drawing_no LIKE ? OR s.full_text LIKE ?){where_sql}
                    ORDER BY s.category, s.title, s.page_no
                    LIMIT ? OFFSET ?
                    """,
                    [like, like, like, *params, limit, offset],
                ).fetchall()
        else:
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute(f"SELECT COUNT(*) FROM sheets s {where_sql}", params).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT s.sheet_id, s.category, s.subcategory, s.revision, s.drawing_no, s.title,
                       s.file_type, s.page_no, s.total_pages, s.width_px, s.height_px,
                       s.tag_count, s.has_pdf, s.has_preview, s.sha256
                FROM sheets s
                {where_sql}
                ORDER BY s.category, s.subcategory, s.title, s.page_no
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        d["is_favorite"] = d["sheet_id"] in fav_ids
        d["v"] = d["sha256"][:8]
        if d["sheet_id"] in snippets_map:
            d["snippet"] = snippets_map[d["sheet_id"]]
        items.append(d)
    return {"total": total, "page": page, "limit": limit, "items": items}


@app.get("/api/drawings/{sheet_id}")
def get_drawing_detail(sheet_id: str, request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    _validate_sheet_id(sheet_id)
    with drawings_conn() as conn:
        row = conn.execute("SELECT * FROM sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="도면을 찾을 수 없습니다")
        sheet = dict(row)
        tag_rows = conn.execute(
            "SELECT id, tag, kind, func, unit, loop, x0, y0, x1, y1, confidence, source, raw_text "
            "FROM tags WHERE sheet_id = ? ORDER BY tag, y0",
            (sheet_id,),
        ).fetchall()
        tags = [dict(t) for t in tag_rows]

        # 각 태그가 다른 도면(P&ID, 결선도, 배선도, 데이터시트 등)에 몇 번 등장하는지 교차 카운트
        tag_names = sorted({t["tag"] for t in tags})
        cross_counts: dict[str, int] = {}
        if tag_names:
            placeholders = ",".join("?" for _ in tag_names)
            for cr in conn.execute(
                f"SELECT tag, COUNT(DISTINCT sheet_id) AS cnt FROM tags "
                f"WHERE tag IN ({placeholders}) GROUP BY tag",
                tag_names,
            ).fetchall():
                cross_counts[cr["tag"]] = cr["cnt"]

        for t in tags:
            t["sheet_count"] = cross_counts.get(t["tag"], 1)

        # 같은 원본 PDF의 형제 페이지 정보 (P&ID 멀티페이지 이동용)
        siblings = [
            dict(r)
            for r in conn.execute(
                "SELECT sheet_id, page_no, drawing_no, title, tag_count FROM sheets "
                "WHERE source_file = ? ORDER BY page_no",
                (sheet["source_file"],),
            ).fetchall()
        ]

    with state_conn() as sconn:
        fav = sconn.execute(
            "SELECT 1 FROM favorites WHERE username = ? AND sheet_id = ?",
            (user["username"], sheet_id),
        ).fetchone() is not None
        pins = [
            dict(r)
            for r in sconn.execute(
                "SELECT id, sheet_id, username, x, y, note, created_at "
                "FROM sheet_pins WHERE sheet_id = ? ORDER BY id ASC",
                (sheet_id,),
            ).fetchall()
        ]

    record_audit(user["username"], "view_sheet", target=sheet_id,
                 ip=_client_ip(request), user_agent=request.headers.get("user-agent", ""))
    sheet.pop("full_text", None)
    sheet["v"] = sheet["sha256"][:8]
    sheet["is_favorite"] = fav
    sheet["tags"] = tags
    sheet["siblings"] = siblings
    sheet["pins"] = pins
    return sheet


@app.get("/api/tags")
def search_tags(
    q: str = Query(default="", max_length=100),
    kind: str = Query(default="", max_length=40),
    func: str = Query(default="", max_length=16),
    limit: int = Query(default=60, ge=1, le=200),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if q.strip():
        where.append("(UPPER(t.tag) LIKE UPPER(?) OR UPPER(COALESCE(t.raw_text, '')) LIKE UPPER(?))")
        like = f"%{q.strip()}%"
        params.extend([like, like])
    if kind:
        where.append("t.kind = ?")
        params.append(kind)
    if func:
        where.append("UPPER(t.func) = UPPER(?)")
        params.append(func)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with drawings_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT t.tag, t.kind, t.func, COUNT(DISTINCT t.sheet_id) AS sheet_count,
                   COUNT(*) AS occurrence_count,
                   GROUP_CONCAT(DISTINCT s.category) AS categories
            FROM tags t
            JOIN sheets s ON s.sheet_id = t.sheet_id
            {where_sql}
            GROUP BY t.tag, t.kind, t.func
            ORDER BY sheet_count DESC, t.tag
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        funcs = [
            {"func": r["func"], "count": r["cnt"]}
            for r in conn.execute(
                "SELECT func, COUNT(DISTINCT tag) AS cnt FROM tags WHERE func IS NOT NULL "
                "GROUP BY func ORDER BY cnt DESC LIMIT 30"
            ).fetchall()
        ]
    return {"items": [dict(r) for r in rows], "top_funcs": funcs}


@app.get("/api/tags/{tag_name}/sheets")
def tag_cross_references(tag_name: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    with drawings_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.tag, t.kind, t.func, t.x0, t.y0, t.x1, t.y1, t.confidence, t.source,
                   s.sheet_id, s.category, s.subcategory, s.drawing_no, s.title, s.page_no, s.sha256
            FROM tags t
            JOIN sheets s ON s.sheet_id = t.sheet_id
            WHERE t.tag = ? COLLATE NOCASE
            ORDER BY s.category, s.title, s.page_no
            """,
            (tag_name.strip(),),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["v"] = d["sha256"][:8]
        items.append(d)
    return {"tag": tag_name, "count": len(items), "occurrences": items}


# ── 즐겨찾기 ────────────────────────────────────────────────────────────────

@app.post("/api/favorites/{sheet_id}")
def toggle_favorite(sheet_id: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    _validate_sheet_id(sheet_id)
    with state_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM favorites WHERE username = ? AND sheet_id = ?",
            (user["username"], sheet_id),
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM favorites WHERE username = ? AND sheet_id = ?",
                         (user["username"], sheet_id))
            return {"sheet_id": sheet_id, "is_favorite": False}
        conn.execute(
            "INSERT INTO favorites(username, sheet_id, created_at) VALUES (?, ?, ?)",
            (user["username"], sheet_id, now_kst_iso()),
        )
        return {"sheet_id": sheet_id, "is_favorite": True}


# ── 현장 점검 메모 핀 (app_state.db 영속 저장) ────────────────────────────────

class PinCreateBody(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    note: str = Field(min_length=1, max_length=300)


@app.get("/api/sheets/{sheet_id}/pins")
def list_sheet_pins(sheet_id: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    _validate_sheet_id(sheet_id)
    with state_conn() as conn:
        rows = conn.execute(
            "SELECT id, sheet_id, username, x, y, note, created_at "
            "FROM sheet_pins WHERE sheet_id = ? ORDER BY id ASC",
            (sheet_id,),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/sheets/{sheet_id}/pins")
def create_sheet_pin(
    sheet_id: str,
    body: PinCreateBody,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    _validate_sheet_id(sheet_id)
    note = body.note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="메모 내용을 입력해 주세요")
    now = now_kst_iso()
    with state_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sheet_pins(sheet_id, username, x, y, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sheet_id, user["username"], round(body.x, 5), round(body.y, 5), note, now),
        )
        pin_id = cur.lastrowid
    record_audit(user["username"], "create_pin", target=sheet_id, ip=_client_ip(request), detail=note[:120])
    return {
        "id": pin_id,
        "sheet_id": sheet_id,
        "username": user["username"],
        "x": round(body.x, 5),
        "y": round(body.y, 5),
        "note": note,
        "created_at": now,
    }


@app.delete("/api/pins/{pin_id}")
def delete_sheet_pin(
    pin_id: int,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    with state_conn() as conn:
        row = conn.execute("SELECT id, sheet_id, username FROM sheet_pins WHERE id = ?", (pin_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="메모 핀을 찾을 수 없습니다")
        if row["username"] != user["username"] and user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="본인이 작성한 메모 핀만 삭제할 수 있습니다")
        conn.execute("DELETE FROM sheet_pins WHERE id = ?", (pin_id,))
    record_audit(user["username"], "delete_pin", target=f"{row['sheet_id']}#{pin_id}", ip=_client_ip(request))
    return {"status": "ok", "deleted_id": pin_id}


# ── 도면 정적 리소스 서빙 (타일 · 프리뷰 · 썸네일 · 단일 페이지 PDF) ────────

IMMUTABLE_HEADERS = {"Cache-Control": "private, max-age=31536000, immutable"}


@app.get("/api/sheets/{sheet_id}/thumb.webp")
def get_thumb(sheet_id: str, user: dict[str, Any] = Depends(require_user)) -> FileResponse:
    _validate_sheet_id(sheet_id)
    p = get_data_dir() / "thumbs" / f"{sheet_id}.webp"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="썸네일이 없습니다")
    return FileResponse(p, media_type="image/webp", headers=IMMUTABLE_HEADERS)


@app.get("/api/sheets/{sheet_id}/preview.webp")
def get_preview(sheet_id: str, user: dict[str, Any] = Depends(require_user)) -> FileResponse:
    _validate_sheet_id(sheet_id)
    p = get_data_dir() / "tiles" / sheet_id / "preview.webp"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="미리보기 이미지가 없습니다")
    return FileResponse(p, media_type="image/webp", headers=IMMUTABLE_HEADERS)


@app.get("/api/sheets/{sheet_id}/tile/{col}_{row}.webp")
def get_tile(sheet_id: str, col: int, row: int, user: dict[str, Any] = Depends(require_user)) -> FileResponse:
    _validate_sheet_id(sheet_id)
    if not (0 <= col <= 64 and 0 <= row <= 64):
        raise HTTPException(status_code=400, detail="잘못된 타일 좌표입니다")
    p = get_data_dir() / "tiles" / sheet_id / f"{col}_{row}.webp"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="타일이 없습니다")
    return FileResponse(p, media_type="image/webp", headers=IMMUTABLE_HEADERS)


@app.get("/api/sheets/{sheet_id}/pdf")
def get_sheet_pdf(sheet_id: str, request: Request, user: dict[str, Any] = Depends(require_user)) -> FileResponse:
    _validate_sheet_id(sheet_id)
    p = get_data_dir() / "pdfs" / f"{sheet_id}.pdf"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="원본 PDF가 없습니다")
    record_audit(user["username"], "download_pdf", target=sheet_id,
                 ip=_client_ip(request), user_agent=request.headers.get("user-agent", ""))
    return FileResponse(
        p,
        media_type="application/pdf",
        filename=f"{sheet_id}.pdf",
        headers={"Cache-Control": "private, max-age=86400"},
    )


# ── 관리자 전용 API (로컬 PC 동기화 업로드 · 감사 로그 · 계정 관리) ─────────

def _authorize_sync(request: Request) -> str:
    sync_tok = get_sync_token()
    hdr = request.headers.get("x-sync-token", "").strip()
    if sync_tok and hdr and hmac.compare_digest(hdr, sync_tok):
        return "sync-cli"
    sess = get_session(_extract_token(request))
    if sess and sess.get("role") == "admin":
        return sess["username"]
    raise HTTPException(status_code=401, detail="동기화 인증이 필요합니다")


@app.post("/api/admin/sync")
async def upload_sync_bundle(
    request: Request,
    bundle: UploadFile = File(...),
    replace_db: bool = Query(default=False),
) -> dict[str, Any]:
    """로컬 PC에서 생성한 tar.gz 번들(drawings.db, tiles/, thumbs/, pdfs/)을 안전하게 반영."""
    actor = _authorize_sync(request)
    data_dir = get_data_dir().resolve()
    allowed_roots = {"drawings.db", "tiles", "thumbs", "pdfs"}
    extracted = 0

    raw = await bundle.read(MAX_SYNC_UPLOAD_BYTES + 1)
    if len(raw) > MAX_SYNC_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="동기화 번들 크기가 64MB 상한을 초과했습니다")
    extracted_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            for member in tf.getmembers():
                if not (member.isfile() or member.isdir()):
                    raise HTTPException(status_code=400, detail=f"허용되지 않는 아카이브 항목: {member.name}")
                if member.size < 0 or extracted_bytes + member.size > MAX_SYNC_EXTRACT_BYTES:
                    raise HTTPException(status_code=413, detail="아카이브 압축 해제 총량이 192MB 상한을 초과했습니다")
                rel = Path(member.name)
                if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                    raise HTTPException(status_code=400, detail=f"경로 조작 차단: {member.name}")
                if rel.parts[0] not in allowed_roots:
                    raise HTTPException(status_code=400, detail=f"허용되지 않은 최상위 경로: {rel.parts[0]}")

                dest = (data_dir / rel).resolve()
                if data_dir not in dest.parents and dest != data_dir:
                    raise HTTPException(status_code=400, detail=f"경로 이탈 차단: {member.name}")

                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue

                dest.parent.mkdir(parents=True, exist_ok=True)
                fobj = tf.extractfile(member)
                if fobj is None:
                    continue
                if member.name == "drawings.db":
                    tmp_db = data_dir / ".drawings.db.incoming"
                    with open(tmp_db, "wb") as out:
                        shutil.copyfileobj(fobj, out)
                    # 기존 WAL/SHM 정리 후 원자적 교체 + v1.2.0 인덱스 보장
                    for suffix in ("-wal", "-shm"):
                        (data_dir / f"drawings.db{suffix}").unlink(missing_ok=True)
                    tmp_db.replace(data_dir / "drawings.db")
                    with drawings_conn() as dconn:
                        ensure_drawings_migrations(dconn)
                        dconn.commit()
                else:
                    tmp_f = dest.with_name(f".{dest.name}.tmp")
                    with open(tmp_f, "wb") as out:
                        shutil.copyfileobj(fobj, out)
                    tmp_f.replace(dest)
                extracted_bytes += member.size
                extracted += 1
    except tarfile.TarError as exc:
        raise HTTPException(status_code=400, detail=f"손상된 tar.gz 번들: {exc}") from exc

    record_audit(actor, "admin_sync", target=bundle.filename or "bundle.tar.gz",
                 ip=_client_ip(request), detail=f"extracted={extracted}, bytes={len(raw)}, replace_db={replace_db}")
    return {"status": "ok", "extracted_files": extracted, "bytes": len(raw)}


def _call_worker(path: str, method: str = "GET", timeout: int = 12) -> dict[str, Any]:
    worker_url = get_worker_url()
    if not worker_url:
        raise HTTPException(status_code=503, detail="자동 동기화 워커(SYNC_WORKER_URL)가 설정되지 않았습니다")
    req = urllib.request.Request(
        f"{worker_url}{path}",
        method=method,
        headers={"X-Sync-Token": get_sync_token()},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except Exception:
            detail = body[:300]
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"동기화 워커 연결 실패: {exc}") from exc


@app.get("/api/admin/sync/status")
def get_sync_status(admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    status_file = get_data_dir() / "sync_status.json"
    file_data: dict[str, Any] = {}
    if status_file.is_file():
        try:
            file_data = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception:
            file_data = {}

    worker_url = get_worker_url()
    if worker_url:
        try:
            live = _call_worker("/status", method="GET", timeout=5)
            live["worker_enabled"] = True
            live["worker_online"] = True
            return live
        except HTTPException:
            return {
                **file_data,
                "worker_enabled": True,
                "worker_online": False,
            }
    return {
        **file_data,
        "worker_enabled": False,
        "worker_online": False,
    }


@app.post("/api/admin/sync/trigger")
def trigger_drive_sync(request: Request, admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    res = _call_worker("/trigger", method="POST", timeout=10)
    record_audit(admin["username"], "trigger_drive_sync", target=str(res.get("status", "")),
                 ip=_client_ip(request), user_agent=request.headers.get("user-agent", ""))
    return res


@app.post("/api/admin/sync/test-alert")
def test_n8n_alert(request: Request, admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    res = _call_worker("/test-alert", method="POST", timeout=20)
    record_audit(admin["username"], "test_n8n_alert", target="ok" if res.get("sent") else "failed",
                 ip=_client_ip(request), user_agent=request.headers.get("user-agent", ""))
    return res


@app.get("/api/admin/audit-logs")
def list_audit_logs(limit: int = Query(default=100, ge=1, le=500),
                    admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with state_conn() as conn:
        rows = conn.execute(
            "SELECT id, at_kst, username, action, target, ip, user_agent, detail "
            "FROM audit_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/admin/users")
def list_users(admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with state_conn() as conn:
        rows = conn.execute(
            "SELECT username, role, created_at FROM users ORDER BY role, username"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/admin/users")
def create_user(body: UserCreateBody, request: Request,
                admin: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    uname = body.username.strip()
    with state_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (uname,)).fetchone():
            raise HTTPException(status_code=409, detail="이미 존재하는 아이디입니다")
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (uname, hash_password(body.password), body.role, now_kst_iso()),
        )
    record_audit(admin["username"], "create_user", target=uname, ip=_client_ip(request), detail=body.role)
    return {"status": "ok", "username": uname, "role": body.role}


@app.delete("/api/admin/users/{username}")
def delete_user(username: str, request: Request,
                admin: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    uname = username.strip()
    if uname == admin["username"]:
        raise HTTPException(status_code=400, detail="현재 로그인한 최고관리자 계정은 삭제할 수 없습니다")
    with state_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (uname,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다")
        conn.execute("DELETE FROM sessions WHERE username = ?", (uname,))
    record_audit(admin["username"], "delete_user", target=uname, ip=_client_ip(request))
    return {"status": "ok", "deleted": uname}


# ── 정적 자산 & SPA 엔트리 ──────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_index() -> HTMLResponse:
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache"})
