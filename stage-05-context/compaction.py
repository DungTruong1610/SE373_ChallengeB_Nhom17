"""B1 Compaction: structured checkpoint khi message history vượt ngưỡng.

Compaction là checkpoint cho hành động tiếp theo, không phải bản recap của chat (slide "Compaction").
Một lần compact (CompactionMiddleware.before_model, chạy ngay trước model call):
1. Khi nào: ước lượng token của messages >= ngưỡng (budget riêng, không đợi context window đầy).
2. Chia history: phần cũ -> tạo checkpoint; giữ nguyên câu hỏi hiện tại của user và vòng tool call mới nhất
   (model chưa xem kết quả của vòng này). Điểm cắt không tách cặp tool_call / tool_result.
3. Selective pre-pass (tất định) cho phần cũ: KEEP message của user và checkpoint cũ, TRUNCATE tool output dài
   còn vài dòng lỗi chính, DROP lời gọi tool trùng lặp.
4. LLM điền schema Checkpoint (goal, constraints, findings, failed_attempts, next_action...). Thiếu goal hoặc
   next_action, hay LLM lỗi: KHÔNG compact (giữ history nguyên vẹn an toàn hơn checkpoint hỏng).
5. Thay history: [checkpoint] + phần giữ nguyên. System prompt không đổi. Phần cũ được archive ra traces/archive/.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.config import get_stream_writer
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from pydantic import BaseModel, Field

import paths
from config import TRUNCATE_KEEP_LINES, TRUNCATE_MIN_CHARS

CHECKPOINT_ID_PREFIX = "checkpoint-"
KEY_LINE_RE = re.compile(r"\b(ERROR|FAIL(ED)?|Traceback|Exception|WARN)\b|\"ok\": false|ERR_[A-Z0-9_]+", re.IGNORECASE)


class Checkpoint(BaseModel):
    """Trạng thái làm việc đủ để agent tiếp tục task chính xác sau khi history cũ bị bỏ."""

    goal: str = Field(description="Mục tiêu hiện tại của user, một câu cụ thể (gồm mã lỗi/endpoint nếu có).")
    current_state: str = Field(description="Đang ở bước nào, đã xong gì, đang dở gì.")
    constraints: list[str] = Field(default_factory=list, description="Ràng buộc/yêu cầu bắt buộc của user, giữ nguyên ý.")
    decisions: list[str] = Field(default_factory=list, description="Quyết định đã chốt (thiết kế, phạm vi, quy ước).")
    findings: list[str] = Field(default_factory=list, description="Phát hiện có bằng chứng: số liệu, dòng log chính, file:dòng.")
    failed_attempts: list[str] = Field(default_factory=list, description="Cách đã thử nhưng không hiệu quả và vì sao, để không lặp lại.")
    unresolved_errors: list[str] = Field(default_factory=list, description="Lỗi chưa giải quyết (mã lỗi, endpoint, triệu chứng).")
    files: list[str] = Field(default_factory=list, description="File liên quan (đường dẫn tương đối workspace).")
    next_action: str = Field(description="Bước kế tiếp cụ thể agent sẽ làm.")


SUMMARIZER_PROMPT = """Bạn là bộ phận compaction của agent harness. Từ history bên dưới, điền checkpoint để agent TIẾP TỤC công việc chính xác.
Checkpoint không phải bản kể lại hội thoại; chỉ giữ thông tin cần cho hành động tiếp theo.

- KEEP: mục tiêu; ràng buộc và yêu cầu bắt buộc của user (giữ nguyên ý); quyết định đã chốt; lỗi chưa giải quyết (mã lỗi, endpoint, số liệu);
  cách đã thử và kết quả, đặc biệt cách thất bại; file liên quan; bước kế tiếp.
