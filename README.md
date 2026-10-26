# SE373 Buổi 7: Challenge B, Context management

Bài làm của nhóm 17.

| STT | MSSV | Họ và tên |
|---:|---|---|
| 81 | 24520343 | Nguyễn Trung Dũng |
| 82 | 24520352 | Trương Đình Việt Dũng |
| 83 | 24520398 | Trần Đình Duy |
| 84 | 24521140 | Võ Hoàng Nghi |
| 85 | 24521040 | Bùi Duy Mạnh |

Môn SE373 (Agentic AI), buổi 7 "Memory, Context Engineering & Prompt Caching". Repo này làm **Challenge B** (slide 32–33):
giữ context nhỏ gọn mà agent vẫn làm đúng việc.

| Bài | Yêu cầu của đề | Cách làm trong repo |
|---|---|---|
| **B1: Compact** | Session dài được compact; agent vẫn nhớ lỗi đang điều tra, cách đã thử, bước tiếp | `CompactionMiddleware` tạo **structured checkpoint** (schema theo slide 22), áp KEEP / TRUNCATE / DROP (slide 23) khi history vượt ngưỡng |
| **B2: Code Mode** | Agent trả lời câu hỏi trên nhiều file log; raw log không đi vào context | Tool `run_python`: model gửi chương trình, runtime chạy nó và gọi `tools.read_file / list_files` qua **proxy** của harness (permission, timeout, output cap); chỉ `print` mới vào context |

Agent dùng lại lab Buổi 4 (`agent-tools-skills-lab`): LangChain `create_agent`, UI Streamlit có panel State & Context,
`ObserverMiddleware` và trace JSONL. Bản gốc là `stage-02-skills` (đã có `list_files`). Stage mới là
[`stage-05-context/`](stage-05-context/).

## Kết quả

Số liệu đo bằng model thật (DeepSeek, OpenAI-compatible API) nằm ở [`submission/measurements.md`](submission/measurements.md).
Phân tích thiết kế, bằng chứng và câu trả lời nằm ở [`submission/analysis.md`](submission/analysis.md).

## Cấu trúc

```
run_case.py                  chạy một session thật qua app.py (Streamlit AppTest), tổng hợp usage, copy trace vào submission/
requirements.txt
stage-05-context/
  agent.py                   tools (hybrid: native + run_python), middleware: limits -> Compaction -> Observer
  compaction.py              B1: Checkpoint schema, selective pre-pass, CompactionMiddleware, archive
  code_runtime/runner.py     B2 phía harness: dispatcher, proxy tools.*, permission, timeout, output cap
  code_runtime/bootstrap.py  B2 phía runtime (process con): SDK tools.*, audit hook chặn truy cập vòng qua proxy
  tools/code_mode.py         tool run_python; description chứa tài liệu SDK tools.*
  tools/files.py             read_file, write_file, list_files (dùng lại từ Buổi 4, runtime gọi cùng hàm _read/_list)
  scripts/gen_logs.py        sinh 10 file log tất định + expected_summary() (đáp án đúng để chấm)
  fixtures/data/logs/        log giả (khoảng 18 KB mỗi file); fixtures/data/src/ mã giả auth.ts, session.ts
  app.py, observer.py        UI + observer (thêm event compaction, panel Compaction, hiển thị code của run_python)
  tests/                     pytest offline (mock model), không cần API key
submission/                  analysis.md, measurements.md, transcripts.md, traces/, runs/, pytest-stage-05-context.txt
```

## Cài đặt

Cần Python 3.11+. Chạy được trên Windows; runtime của Code Mode là Python của project, không cần bash/WSL.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # Linux/macOS: .venv/bin/python
cp stage-05-context/.env.example stage-05-context/.env       # điền OPENAI_API_KEY, MODEL_NAME, OPENAI_BASE_URL
```

Cờ trong `.env` hoặc biến môi trường:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `CODE_MODE` | `1` | `0` = baseline native: agent không có `run_python` (để so sánh B2) |
| `COMPACTION` | `1` | `0` = không compact (để so sánh B1) |
| `COMPACT_TRIGGER_TOKENS` | `12000` | Ngưỡng ước lượng token của message history để kích hoạt compact |

## Chạy

```bash
cd stage-05-context
../.venv/Scripts/python -m streamlit run app.py   # UI chat + panel State & Context (có mục Compaction)
../.venv/Scripts/python -m pytest -v              # test offline
../.venv/Scripts/python scripts/gen_logs.py --check   # in đáp án đúng của bộ log
```

Tái hiện số liệu (từ thư mục gốc repo; cần `.env`):

```bash
# B2: cùng một câu hỏi, native và Code Mode
.venv/Scripts/python run_case.py stage-05-context --env CODE_MODE=0 --env COMPACTION=0 --label b2_native "<câu hỏi B2>"
.venv/Scripts/python run_case.py stage-05-context --env CODE_MODE=1 --env COMPACTION=0 --label b2_codemode "<câu hỏi B2>"
# B1: cùng một session nhiều lượt, không compact và có compact
.venv/Scripts/python run_case.py stage-05-context --env CODE_MODE=0 --env COMPACTION=0 --label b1_no_compaction "<lượt 1>" "<lượt 2>" ...
.venv/Scripts/python run_case.py stage-05-context --env CODE_MODE=0 --env COMPACTION=1 --label b1_compaction "<lượt 1>" "<lượt 2>" ...
```

Câu hỏi và các lượt chat dùng khi đo được ghi đầy đủ trong [`submission/measurements.md`](submission/measurements.md).

## Giới hạn

- Runtime của Code Mode là process con với `-I`, env tối thiểu, cwd tạm và audit hook. Đây **không phải sandbox**:
  chỉ dùng với dữ liệu giả trong lab. Production cần container hoặc isolate không có mạng và file system.
- Token trong event compaction là ước lượng (`count_tokens_approximately`); số liệu so sánh lấy từ `usage` provider trả về.
