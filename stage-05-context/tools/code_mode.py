"""Tool run_python (Code Mode): model gửi một chương trình, harness chạy nó; chỉ kết quả đã tổng hợp vào context.

Danh sách hàm tools.* KHÔNG nằm trong tham số `tools` của API; nó là tài liệu SDK đặt trong description của
run_python (cách "nhúng type definitions vào description" trong explainer, đủ gọn vì SDK chỉ có 2 hàm).
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

import paths
from code_runtime.runner import Limits, run_code
from config import CODE_MAX_TOOL_CALLS, CODE_OUTPUT_CAP_CHARS, CODE_TIMEOUT_SECONDS

LIMITS = Limits(timeout_seconds=CODE_TIMEOUT_SECONDS, max_tool_calls=CODE_MAX_TOOL_CALLS, output_cap_chars=CODE_OUTPUT_CAP_CHARS)

RUN_PYTHON_DESCRIPTION = f"""Chạy một chương trình Python 3 ngắn trong runtime riêng (Code Mode) và trả về JSON.

Dùng khi cần quét, lọc, đếm hoặc tổng hợp NHIỀU file (ví dụ nhiều file log): code tự đọc dữ liệu qua SDK `tools`,
dữ liệu thô nằm lại trong runtime, chỉ phần bạn print() mới quay về context. Cần đọc hiểu một file đơn lẻ thì dùng read_file.

SDK có sẵn trong biến `tools` (không cần import):
  tools.list_files(path: str) -> list[dict]  # mục trực tiếp trong thư mục, mỗi mục {{"name", "path", "type": "file"|"directory"}}
  tools.read_file(path: str) -> str          # nội dung file văn bản UTF-8
Path tương đối workspace, ví dụ "data/logs" hoặc entry["path"]. Lỗi tool raise ToolError("CODE: message").

Quy tắc runtime:
- Không mở file, mạng hay subprocess trực tiếp (bị chặn); chỉ truy cập dữ liệu qua tools.*. Được import stdlib (re, json, collections...).
- Timeout {CODE_TIMEOUT_SECONDS} giây; tối đa {CODE_MAX_TOOL_CALLS} lời gọi tools.* mỗi lần chạy.
- Chỉ stdout vào context, tối đa {CODE_OUTPUT_CAP_CHARS} ký tự: print kết quả đã tổng hợp kèm số liệu kiểm chứng
  (số file đã quét, số dòng, file lỗi), KHÔNG print dữ liệu thô.

Kết quả: {{"ok": true, "stdout": "...", "truncated": false, "proxy_calls": 11, "runtime_bytes": 182000, "denied": []}}.
Lỗi: {{"ok": false, "error": {{"code": "CODE_ERROR" | "TIMEOUT" | "RUNTIME_CRASH", "message": "NameError: ... (line 3)"}}, ...}}: sửa code rồi chạy lại."""


@tool("run_python", description=RUN_PYTHON_DESCRIPTION)
def run_python(code: str) -> str:
    return json.dumps(run_code(code, paths.WORKSPACE_DIR, LIMITS), ensure_ascii=False)
