"""Cấu hình project: hằng số hiển thị, giới hạn thực thi và settings đọc từ .env."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

import paths

PROJECT_NAME = "stage-05-context"
APP_TITLE = "Stage 05: Context management"
CAPABILITY_TEXT = (
    "Tools: read_file, write_file, list_files, run_python (Code Mode, bật/tắt bằng CODE_MODE). "
    "Compaction: structured checkpoint khi history vượt ngưỡng (bật/tắt bằng COMPACTION)."
)

# Giới hạn số lần gọi model trong một lượt chat (ModelCallLimitMiddleware).
MODEL_CALL_LIMIT = 8
# Giới hạn số tool calls trong một lượt chat (ToolCallLimitMiddleware). Lời gọi tools.* bên trong run_python không tính.
TOOL_CALL_LIMIT = 20
# Giới hạn số bước của LangGraph (recursion limit), không phải số tool calls.
RECURSION_LIMIT = 50

# B2 Code Mode: giới hạn của một lần run_python.
CODE_TIMEOUT_SECONDS = 10
CODE_MAX_TOOL_CALLS = 300
CODE_OUTPUT_CAP_CHARS = 2_000

# B1 Compaction. Ngưỡng là budget của message history (ước lượng token), không phải context window của model:
# compact sớm, không đợi context đầy. Override bằng env COMPACT_TRIGGER_TOKENS.
COMPACT_TRIGGER_TOKENS = 12_000
# Tool output dài hơn mức này bị rút gọn (TRUNCATE) trước khi đưa cho bước tạo checkpoint.
TRUNCATE_MIN_CHARS = 1_500
# Số dòng lỗi chính giữ lại cho mỗi tool output bị rút gọn.
TRUNCATE_KEEP_LINES = 12


@dataclass(frozen=True)
class Settings:
    api_key: str
    model_name: str
    base_url: str | None


@dataclass(frozen=True)
class Features:
    code_mode: bool
    compaction: bool
    compact_trigger_tokens: int


def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "off", "no")


def load_features() -> Features:
    """Đọc cờ tính năng từ môi trường (.env hoặc biến môi trường của process) tại thời điểm gọi."""
    load_dotenv(paths.ENV_PATH, override=False)
    trigger = os.getenv("COMPACT_TRIGGER_TOKENS", "").strip()
    return Features(
        code_mode=_flag("CODE_MODE", True),
        compaction=_flag("COMPACTION", True),
        compact_trigger_tokens=int(trigger) if trigger.isdigit() else COMPACT_TRIGGER_TOKENS,
    )


def load_settings() -> tuple[Settings | None, list[str]]:
    """Đọc .env của project này. Trả (settings, []) hoặc (None, danh sách biến còn thiếu)."""
    load_dotenv(paths.ENV_PATH, override=False)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model_name = os.getenv("MODEL_NAME", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    missing = [name for name, value in (("OPENAI_API_KEY", api_key), ("MODEL_NAME", model_name)) if not value]
    if missing:
        return None, missing
    return Settings(api_key=api_key, model_name=model_name, base_url=base_url), []