- TRUNCATE: output dài chỉ còn vài dòng lỗi chính hoặc số liệu tổng hợp.
- DROP: log trùng lặp, thử nghiệm thất bại đã có kết luận (chỉ giữ kết luận), dữ liệu dễ tái tạo bằng cách đọc lại file.
- Chỉ dùng thông tin có trong history; không suy đoán thêm. Nếu có checkpoint cũ, cập nhật nó: giữ mục còn đúng, ghi rõ mục đã bị thay thế.
- Viết tiếng Việt, ngắn gọn, giữ nguyên mã lỗi, tên file, số liệu."""

SCHEMA_INSTRUCTION = (
    "\n\nChỉ trả về DUY NHẤT một JSON object hợp lệ theo JSON Schema dưới đây, không kèm giải thích hay markdown:\n"
    + json.dumps(Checkpoint.model_json_schema(), ensure_ascii=False)
)


def parse_checkpoint(text: str) -> Checkpoint | None:
    """Lấy JSON object đầu-cuối trong câu trả lời (chịu được ```json fence) rồi validate bằng schema. Không có JSON: None."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    return Checkpoint.model_validate_json(text[start : end + 1])


# ---------------------------------------------------------------- selective pre-pass


def is_checkpoint(message: AnyMessage) -> bool:
    return isinstance(message, HumanMessage) and str(message.id or "").startswith(CHECKPOINT_ID_PREFIX)


def checkpoint_number(message: AnyMessage) -> int:
    match = re.search(r"(\d+)$", str(message.id or ""))
    return int(match.group(1)) if match else 0


def _text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)


def _payload_text(content: str) -> str:
    """Tool result trong lab là JSON; lấy phần văn bản (content/stdout) để lọc dòng, không lọc trên chuỗi đã escape."""
    try:
        data = json.loads(content)
    except ValueError:
        return content
    if isinstance(data, dict):
        parts = [str(data[k]) for k in ("content", "stdout", "stderr") if isinstance(data.get(k), str) and data[k]]
        if parts:
            return "\n".join(parts)
        if isinstance(data.get("entries"), list):
            return "\n".join(str(e.get("path", e)) if isinstance(e, dict) else str(e) for e in data["entries"])
    return content


def truncate_tool_output(content: str, keep_lines: int = TRUNCATE_KEEP_LINES, min_chars: int = TRUNCATE_MIN_CHARS) -> tuple[str, bool]:
    """TRUNCATE: output dài chỉ còn vài dòng lỗi chính (hoặc vài dòng đầu nếu không có dòng lỗi)."""
    if len(content) <= min_chars:
        return content, False
    lines = _payload_text(content).splitlines()
    key_lines = [line for line in lines if KEY_LINE_RE.search(line)]
    picked = (key_lines or lines)[:keep_lines]
    label = "dòng lỗi chính" if key_lines else "dòng đầu"
    note = f"[TRUNCATE: giữ {len(picked)}/{len(lines)} {label}; gốc {len(content)} ký tự, có {len(key_lines)} dòng khớp mẫu lỗi]"
    return "\n".join([note, *picked]), True


def selective_prepass(messages: list[AnyMessage]) -> tuple[list[AnyMessage], dict[str, int]]:
    """KEEP / TRUNCATE / DROP tất định trên phần history sắp được tóm tắt. Trả (messages mới, thống kê)."""
    stats = {"kept": 0, "truncated": 0, "dropped": 0}
    # DROP: cùng tool + cùng args gọi nhiều lần, chỉ giữ lần cuối
    last_call: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                last_call[json.dumps([call.get("name"), call.get("args")], sort_keys=True, ensure_ascii=False)] = call.get("id")
    keep_ids = set(last_call.values())

    result: list[AnyMessage] = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            calls = [c for c in message.tool_calls if c.get("id") in keep_ids]
            stats["dropped"] += len(message.tool_calls) - len(calls)
            if not calls and not message.text:
                continue
            result.append(AIMessage(content=message.content, tool_calls=calls, id=message.id))
        elif isinstance(message, ToolMessage):
            if message.tool_call_id not in keep_ids:
                stats["dropped"] += 1
                continue
            content, cut = truncate_tool_output(_text(message.content))
            stats["truncated" if cut else "kept"] += 1
            result.append(ToolMessage(content=content, tool_call_id=message.tool_call_id, name=message.name, id=message.id))
        else:  # KEEP: user, checkpoint cũ, câu trả lời của assistant
            stats["kept"] += 1
            result.append(message)
    return result, stats


def find_split(messages: list[AnyMessage]) -> tuple[int, int | None]:
    """(preserve_from, last_user_index). Giữ nguyên từ vòng tool call mới nhất của lượt hiện tại;
    câu hỏi hiện tại của user được giữ riêng. Không tách cặp tool_call / tool_result."""
    last_user = next((i for i in range(len(messages) - 1, -1, -1)
                      if isinstance(messages[i], HumanMessage) and not is_checkpoint(messages[i])), None)
    if last_user is None:
        return len(messages), None
    preserve_from = last_user
    for i in range(len(messages) - 1, last_user, -1):
        if isinstance(messages[i], AIMessage) and messages[i].tool_calls:
            preserve_from = i
            break
    return preserve_from, last_user


def render_transcript(messages: list[AnyMessage]) -> str:
    rows = []
    for message in messages:
        if is_checkpoint(message):
            rows.append(f"[checkpoint cũ]\n{_text(message.content)}")
        elif isinstance(message, HumanMessage):
            rows.append(f"[user]\n{_text(message.content)}")
        elif isinstance(message, AIMessage):
            calls = "".join(f"\n-> tool call {c['name']} {json.dumps(c.get('args', {}), ensure_ascii=False)}" for c in message.tool_calls)
            rows.append(f"[assistant]\n{message.text}{calls}".rstrip())
        elif isinstance(message, ToolMessage):
            rows.append(f"[tool result {message.name}]\n{_text(message.content)}")
    return "\n\n".join(rows)


def render_checkpoint(checkpoint: Checkpoint, number: int) -> str:
    """Định dạng giống slide "Structured checkpoint": dễ đọc với cả model và người."""
    lines = [
        f"[CHECKPOINT #{number}] History trước đó đã được harness compact. Đây là trạng thái làm việc để tiếp tục task;",
        "dữ liệu gốc vẫn đọc lại được từ các file trong workspace nếu cần.",
        f"goal: {checkpoint.goal}",
        f"current_state: {checkpoint.current_state}",
    ]
    for field in ("constraints", "decisions", "findings", "failed_attempts", "unresolved_errors"):
        items = getattr(checkpoint, field)
        lines.append(f"{field}:" + ("" if items else " []"))
        lines.extend(f"- {item}" for item in items)
    lines.append(f"files: [{', '.join(checkpoint.files)}]")
    lines.append(f"next_action: {checkpoint.next_action}")
    return "\n".join(lines)


def validate_checkpoint(checkpoint: Checkpoint | None) -> str | None:
    if checkpoint is None:
        return "LLM không trả checkpoint theo schema"
    missing = [name for name in ("goal", "next_action") if not getattr(checkpoint, name).strip()]
    return f"checkpoint thiếu {', '.join(missing)}" if missing else None


def archive_messages(messages: list[AnyMessage], number: int, archive_dir: Path) -> Path:
    """Raw history bị bỏ khỏi context được archive ra đĩa (không gửi model), để debug hoặc audit."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:8]}_compact{number:02d}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(message.model_dump(), ensure_ascii=False, default=str) + "\n")
    return path


# ---------------------------------------------------------------- middleware


class CompactionMiddleware(AgentMiddleware):
    """Compact message history thành structured checkpoint khi vượt ngưỡng; chạy trước mỗi model call."""

    def __init__(self, model, trigger_tokens: int):
        super().__init__()
        self.model = model
        self.trigger_tokens = trigger_tokens

    def before_model(self, state, runtime) -> dict[str, Any] | None:
        messages: list[AnyMessage] = state["messages"]
        before_tokens = count_tokens_approximately(messages)
        if before_tokens < self.trigger_tokens:
            return None
        preserve_from, last_user = find_split(messages)
        to_summarize = [m for i, m in enumerate(messages[:preserve_from]) if i != last_user]
        if all(is_checkpoint(m) for m in to_summarize):
            return None  # chỉ còn checkpoint + vòng hiện tại: không có gì mới để compact

        writer = get_stream_writer()
        number = max((checkpoint_number(m) for m in messages if is_checkpoint(m)), default=0) + 1
        pruned, prepass = selective_prepass(to_summarize)
        try:
            checkpoint, usage = self.summarize(pruned)
        except Exception as exc:  # provider lỗi, schema sai: không compact
            checkpoint, usage, failure = None, None, f"{type(exc).__name__}: {exc}"
        else:
            failure = validate_checkpoint(checkpoint)
        if failure:
            writer({"observer": True, "event": "compaction_failed",
                    "data": {"number": number, "reason": failure, "before_tokens": before_tokens}})
            return None

        for message in messages:  # add_messages cần id ổn định cho message được giữ lại
            if message.id is None:
                message.id = str(uuid.uuid4())
        checkpoint_message = HumanMessage(content=render_checkpoint(checkpoint, number), id=f"{CHECKPOINT_ID_PREFIX}{number}")
        preserved = ([messages[last_user]] if last_user is not None and last_user < preserve_from else []) + messages[preserve_from:]
        new_messages = [checkpoint_message, *preserved]
        archive = archive_messages(to_summarize, number, paths.TRACES_DIR / "archive")
        writer(
            {
                "observer": True,
                "event": "compaction",
                "data": {
                    "number": number,
                    "trigger_tokens": self.trigger_tokens,
                    "before_tokens": before_tokens,
                    "after_tokens": count_tokens_approximately(new_messages),
                    "before_messages": len(messages),
                    "after_messages": len(new_messages),
                    "summarized_messages": len(to_summarize),
                    "preserved_messages": len(preserved),
                    "prepass": prepass,
                    "summarizer_usage": usage,
                    "checkpoint": checkpoint.model_dump(),
                    "archive": archive.relative_to(paths.TRACES_DIR).as_posix(),
                },
            }
        )
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]}

    def summarize(self, messages: list[AnyMessage]) -> tuple[Checkpoint | None, dict | None]:
        """Một model call riêng (không qua agent loop) điền schema Checkpoint; trả kèm usage để tính chi phí compaction.

        Schema nằm trong prompt và harness tự validate bằng Pydantic, không dùng tool_choice bắt buộc:
        một số provider (ví dụ DeepSeek ở thinking mode) từ chối tool_choice nên with_structured_output lỗi 400."""
        reply = self.model.invoke([SystemMessage(content=SUMMARIZER_PROMPT + SCHEMA_INSTRUCTION), HumanMessage(content=render_transcript(messages))])
        usage = dict(reply.usage_metadata) if isinstance(reply, AIMessage) and reply.usage_metadata else None
        return parse_checkpoint(reply.text if isinstance(reply, AIMessage) else str(reply)), usage
