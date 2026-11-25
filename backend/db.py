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
import threading
import time
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
SESSION_CACHE_TTL_S = 60.0               # 세션 60초 인메모리 캐시 (타일 버스트 SQLite 조회 제거)
MAX_SESSION_CACHE = 512                  # 256MB 컨테이너 메모리 보호 상한
_REV_DIR_RE = re.compile(r"(?:AFC|AFD|IFC|REV|R\d|^\d+_P&ID)", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")

_session_cache_lock = threading.Lock()
_session_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _hash_session_token(token: str) -> str:
    """세션 토큰을 DB 저장·조회용 SHA-256 해시로 변환 (DB 유출 시 탈취 방지)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invalidate_session_cache(token: str | None = None, username: str | None = None) -> None:
    """로그아웃·계정 삭제·테스트 초기화 시 인메모리 세션 캐시 즉시 무효화."""
    with _session_cache_lock:
        if token is None and username is None:
            _session_cache.clear()
            return
        if token is not None:
            _session_cache.pop(_hash_session_token(token), None)
        if username is not None:
            dead = [k for k, (_, sess) in _session_cache.items() if sess.get("username") == username]
            for k in dead:
                _session_cache.pop(k, None)


def canonicalize_tag(tag: str) -> str:
    """구분자(_, -, 공백, *)를 제거한 정규화 대문자 키 (TIC_N2USCW31 == TIC-N2USCW31)."""
    return _NON_ALNUM_RE.sub("", (tag or "").upper())


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


def _tune(conn: sqlite3.Connection, read_only: bool = False, journal_mode: str = "WAL") -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-16000")      # 16MB 상한 (DS920+ RAM 보호)
    conn.execute("PRAGMA temp_store=MEMORY")
    if not read_only:
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")


@contextmanager
def state_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(get_auth_db_path(), timeout=10)
    _tune(conn, read_only=False, journal_mode="WAL")
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
    """도면 인덱스 DB 순수 읽기 전용 커넥션 (DELETE 저널 전제, -wal/-shm 생성 원천 차단)."""
    path = get_db_path()
    if not path.is_file():
        init_drawings_db(path)
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    _tune(conn, read_only=True)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def drawings_write_conn(path=None) -> Iterator[sqlite3.Connection]:
    """기동·동기화 마이그레이션 전용 쓰기 커넥션 (종료 시 DELETE 저널 모드 강제 유지)."""
    target = path or get_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=15)
    _tune(conn, read_only=False, journal_mode="DELETE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        for suffix in ("-wal", "-shm"):
            target.with_name(f"{target.name}{suffix}").unlink(missing_ok=True)


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
    _tune(conn, read_only=False, journal_mode="DELETE")
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
                drawing_id TEXT NOT NULL DEFAULT '',
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
                ocr_status TEXT NOT NULL DEFAULT '',
                full_text TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_sheets_cat ON sheets(category, subcategory);
            CREATE INDEX IF NOT EXISTS idx_sheets_cat_order ON sheets(category, subcategory, title, page_no);
            CREATE INDEX IF NOT EXISTS idx_sheets_source ON sheets(source_file, page_no);
            CREATE INDEX IF NOT EXISTS idx_sheets_rev ON sheets(revision);
            CREATE INDEX IF NOT EXISTS idx_sheets_dwgno ON sheets(drawing_no);
            CREATE INDEX IF NOT EXISTS idx_sheets_dwgid ON sheets(drawing_id);

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                canonical_tag TEXT NOT NULL DEFAULT '',
                loop_key TEXT NOT NULL DEFAULT '',
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
            CREATE INDEX IF NOT EXISTS idx_tags_canonical ON tags(canonical_tag);
            CREATE INDEX IF NOT EXISTS idx_tags_loop_key ON tags(loop_key);
            CREATE INDEX IF NOT EXISTS idx_tags_kind ON tags(kind);
            CREATE INDEX IF NOT EXISTS idx_tags_sheet ON tags(sheet_id);
            CREATE INDEX IF NOT EXISTS idx_tags_func ON tags(func);

            CREATE TABLE IF NOT EXISTS continuations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet_id TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT '',
                fluid TEXT NOT NULL DEFAULT '',
                target_dwg TEXT NOT NULL,
                target_sheet_id TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                x0 REAL NOT NULL,
                y0 REAL NOT NULL,
                x1 REAL NOT NULL,
                y1 REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'pdf_text',
                FOREIGN KEY(sheet_id) REFERENCES sheets(sheet_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_cont_sheet ON continuations(sheet_id);
            CREATE INDEX IF NOT EXISTS idx_cont_target_dwg ON continuations(target_dwg);
            CREATE INDEX IF NOT EXISTS idx_cont_target_sheet ON continuations(target_sheet_id);

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


def _ensure_trigram_table(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS sheets_trigram USING fts5("
            "sheet_id UNINDEXED, search_blob, tokenize='trigram')"
        )
        return True
    except sqlite3.OperationalError:
        return False


def ensure_drawings_migrations(conn: sqlite3.Connection) -> None:
    """기존 drawings.db에도 v1.2/v1.3 인덱스·컬럼·continuations·trigram을 무중단 보강."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sheets)").fetchall()}
    if "revision" not in cols:
        conn.execute("ALTER TABLE sheets ADD COLUMN revision TEXT NOT NULL DEFAULT ''")
    if "drawing_id" not in cols:
        conn.execute("ALTER TABLE sheets ADD COLUMN drawing_id TEXT NOT NULL DEFAULT ''")
    if "ocr_status" not in cols:
        conn.execute("ALTER TABLE sheets ADD COLUMN ocr_status TEXT NOT NULL DEFAULT ''")

    tcols = {r[1] for r in conn.execute("PRAGMA table_info(tags)").fetchall()}
    if "canonical_tag" not in tcols:
        conn.execute("ALTER TABLE tags ADD COLUMN canonical_tag TEXT NOT NULL DEFAULT ''")
    if "loop_key" not in tcols:
        conn.execute("ALTER TABLE tags ADD COLUMN loop_key TEXT NOT NULL DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS continuations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_id TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT '',
            fluid TEXT NOT NULL DEFAULT '',
            target_dwg TEXT NOT NULL,
            target_sheet_id TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            x0 REAL NOT NULL,
            y0 REAL NOT NULL,
            x1 REAL NOT NULL,
            y1 REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'pdf_text',
            FOREIGN KEY(sheet_id) REFERENCES sheets(sheet_id) ON DELETE CASCADE
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_sheets_cat_order ON sheets(category, subcategory, title, page_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sheets_source ON sheets(source_file, page_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sheets_rev ON sheets(revision)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sheets_dwgid ON sheets(drawing_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_tag_nocase ON tags(tag COLLATE NOCASE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_canonical ON tags(canonical_tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_loop_key ON tags(loop_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_kind ON tags(kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cont_sheet ON continuations(sheet_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cont_target_dwg ON continuations(target_dwg)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cont_target_sheet ON continuations(target_sheet_id)")

    # 기존 행 중 revision이 비어 있는 다중 리비전 경로 자동 채움
    rows = conn.execute("SELECT sheet_id, source_file FROM sheets WHERE revision = ''").fetchall()
    updates = [(rev, r[0]) for r in rows if (rev := extract_revision(r[1]))]
    if updates:
        conn.executemany("UPDATE sheets SET revision = ? WHERE sheet_id = ?", updates)

    # 기존 태그의 canonical_tag 미설정분 자동 백필
    trows = conn.execute("SELECT id, tag FROM tags WHERE canonical_tag = ''").fetchall()
    if trows:
        conn.executemany(
            "UPDATE tags SET canonical_tag = ? WHERE id = ?",
            [(canonicalize_tag(r[1]), r[0]) for r in trows],
        )

    # FTS5 trigram 가상 테이블 생성 및 기존 데이터 자동 백필 (부분 문자열 초고속 검색)
    if _ensure_trigram_table(conn):
        tri_cnt = conn.execute("SELECT COUNT(*) FROM sheets_trigram").fetchone()[0]
        fts_cnt = conn.execute("SELECT COUNT(*) FROM sheets_fts").fetchone()[0]
        if tri_cnt == 0 and fts_cnt > 0:
            fts_rows = conn.execute(
                "SELECT f.sheet_id, COALESCE(s.drawing_id, '') AS dwgid, "
                "f.drawing_no, f.title, f.tags_joined, f.full_text "
                "FROM sheets_fts f LEFT JOIN sheets s ON s.sheet_id = f.sheet_id"
            ).fetchall()
            tri_rows = []
            for fr in fts_rows:
                canon_tags = " ".join(canonicalize_tag(tok) for tok in (fr[4] or "").split() if tok)
                blob = " ".join(x for x in (fr[2], fr[1], fr[3], fr[4], canon_tags, fr[5]) if x)
                tri_rows.append((fr[0], blob[:24_000]))
            if tri_rows:
                conn.executemany(
                    "INSERT INTO sheets_trigram(sheet_id, search_blob) VALUES (?, ?)",
                    tri_rows,
                )


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
                invalidate_session_cache(username=admin_user)


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
    token_hash = _hash_session_token(token)
    now = now_kst_iso()
    with state_conn() as conn:
        conn.execute(
            "INSERT INTO sessions(token, username, role, user_agent, ip, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_hash, username, role, (user_agent or "")[:200], ip, now, now),
        )
    return token


def get_session(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    token_hash = _hash_session_token(token)
    mono_now = time.monotonic()
    with _session_cache_lock:
        hit = _session_cache.get(token_hash)
        if hit is not None:
            exp_mono, cached = hit
            if exp_mono > mono_now:
                return dict(cached)
            _session_cache.pop(token_hash, None)

    with state_conn() as conn:
        # 해시 토큰 우선 조회 + 구버전 평문 세션 무중단 호환(조회 즉시 해시로 자동 승격)
        row = conn.execute(
            "SELECT token, username, role, created_at, last_used_at FROM sessions "
            "WHERE token = ? OR token = ?",
            (token_hash, token),
        ).fetchone()
        if row is None:
            return None
        stored_key = row["token"]
        now = now_kst()
        last_dt = _parse_kst_iso(row["last_used_at"])
        if last_dt is not None:
            age_s = (now - last_dt).total_seconds()
            if age_s > SESSION_TTL_SECONDS:
                conn.execute("DELETE FROM sessions WHERE token = ?", (stored_key,))
                return None
            if stored_key != token_hash:
                conn.execute(
                    "UPDATE sessions SET token = ?, last_used_at = ? WHERE token = ?",
                    (token_hash, now_kst_iso(), stored_key),
                )
            elif age_s >= SESSION_TOUCH_INTERVAL_S:
                conn.execute(
                    "UPDATE sessions SET last_used_at = ? WHERE token = ?",
                    (now_kst_iso(), stored_key),
                )
        else:
            conn.execute(
                "UPDATE sessions SET token = ?, last_used_at = ? WHERE token = ?",
                (token_hash, now_kst_iso(), stored_key),
            )
        sess = {
            "username": row["username"],
            "role": row["role"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }

    with _session_cache_lock:
        if len(_session_cache) >= MAX_SESSION_CACHE:
            expired = [k for k, (e, _) in _session_cache.items() if e <= mono_now]
            for k in expired:
                _session_cache.pop(k, None)
            while len(_session_cache) >= MAX_SESSION_CACHE:
                _session_cache.pop(next(iter(_session_cache)))
        _session_cache[token_hash] = (mono_now + SESSION_CACHE_TTL_S, sess)
    return dict(sess)


def delete_session(token: str) -> None:
    if not token:
        return
    token_hash = _hash_session_token(token)
    invalidate_session_cache(token=token)
    with state_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ? OR token = ?", (token_hash, token))
