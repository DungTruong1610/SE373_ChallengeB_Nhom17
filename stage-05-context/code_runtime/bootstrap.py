"""Runtime của Code Mode: process con chạy code Python do model sinh.

Chỉ giao tiếp với harness qua stdin/stdout theo JSON-lines:
- harness -> runtime, dòng đầu: {"code": "..."}
- runtime -> harness: {"type": "call", "id": n, "tool": "read_file", "args": {...}}
- harness -> runtime:  {"id": n, "ok": true, "value": ...} hoặc {"id": n, "ok": false, "error": {...}}
- runtime -> harness, cuối cùng: {"type": "done", "stdout": "...", "error": null | "NameError: ... (line 3)"}

print() của code được gom vào buffer; chỉ buffer này (sau khi harness cắt theo output cap) vào context.
Audit hook chặn open/listdir ngoài thư mục Python, ghi file, socket, subprocess để dữ liệu chỉ đi qua tools.*.
Đây là lớp phòng thủ best-effort, KHÔNG phải sandbox (ctypes, lỗ hổng interpreter... vẫn có thể vượt qua).
File này chạy độc lập trong process con, không import module của project.
"""

# Import trước các module stdlib hay dùng: sau khi cài audit hook, import mới vẫn được phép
# (chỉ đọc file trong thư mục Python), nhưng import sẵn giúp code của model chạy nhanh hơn.
import collections  # noqa: F401
import datetime  # noqa: F401
import io
import itertools  # noqa: F401
import json
import math  # noqa: F401
import os
import re  # noqa: F401
import statistics  # noqa: F401
import sys
import traceback

CODE_FILENAME = "<run_python>"
PROTO_OUT = sys.stdout
PROTO_IN = sys.stdin
ALLOWED_ROOTS = tuple(sorted({os.path.normcase(os.path.realpath(p)) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}))
BLOCKED_PREFIXES = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile",
                    "os.fork", "os.kill", "ctypes.", "winreg.", "_winapi.", "webbrowser.", "urllib.", "http.")
BLOCKED_EVENTS = {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.symlink", "os.link",
                  "os.truncate", "os.putenv", "os.unsetenv", "shutil.rmtree", "shutil.copyfile", "shutil.move"}
PATH_EVENTS = {"open", "os.listdir", "os.scandir", "glob.glob", "os.chdir"}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class ToolError(Exception):
    """Tool trả lỗi (ví dụ FILE_NOT_FOUND, PERMISSION_DENIED); message dạng 'CODE: mô tả'."""


def _send(message: dict) -> None:
    PROTO_OUT.write(json.dumps(message, ensure_ascii=False) + "\n")
    PROTO_OUT.flush()


def _inside_python(path) -> bool:
    if isinstance(path, int):  # file descriptor đã mở sẵn
        return True
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    real = os.path.normcase(os.path.realpath(str(path)))
    return any(real == root or real.startswith(root + os.sep) for root in ALLOWED_ROOTS)


def _is_write(event: str, args: tuple) -> bool:
    if event != "open":
        return False
    mode, flags = (args + (None, None))[1:3]
    if isinstance(mode, str):
        return any(ch in mode for ch in "wax+")
    return isinstance(flags, int) and bool(flags & WRITE_FLAGS)


def _guard(event: str, args: tuple) -> None:
    blocked = event in BLOCKED_EVENTS or event.startswith(BLOCKED_PREFIXES)
    if not blocked and event in PATH_EVENTS:
        blocked = _is_write(event, args) or not (args and _inside_python(args[0]))
    if blocked:
        raise PermissionError(f"Runtime chặn {event}: chỉ truy cập dữ liệu qua tools.*")


class Tools:
    """SDK `tools.*` của runtime. Mỗi lời gọi là một request về harness (proxy), không truy cập dữ liệu trực tiếp."""

    def __init__(self) -> None:
        self._next_id = 0

    def _call(self, tool: str, **args):
        self._next_id += 1
        _send({"type": "call", "id": self._next_id, "tool": tool, "args": args})
        line = PROTO_IN.readline()
        if not line:
            raise ToolError("HARNESS_CLOSED: harness đã đóng kênh")
        reply = json.loads(line)
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise ToolError(f"{error.get('code', 'TOOL_ERROR')}: {error.get('message', '')}")
        return reply.get("value")

    def list_files(self, path: str = ".") -> list:
        """Mục trực tiếp trong thư mục: [{"name", "path", "type"}]."""
        return self._call("list_files", path=path)

    def read_file(self, path: str) -> str:
        """Nội dung file văn bản UTF-8."""
        return self._call("read_file", path=path)


def short_error(exc: BaseException) -> str:
    """Lỗi ngắn để model sửa code: 'Loại: thông điệp (line N)', không trả stack trace dài."""
    line = getattr(exc, "lineno", None) if isinstance(exc, SyntaxError) else None
    if line is None:
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == CODE_FILENAME]
        line = frames[-1].lineno if frames else None
    where = f" (line {line})" if line else ""
    message = exc.msg if isinstance(exc, SyntaxError) else str(exc)
    return f"{type(exc).__name__}: {message}{where}"


def main() -> None:
    request = json.loads(PROTO_IN.readline() or "{}")
    buffer = io.StringIO()
    error = None
    sys.addaudithook(_guard)  # không gỡ được sau khi cài
    sys.stdout = sys.stderr = buffer
    try:
        compiled = compile(request.get("code", ""), CODE_FILENAME, "exec")
        exec(compiled, {"__name__": "__main__", "tools": Tools(), "ToolError": ToolError})
    except BaseException as exc:  # gồm cả SystemExit do code gọi exit()
        if not (isinstance(exc, SystemExit) and exc.code in (None, 0)):
            error = short_error(exc)
    finally:
        sys.stdout, sys.stderr = PROTO_OUT, sys.__stderr__
    _send({"type": "done", "stdout": buffer.getvalue(), "error": error})


if __name__ == "__main__":
    main()
