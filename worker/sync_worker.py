#!/usr/bin/env python3
"""구글 드라이브 증분 감지 · 저부하 타일링 · n8n 실시간 알림 워커.

뷰어 컨테이너(drawing-smart-viewer, RAM 256MB 캡)와 물리적으로 분리 실행되며,
공유 볼륨(/app/data)의 drawings.db를 원자 교체하여 무중단 자동 갱신을 제공한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_index import build_index_incremental  # noqa: E402

KST = timezone(timedelta(hours=9), "KST")


def now_kst_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
STATE_DIR = Path(os.environ.get("WORKER_STATE_DIR", "/app/worker_state"))
RAW_CACHE_DIR = STATE_DIR / "raw_cache"
RCLONE_CONF_PATH = STATE_DIR / "rclone.conf"
RCLONE_HASH_PATH = STATE_DIR / ".rclone_env.sha256"
STATUS_FILE = DATA_DIR / "sync_status.json"

GDRIVE_REMOTE_PATH = os.environ.get("GDRIVE_REMOTE_PATH", "").strip()
GDRIVE_ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "").strip()
PROJECT_NAME = os.environ.get("PROJECT_NAME", "도면 프로젝트").strip() or "도면 프로젝트"
SYNC_INTERVAL_MIN = max(1, int(os.environ.get("SYNC_INTERVAL_MIN", "15")))
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "").strip()
WORKER_PORT = int(os.environ.get("WORKER_PORT", "8099"))
BATCH_COMMIT = max(1, int(os.environ.get("BATCH_COMMIT", "20")))
SHEET_SLEEP = max(0.0, float(os.environ.get("SHEET_SLEEP", "0.15")))
ENABLE_OCR = os.environ.get("ENABLE_OCR", "true").strip().lower() not in {"0", "false", "no", "off"}
RCLONE_MAX_DELETE = max(0, int(os.environ.get("RCLONE_MAX_DELETE", "50")))
MAX_DELETE_RATIO = max(0.0, float(os.environ.get("MAX_DELETE_RATIO", "0.30")))
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "").strip()
VIEWER_PUBLIC_URL = os.environ.get("VIEWER_PUBLIC_URL", "").strip()

_sync_lock = threading.Lock()
_state_lock = threading.Lock()
_status: dict[str, Any] = {
    "state": "idle",
    "remote_path": GDRIVE_REMOTE_PATH,
    "interval_min": SYNC_INTERVAL_MIN,
    "n8n_configured": bool(N8N_WEBHOOK_URL),
    "last_checked_kst": None,
    "last_updated_kst": None,
    "last_reason": None,
    "progress": None,
    "last_result": None,
    "last_error": None,
    "last_alert": None,
}


def _persist_status() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _state_lock:
        payload = json.dumps(_status, ensure_ascii=False, indent=2)
    tmp = DATA_DIR / ".sync_status.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(STATUS_FILE)


def _update_status(**kwargs: Any) -> None:
    with _state_lock:
        _status.update(kwargs)
    _persist_status()


def ensure_rclone_config() -> Path:
    """RCLONE_CONF_B64 환경변수로 초기 rclone.conf를 생성(토큰 자동 갱신은 볼륨에 보존)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    b64 = os.environ.get("RCLONE_CONF_B64", "").strip()
    if b64:
        env_hash = hashlib.sha256(b64.encode("utf-8")).hexdigest()
        prev_hash = RCLONE_HASH_PATH.read_text(encoding="utf-8").strip() if RCLONE_HASH_PATH.is_file() else ""
        if not RCLONE_CONF_PATH.is_file() or env_hash != prev_hash:
            decoded = base64.b64decode(b64)
            RCLONE_CONF_PATH.write_bytes(decoded)
            os.chmod(RCLONE_CONF_PATH, 0o600)
            RCLONE_HASH_PATH.write_text(env_hash, encoding="utf-8")
    if not RCLONE_CONF_PATH.is_file():
        raise RuntimeError("rclone 설정(RCLONE_CONF_B64 또는 /app/worker_state/rclone.conf)이 없습니다")
    return RCLONE_CONF_PATH


