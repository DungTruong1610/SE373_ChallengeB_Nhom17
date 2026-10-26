"""B2 Code Mode: runtime chạy code qua proxy tools.*; dữ liệu thô không quay về context; permission và limits."""

import json

import pytest

import paths
from code_runtime.runner import Limits, check_permission, run_code
from config import CODE_MAX_TOOL_CALLS, CODE_OUTPUT_CAP_CHARS, CODE_TIMEOUT_SECONDS
from scripts.gen_logs import expected_summary
from tools import run_python

LIMITS = Limits(timeout_seconds=10, max_tool_calls=300, output_cap_chars=2_000)

AGGREGATE = """
import collections
codes = collections.Counter()
files = 0
for entry in tools.list_files("data/logs"):
    files += 1
    for line in tools.read_file(entry["path"]).splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[1] == "ERROR":
            codes[parts[3]] += 1
print(files, codes.most_common(3))
"""


def run(code, limits=LIMITS):
    return run_code(code, paths.WORKSPACE_DIR, limits)


def test_aggregates_many_logs_and_only_summary_reaches_context(lab_dirs):
    result = run(AGGREGATE)
    truth = expected_summary(paths.WORKSPACE_DIR / "data" / "logs")
    assert result["ok"], result
    assert result["stdout"].strip() == f"{truth['files']} {truth['top_error_codes']}"
    assert result["proxy_calls"] == 1 + truth["files"]
    assert result["calls_by_tool"] == {"list_files": 1, "read_file": truth["files"]}
    # runtime nhận cả trăm KB log, context chỉ nhận một dòng tổng hợp
    assert result["runtime_bytes"] > 100_000
    assert len(result["stdout"]) < 200 and "req=" not in result["stdout"]
    assert result["denied"] == []


def test_output_cap_truncates_and_reports_total(lab_dirs):
    result = run("print('x' * 5000)")
    assert result["ok"] and result["truncated"] is True
    assert result["stdout"].startswith("x" * 2_000)
    assert "tổng 5001 ký tự" in result["stdout"]


def test_timeout_kills_infinite_loop(lab_dirs):
    result = run("while True:\n    pass", Limits(timeout_seconds=1, max_tool_calls=10, output_cap_chars=500))
    assert result["ok"] is False
    assert result["error"]["code"] == "TIMEOUT"
    assert result["elapsed_ms"] < 8_000


def test_tool_call_limit(lab_dirs):
    code = """
errors = 0
for _ in range(5):
    try:
        tools.read_file("data/weekly_notes.md")
    except ToolError as exc:
        errors += 1
        last = str(exc)
print(errors, last.split(':')[0])
"""
    result = run(code, Limits(timeout_seconds=10, max_tool_calls=3, output_cap_chars=500))
    assert result["ok"]
    assert result["stdout"].strip() == "2 TOOL_CALL_LIMIT"
    assert result["proxy_calls"] == 3
    assert [d["reason"] for d in result["denied"]] == ["TOOL_CALL_LIMIT", "TOOL_CALL_LIMIT"]


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ('tools.read_file(".lab-workspace")', "PERMISSION_DENIED"),
        ('tools.read_file("../config.py")', "PATH_OUTSIDE_WORKSPACE"),
        ('tools._call("write_file", path="output/x.md", content="y")', "PERMISSION_DENIED"),
        ('tools._call("http_post", url="https://example.com")', "PERMISSION_DENIED"),
    ],
)
def test_proxy_enforces_permissions(lab_dirs, call, expected):
    result = run(f"try:\n    {call}\nexcept ToolError as exc:\n    print(str(exc).split(':')[0])")
    assert result["ok"]
    assert result["stdout"].strip() == expected
    assert not (paths.WORKSPACE_DIR / "output" / "x.md").exists()


@pytest.mark.parametrize(
    "code",
    [
        "open(LOG).read()",
        "import os\nos.listdir(WS)",
        "import socket\nsocket.create_connection(('example.com', 80), timeout=1)",
        "import subprocess\nsubprocess.run(['python', '--version'])",
        "open('stolen.txt', 'w').write('x')",
    ],
)
def test_runtime_blocks_bypassing_the_proxy(lab_dirs, code):
    log = paths.WORKSPACE_DIR / "data" / "logs" / "app-2026-10-11.log"
    prelude = f"LOG = {str(log)!r}\nWS = {str(paths.WORKSPACE_DIR)!r}\n"
    result = run(prelude + code)
    assert result["ok"] is False
    assert result["error"]["code"] == "CODE_ERROR"
    assert result["error"]["message"].startswith("PermissionError: Runtime chặn")


def test_errors_are_short_with_line_number(lab_dirs):
    syntax = run("def f(:\n    pass")
    assert syntax["error"]["code"] == "CODE_ERROR"
    assert syntax["error"]["message"].startswith("SyntaxError") and "(line 1)" in syntax["error"]["message"]
    name = run("x = 1\nprint(y)")
    assert name["error"]["message"] == "NameError: name 'y' is not defined (line 2)"
    missing = run("tools.read_file('data/khong-co.log')")
    assert missing["error"]["message"].startswith("ToolError: FILE_NOT_FOUND")
    assert "Traceback" not in json.dumps([syntax, name, missing], ensure_ascii=False)


def test_runtime_env_has_no_credentials(lab_dirs, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    result = run("import os\nprint(sorted(os.environ))")
    assert result["ok"]
    assert "OPENAI_API_KEY" not in result["stdout"]


def test_check_permission_rules():
    assert check_permission("read_file", {"path": "data/logs/a.log"}) is None
    assert check_permission("list_files", {"path": "."}) is None
    assert "ẩn" in check_permission("read_file", {"path": "data/.env"})
    assert "không được cấp" in check_permission("bash", {"command": "ls"})
    assert "Tham số không hợp lệ" in check_permission("read_file", {"path": "a", "mode": "w"})


def test_run_python_tool_returns_json_and_documents_sdk(lab_dirs):
    description = run_python.description
    assert "tools.read_file(path: str) -> str" in description and "tools.list_files(path: str)" in description
    assert f"Timeout {CODE_TIMEOUT_SECONDS} giây" in description and str(CODE_MAX_TOOL_CALLS) in description
    assert str(CODE_OUTPUT_CAP_CHARS) in description
    result = json.loads(run_python.invoke({"code": "print(len(tools.list_files('data/logs')))"}))
    assert result["ok"] and result["stdout"].strip() == "10"
