"""SQLite 영속 저장소: 도면 FTS5 인덱스 DB + 인증/세션/감사로그 DB 분리 관리.

도면 인덱스(`drawings.db`)는 로컬 PC에서 재생성 후 원자적 교체(sync)되며,
사용자 계정·영구 로그인 세션·감사 로그·즐겨찾기는 `app_state.db`에 분리되어
도면 리비전 갱신 시에도 절대 유실되지 않는다.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from backend.config import (
    KST,
    get_admin_password,
    get_admin_user,
    get_auth_db_path,
    get_db_path,
    now_kst,
    now_kst_iso,
)

MAX_AUDIT_LOGS = 5000
PBKDF2_ITERS = 120_000
SESSION_TTL_SECONDS = 60 * 60 * 24 * 90  # 90일 슬라이딩 만료
SESSION_TOUCH_INTERVAL_S = 300           # 5분 디바운싱 (타일 요청 쓰기 I/O 99% 절감)
_REV_DIR_RE = re.compile(r"(?:AFC|AFD|IFC|REV|R\d|^\d+_P&ID)", re.IGNORECASE)


def extract_revision(source_file: str) -> str:
    """상대 경로의 최상위 리비전 폴더명(예: 2_AFC_0, 1_AFD_2026.09.11, 0_P&ID) 추출."""
    norm = (source_file or "").replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p]
    if len(parts) >= 2 and _REV_DIR_RE.search(parts[0]):
        return parts[0]
    return ""


def _parse_kst_iso(ts: str) -> datetime | None:
    try:
        clean = (ts or "").replace(" KST", "").strip()
        return datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except Exception:
        return None


def _tune(conn: sqlite3.Connection, read_only: bool = False) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-16000")      # 16MB 상한 (DS920+ RAM 보호)
    conn.execute("PRAGMA temp_store=MEMORY")
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")


@contextmanager
def state_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(get_auth_db_path(), timeout=10)
    _tune(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def drawings_conn() -> Iterator[sqlite3.Connection]:
    path = get_db_path()
    if not path.is_file():
        # 아직 첫 동기화 전: 빈 스키마만 메모리가 아닌 디스크에 초기화
        init_drawings_db(path)
    conn = sqlite3.connect(path, timeout=10)
    _tune(conn)
    try:
        yield conn
    finally:
        conn.close()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters_s))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


def init_drawings_db(path=None) -> None:
    target = path or get_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    _tune(conn)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sheets (
                sheet_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                subcategory TEXT NOT NULL,
                drawing_no TEXT,
                title TEXT NOT NULL,
                source_file TEXT NOT NULL,
                file_type TEXT NOT NULL,
                page_no INTEGER NOT NULL DEFAULT 1,
                total_pages INTEGER NOT NULL DEFAULT 1,
                width_px INTEGER NOT NULL DEFAULT 0,
                height_px INTEGER NOT NULL DEFAULT 0,
                tile_cols INTEGER NOT NULL DEFAULT 0,
                tile_rows INTEGER NOT NULL DEFAULT 0,
                tile_size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL,
                has_pdf INTEGER NOT NULL DEFAULT 0,
                has_preview INTEGER NOT NULL DEFAULT 0,
                tag_count INTEGER NOT NULL DEFAULT 0,
                extra_categories TEXT NOT NULL DEFAULT '',
                revision TEXT NOT NULL DEFAULT '',
                full_text TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_sheets_cat ON sheets(category, subcategory);
            CREATE INDEX IF NOT EXISTS idx_sheets_cat_order ON sheets(category, subcategory, title, page_no);
            CREATE INDEX IF NOT EXISTS idx_sheets_source ON sheets(source_file, page_no);
            CREATE INDEX IF NOT EXISTS idx_sheets_rev ON sheets(revision);
            CREATE INDEX IF NOT EXISTS idx_sheets_dwgno ON sheets(drawing_no);

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                kind TEXT NOT NULL,
                func TEXT,
                unit TEXT,
                loop TEXT,
                x0 REAL NOT NULL,
                y0 REAL NOT NULL,
                x1 REAL NOT NULL,
                y1 REAL NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                source TEXT NOT NULL DEFAULT 'pdf_text',
                raw_text TEXT,
                FOREIGN KEY(sheet_id) REFERENCES sheets(sheet_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
            CREATE INDEX IF NOT EXISTS idx_tags_tag_nocase ON tags(tag COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_tags_sheet ON tags(sheet_id);
            CREATE INDEX IF NOT EXISTS idx_tags_func ON tags(func);

            CREATE VIRTUAL TABLE IF NOT EXISTS sheets_fts USING fts5(
                sheet_id UNINDEXED,
                drawing_no,
                title,
                category,
                subcategory,
                tags_joined,
                full_text,
                tokenize='unicode61 remove_diacritics 0'
            );
        """)
        ensure_drawings_migrations(conn)
        conn.commit()
    finally:
        conn.close()