def notify_n8n(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """n8n 웹훅으로 동기화 결과 또는 오류 실시간 알림 전송."""
    if not N8N_WEBHOOK_URL:
        return {"sent": False, "reason": "N8N_WEBHOOK_URL 미설정"}

    body = {
        "event": event,
        "at_kst": now_kst_iso(),
        "project": PROJECT_NAME,
        "viewer_url": VIEWER_PUBLIC_URL,
        **payload,
    }
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        N8N_WEBHOOK_URL,
        data=raw,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    last_err = ""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                code = resp.status
                resp_txt = resp.read(300).decode("utf-8", errors="replace")
                res = {"sent": True, "http_status": code, "at_kst": now_kst_iso(), "event": event, "response": resp_txt}
                _update_status(last_alert=res)
                return res
        except Exception as exc:
            last_err = str(exc)
            if attempt == 0:
                time.sleep(2)
    res = {"sent": False, "error": last_err, "at_kst": now_kst_iso(), "event": event}
    _update_status(last_alert=res)
    return res


def _run_rclone_sync(conf_path: Path, started_at: str) -> dict[str, Any]:
    """구글 드라이브 감시 폴더의 PDF만 로컬 캐시와 동기화(서브경로 풀스캔 방지 + 실시간 진행률 파싱)."""
    if not GDRIVE_REMOTE_PATH and not GDRIVE_ROOT_FOLDER_ID:
        raise RuntimeError("GDRIVE_REMOTE_PATH 또는 GDRIVE_ROOT_FOLDER_ID 환경변수가 필요합니다")

    # --drive-root-folder-id가 설정되면 루트 탐색 없이 감시 폴더 ID에서 직결 시작
    remote_arg = GDRIVE_REMOTE_PATH
    extra_flags: list[str] = []
    if GDRIVE_ROOT_FOLDER_ID:
        remote_name = (GDRIVE_REMOTE_PATH.split(":", 1)[0] if ":" in GDRIVE_REMOTE_PATH else "gdrive_personal").strip()
        remote_arg = f"{remote_name}:"
        extra_flags = ["--drive-root-folder-id", GDRIVE_ROOT_FOLDER_ID]

    # 주의: Google Drive 서브경로에서 --fast-list 사용 시 전체 드라이브를 풀스캔하므로 배제
    max_del_flags = ["--max-delete", str(RCLONE_MAX_DELETE)] if RCLONE_MAX_DELETE > 0 else []
    cmd = [
        "rclone", "sync",
        remote_arg,
        str(RAW_CACHE_DIR),
        "--config", str(conf_path),
        *extra_flags,
        "--ignore-case",
        "--include", "*.pdf",
        "--delete-excluded",
        *max_del_flags,
        "--transfers", "3",
        "--checkers", "6",
        "--contimeout", "30s",
        "--timeout", "120s",
        "--stats-one-line",
        "--stats", "3s",
        "-v",
    ]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    tail_lines: list[str] = []
    deadline = t0 + 1800
    last_push = 0.0
    assert proc.stderr is not None
    for raw_line in proc.stderr:
        line = raw_line.strip()
        if not line:
            continue
        tail_lines.append(line)
        if len(tail_lines) > 25:
            tail_lines.pop(0)
        now_t = time.perf_counter()
        if now_t > deadline:
            proc.kill()
            proc.wait()
            raise RuntimeError("rclone sync 타임아웃 (1800초 초과)")
        if ("Transferred:" in line or "ETA" in line or "%" in line) and (now_t - last_push >= 2.5):
            last_push = now_t
            _update_status(
                state="syncing_drive",
                progress={
                    "phase": f"구글 드라이브 동기화 중: {line[:120]}",
                    "started_at": started_at,
                },
            )
    rc = proc.wait(timeout=30)
    elapsed = round(time.perf_counter() - t0, 2)
    if rc != 0:
        err_tail = "\n".join(tail_lines[-8:])[:600].strip()
        raise RuntimeError(f"rclone sync 실패 (exit={rc}): {err_tail}")
    return {"rclone_elapsed_sec": elapsed}


def run_sync_cycle(reason: str = "scheduled", force_notify: bool = False) -> dict[str, Any]:
    """단일 증분 동기화 사이클 (rclone → SHA-256 증분 타일링 → n8n 알림)."""
    if not _sync_lock.acquire(blocking=False):
        return {"status": "already_running", "at_kst": now_kst_iso()}

    started_at = now_kst_iso()
    try:
        _update_status(
            state="syncing_drive",
            last_reason=reason,
            last_error=None,
            progress={"phase": "구글 드라이브 변경 파일 확인 중...", "started_at": started_at},
        )
        conf_path = ensure_rclone_config()
        rclone_info = _run_rclone_sync(conf_path, started_at)

        _update_status(
            state="indexing",
            progress={"phase": "신규/변경 도면 분석 중...", "started_at": started_at, **rclone_info},
        )

        def on_progress(p: dict[str, Any]) -> None:
            _update_status(
                state="indexing",
                progress={
                    "phase": f"신규 도면 타일링 ({p['processed_pdfs']}/{p['pending_pdfs']} PDF)",
                    "started_at": started_at,
                    **rclone_info,
                    **p,
                },
            )

        summary = build_index_incremental(
            RAW_CACHE_DIR,
            DATA_DIR,
            workers=1,
            project_name=PROJECT_NAME,
            batch_commit=BATCH_COMMIT,
            sheet_sleep=SHEET_SLEEP,
            enable_ocr=ENABLE_OCR,
            ocr_subprocess=True,
            max_delete_ratio=MAX_DELETE_RATIO,
            progress_cb=on_progress,
        )
        summary.update(rclone_info)
        summary["reason"] = reason

        now_s = now_kst_iso()
        updates: dict[str, Any] = {
            "state": "idle",
            "last_checked_kst": now_s,
            "last_result": summary,
            "last_error": None,
            "progress": None,
        }
        if summary.get("status") == "updated":
            updates["last_updated_kst"] = now_s
        _update_status(**updates)

        should_notify = (
            force_notify
            or summary.get("added_pdfs", 0) > 0
            or summary.get("removed_pdfs", 0) > 0
        )
        if should_notify:
            notify_n8n("sync_updated", summary)

        print(
            f"[Worker {now_s}] 완료({reason}): status={summary['status']} "
            f"+{summary['added_pdfs']}PDF(+{summary['added_sheets']}장) "
            f"-{summary['removed_pdfs']}PDF(-{summary['removed_sheets']}장) "
            f"total={summary['total_sheets']}장 ({summary['elapsed_sec']}s)",
            flush=True,
        )
        return summary
    except Exception as exc:
        err_msg = str(exc)
        now_s = now_kst_iso()
        _update_status(
            state="idle",
            last_checked_kst=now_s,
            last_error=err_msg,
            progress=None,
        )
        print(f"[Worker {now_s}] 오류({reason}): {err_msg}", file=sys.stderr, flush=True)
        notify_n8n("sync_error", {"reason": reason, "error": err_msg})
        return {"status": "error", "error": err_msg, "at_kst": now_s}
    finally:
        _sync_lock.release()


def _scheduler_loop() -> None:
    # 컨테이너 기동 직후 10초 안정화 대기 후 최초 1회 동기화
    time.sleep(10)
    while True:
        run_sync_cycle(reason="scheduled")
        time.sleep(SYNC_INTERVAL_MIN * 60)


class _ControlHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return  # 헬스체크 로그 노이즈 억제

    def _send_json(self, code: int, obj: dict[str, Any]) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        if not SYNC_TOKEN:
            return False
        got = (self.headers.get("X-Sync-Token") or "").strip()
        return bool(got) and hmac.compare_digest(got, SYNC_TOKEN)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok", "kst": now_kst_iso()})
            return
        if self.path == "/status":
            if not self._authorized():
                self._send_json(401, {"detail": "unauthorized"})
                return
            with _state_lock:
                snap = dict(_status)
            self._send_json(200, snap)
            return
        self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(401, {"detail": "unauthorized"})
            return
        if self.path == "/trigger":
            if _sync_lock.locked():
                self._send_json(200, {"status": "already_running", "at_kst": now_kst_iso()})
                return
            threading.Thread(
                target=run_sync_cycle,
                kwargs={"reason": "manual"},
                daemon=True,
            ).start()
            self._send_json(202, {"status": "started", "at_kst": now_kst_iso()})
            return
        if self.path == "/test-alert":
            res = notify_n8n(
                "test_alert",
                {
                    "message": "도면 스마트 뷰어 ↔ n8n 실시간 알림 연동 테스트 성공",
                    "added_pdfs": 0,
                    "added_sheets": 0,
                    "removed_pdfs": 0,
                    "removed_sheets": 0,
                    "total_sheets": (_status.get("last_result") or {}).get("total_sheets", 0),
                },
            )
            self._send_json(200 if res.get("sent") else 502, res)
            return
        self._send_json(404, {"detail": "not found"})


def main() -> int:
    # DS920+ 타 서비스(DSM, 뷰어, n8n) 보호를 위해 최저 CPU 우선순위 적용
    try:
        os.nice(15)
    except OSError:
        pass

    _persist_status()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print(
        f"[Worker {now_kst_iso()}] 시작 — remote={GDRIVE_REMOTE_PATH or '(미설정)'}, "
        f"interval={SYNC_INTERVAL_MIN}분, n8n={'ON' if N8N_WEBHOOK_URL else 'OFF'}, port={WORKER_PORT}",
        flush=True,
    )
    server = ThreadingHTTPServer(("0.0.0.0", WORKER_PORT), _ControlHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
