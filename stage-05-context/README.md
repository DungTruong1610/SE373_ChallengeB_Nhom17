# stage-05-context

Agent của lab Buổi 4 (`stage-02-skills`) mở rộng cho Challenge B, buổi 7:

- **B1 Compaction:** `compaction.py` (`CompactionMiddleware`, structured checkpoint, KEEP / TRUNCATE / DROP).
- **B2 Code Mode:** `tools/code_mode.py` (`run_python`) + `code_runtime/` (proxy `tools.*`, permission, timeout, output cap).

Cách cài, chạy và cấu hình (`CODE_MODE`, `COMPACTION`, `COMPACT_TRIGGER_TOKENS`) xem [README gốc](../README.md).
Kết quả và phân tích xem [`../submission/`](../submission/).

```bash
../.venv/Scripts/python -m streamlit run app.py
../.venv/Scripts/python -m pytest -v
```