def ensure_drawings_migrations(conn: sqlite3.Connection) -> None:
    """기존 drawings.db에도 v1.2.0 복합 인덱스·revision 컬럼을 무중단 보강."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sheets)").fetchall()}
    if "revision" not in cols:
        conn.execute("ALTER TABLE sheets ADD COLUMN revision TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sheets_cat_order ON sheets(category, subcategory, title, page_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sheets_source ON sheets(source_file, page_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sheets_rev ON sheets(revision)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_tag_nocase ON tags(tag COLLATE NOCASE)")
    # 기존 행 중 revision이 비어 있는 다중 리비전 경로 자동 채움
    rows = conn.execute("SELECT sheet_id, source_file FROM sheets WHERE revision = ''").fetchall()
    updates = [(rev, r[0]) for r in rows if (rev := extract_revision(r[1]))]
    if updates:
        conn.executemany("UPDATE sheets SET revision = ? WHERE sheet_id = ?", updates)


def init_state_db() -> None:
    with state_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'engineer',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                user_agent TEXT,
                ip TEXT,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS favorites (
                username TEXT NOT NULL,
                sheet_id TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (username, sheet_id)
            );

            CREATE TABLE IF NOT EXISTS sheet_pins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet_id TEXT NOT NULL,
                username TEXT NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pins_sheet ON sheet_pins(sheet_id);

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at_kst TEXT NOT NULL,
                username TEXT,
                action TEXT NOT NULL,
                target TEXT,
                ip TEXT,
                user_agent TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_id ON audit_logs(id DESC);
        """)
        admin_user = get_admin_user()
        admin_pw = get_admin_password()
        if admin_pw:
            row = conn.execute(
                "SELECT username, password_hash, role FROM users WHERE username = ?",
                (admin_user,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                    (admin_user, hash_password(admin_pw), now_kst_iso()),
                )
            elif row["role"] != "admin" or not verify_password(admin_pw, row["password_hash"]):
                conn.execute(
                    "UPDATE users SET password_hash = ?, role = 'admin' WHERE username = ?",
                    (hash_password(admin_pw), admin_user),
                )


def record_audit(username: str | None, action: str, target: str = "",
                 ip: str = "", user_agent: str = "", detail: str = "") -> None:
    with state_conn() as conn:
        conn.execute(
            "INSERT INTO audit_logs(at_kst, username, action, target, ip, user_agent, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_kst_iso(), username, action, target, ip, (user_agent or "")[:200], (detail or "")[:400]),
        )
        # 최대 건수 자동 로테이션
        conn.execute(
            "DELETE FROM audit_logs WHERE id <= (SELECT id FROM audit_logs ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (MAX_AUDIT_LOGS,),
        )


def create_session(username: str, role: str, ip: str, user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    now = now_kst_iso()
    with state_conn() as conn:
        conn.execute(
            "INSERT INTO sessions(token, username, role, user_agent, ip, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, username, role, (user_agent or "")[:200], ip, now, now),
        )
    return token


def get_session(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    with state_conn() as conn:
        row = conn.execute(
            "SELECT token, username, role, created_at, last_used_at FROM sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        now = now_kst()
        last_dt = _parse_kst_iso(row["last_used_at"])
        if last_dt is not None:
            age_s = (now - last_dt).total_seconds()
            if age_s > SESSION_TTL_SECONDS:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                return None
            # 5분 이내 재조회는 쓰기 트랜잭션 생략 (타일/썸네일 버스트 시 SQLite 쓰기 99% 절감)
            if age_s >= SESSION_TOUCH_INTERVAL_S:
                conn.execute("UPDATE sessions SET last_used_at = ? WHERE token = ?", (now_kst_iso(), token))
        else:
            conn.execute("UPDATE sessions SET last_used_at = ? WHERE token = ?", (now_kst_iso(), token))
        return dict(row)


def delete_session(token: str) -> None:
    if not token:
        return
    with state_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
