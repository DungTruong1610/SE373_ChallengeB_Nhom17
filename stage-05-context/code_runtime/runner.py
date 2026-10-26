"""Code Mode phía harness: dispatcher, proxy tools.*, permission check, timeout và output cap.

Luồng một lần run_python:
    model -> tool call run_python(code) -> run_code() -> process con (bootstrap.py)
    code gọi tools.read_file(...) -> request JSON-lines về đây -> check_permission -> _read/_list thật
    -> giá trị trả về runtime (KHÔNG vào context) -> code tự lọc/đếm -> print -> stdout (đã cắt) vào context.

Process con chạy bằng Python của project với `-I` (isolated), cwd là thư mục tạm trống, env tối thiểu
(không có API key). Đây vẫn KHÔNG phải sandbox: chỉ dùng trong lab với dữ liệu giả.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tools.files import _list, _read

BOOTSTRAP = Path(__file__).with_name("bootstrap.py")
# Tool mà runtime được gọi qua proxy; mỗi tool trả về đúng phần giá trị code cần (không bọc JSON ok/error).
ALLOWED_TOOLS = {
    "list_files": (_list, "entries"),
    "read_file": (_read, "content"),
}
STDERR_TAIL_CHARS = 1_000


@dataclass(frozen=True)
class Limits:
    timeout_seconds: float
    max_tool_calls: int
    output_cap_chars: int


def runtime_env() -> dict[str, str]:
    """Env tối thiểu cho process con. Không truyền OPENAI_API_KEY hay biến môi trường khác của app."""
    env = {"PATH": str(Path(sys.executable).parent), "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("SYSTEMROOT", "TEMP", "TMP"):  # Windows cần SYSTEMROOT để khởi động Python
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def check_permission(tool: str, args: dict) -> str | None:
    """Trả lý do từ chối, hoặc None nếu được phép. Path ra ngoài workspace đã bị _resolve chặn ở tool thật."""
    if tool not in ALLOWED_TOOLS:
        return f"Tool {tool!r} không được cấp cho runtime. Được phép: {', '.join(ALLOWED_TOOLS)}."
    unknown = set(args) - {"path"}
    if unknown:
        return f"Tham số không hợp lệ cho {tool}: {', '.join(sorted(unknown))}."
    parts = PurePosixPath(str(args.get("path", "")).replace("\\", "/")).parts
    if any(part.startswith(".") and part not in (".", "..") for part in parts):
        return f"Không được đọc file/thư mục ẩn: {args.get('path')}"
    return None


def clip(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap] + f"\n…[đã cắt, tổng {len(text)} ký tự; hãy tổng hợp trước khi print]", True


class _Session:
    """Trạng thái của một lần chạy: đếm proxy call, byte runtime đã nhận, các lời gọi bị từ chối."""

    def __init__(self, workspace: Path, limits: Limits):
        self.workspace = workspace
        self.limits = limits
        self.proxy_calls = 0
        self.runtime_bytes = 0
        self.calls_by_tool: dict[str, int] = {}
        self.denied: list[dict] = []

    def dispatch(self, message: dict) -> dict:
        tool, args = message.get("tool"), message.get("args") or {}
        reply = {"id": message.get("id")}
        if self.proxy_calls >= self.limits.max_tool_calls:
            self.denied.append({"tool": tool, "args": args, "reason": "TOOL_CALL_LIMIT"})
            return {**reply, "ok": False, "error": {"code": "TOOL_CALL_LIMIT", "message": f"Vượt {self.limits.max_tool_calls} lời gọi tools.* trong một lần chạy."}}
        self.proxy_calls += 1
        reason = check_permission(tool, args)
        if reason:
            self.denied.append({"tool": tool, "args": args, "reason": reason})
            return {**reply, "ok": False, "error": {"code": "PERMISSION_DENIED", "message": reason}}
        self.calls_by_tool[tool] = self.calls_by_tool.get(tool, 0) + 1
        func, field = ALLOWED_TOOLS[tool]
        result = func(self.workspace, str(args.get("path", "")))
        if not result.get("ok"):
            return {**reply, "ok": False, "error": result.get("error")}
        value = result[field]
        self.runtime_bytes += len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return {**reply, "ok": True, "value": value}


def _pump(stream, sink: queue.Queue) -> None:
    for line in stream:
        sink.put(line)
    sink.put(None)  # EOF


def _drain(stream, chunks: list[str]) -> None:
    for line in stream:
        chunks.append(line)


def run_code(code: str, workspace: Path, limits: Limits) -> dict:
    """Chạy code trong runtime riêng; chỉ trả stdout đã cắt + số liệu, không trả dữ liệu tool thô."""
    started = time.perf_counter()
    if not code.strip():
        return {"ok": False, "stdout": "", "truncated": False, "error": {"code": "EMPTY_CODE", "message": "Code rỗng."},
                "proxy_calls": 0, "runtime_bytes": 0, "calls_by_tool": {}, "denied": [], "elapsed_ms": 0.0}
    session = _Session(workspace, limits)
    done: dict | None = None
    timed_out = False
    stderr_chunks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="code-mode-") as scratch:
        process = subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", str(BOOTSTRAP)],
            cwd=scratch,
            env=runtime_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=_pump, args=(process.stdout, lines), daemon=True).start()
        threading.Thread(target=_drain, args=(process.stderr, stderr_chunks), daemon=True).start()
        deadline = time.monotonic() + limits.timeout_seconds
        try:
            process.stdin.write(json.dumps({"code": code}, ensure_ascii=False) + "\n")
            process.stdin.flush()
            while True:
                try:
                    line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    timed_out = True
                    break
                if line is None:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # dòng lạ (code ghi thẳng ra sys.__stdout__): bỏ qua, không phải giao thức
                if message.get("type") == "call":
                    process.stdin.write(json.dumps(session.dispatch(message), ensure_ascii=False) + "\n")
                    process.stdin.flush()
                elif message.get("type") == "done":
                    done = message
                    break
        except (BrokenPipeError, OSError):
            pass  # process con đã thoát; xử lý như crash bên dưới
        finally:
            if process.poll() is None and (timed_out or done is None):
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    stdout, truncated = clip((done or {}).get("stdout") or "", limits.output_cap_chars)
    error = None
    if timed_out:
        error = {"code": "TIMEOUT", "message": f"Code chạy quá {limits.timeout_seconds} giây, đã dừng."}
    elif done is None:
        tail = "".join(stderr_chunks)[-STDERR_TAIL_CHARS:].strip()
        error = {"code": "RUNTIME_CRASH", "message": tail or f"Runtime thoát với mã {process.returncode}."}
    elif done.get("error"):
        error = {"code": "CODE_ERROR", "message": done["error"]}
    result = {
        "ok": error is None,
        "stdout": stdout,
        "truncated": truncated,
        "proxy_calls": session.proxy_calls,
        "runtime_bytes": session.runtime_bytes,
        "calls_by_tool": session.calls_by_tool,
        "denied": session.denied,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    if error:
        result["error"] = error
    return result
