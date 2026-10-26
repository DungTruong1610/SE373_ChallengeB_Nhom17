"""B1 Compaction: selective pre-pass, điểm cắt an toàn, structured checkpoint, fallback và luồng qua app."""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from streamlit.testing.v1 import AppTest

import agent
import paths
from agent import build_agent
from compaction import (
    Checkpoint,
    find_split,
    is_checkpoint,
    parse_checkpoint,
    render_checkpoint,
    selective_prepass,
    truncate_tool_output,
    validate_checkpoint,
)
from config import Features
from tests.conftest import ScriptedChatModel

CHECKPOINT_ARGS = {
    "goal": "Điều tra ERR_TOKEN_REFRESH_RACE ở /api/auth/refresh",
    "current_state": "Đã đọc log 2026-10-11 và 2026-10-12",
    "constraints": ["Giữ API backward compatible"],
    "decisions": [],
    "findings": ["Lỗi xảy ra khi 2 refresh cùng session (WARN REFRESH_CONCURRENT ngay trước)"],
    "failed_attempts": ["Tăng token TTL 900s -> 1800s không giảm lỗi"],
    "unresolved_errors": ["ERR_TOKEN_REFRESH_RACE status=500"],
    "files": ["data/src/auth.ts", "data/src/session.ts"],
    "next_action": "Đọc data/src/session.ts, kiểm tra lock quanh rotateRefreshToken",
}


def checkpoint_reply(**overrides):
    """Summarizer trả JSON theo schema (kèm fence markdown như model thật hay làm)."""
    body = json.dumps({**CHECKPOINT_ARGS, **overrides}, ensure_ascii=False)
    return AIMessage(content=f"```json\n{body}\n```")


def log_text(name="app-2026-10-11.log"):
    return (paths.FIXTURES_DIR / "data" / "logs" / name).read_text(encoding="utf-8")


def read_result(call_id, name="app-2026-10-11.log"):
    content = json.dumps({"ok": True, "path": f"data/logs/{name}", "content": log_text(name)}, ensure_ascii=False)
    return ToolMessage(content=content, tool_call_id=call_id, name="read_file")


def read_call(call_id, name="app-2026-10-11.log"):
    return AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": f"data/logs/{name}"}, "id": call_id}])


def long_history():
    """Lượt 1: đọc 2 log lớn; lượt 2: user báo đã thử tăng TTL. Lượt hiện tại: câu hỏi mới."""
    return [
        HumanMessage(content="Điều tra ERR_TOKEN_REFRESH_RACE. Ràng buộc: giữ API backward compatible."),
        read_call("r1"),
        read_result("r1"),
        read_call("r2", "app-2026-10-12.log"),
        read_result("r2", "app-2026-10-12.log"),
        AIMessage(content="Lỗi đi kèm WARN REFRESH_CONCURRENT cùng session."),
        HumanMessage(content="Đã thử tăng TTL lên 1800s, không giúp."),
        AIMessage(content="Ghi nhận."),
        HumanMessage(content="Đang điều tra lỗi gì, đã thử gì, bước tiếp là gì?"),
    ]


def stream(agent_, messages):
    events, updates = [], []
    for mode, chunk in agent_.stream({"messages": messages}, stream_mode=["updates", "custom"]):
        if mode == "custom" and chunk.get("observer"):
            events.append(chunk)
        elif mode == "updates":
            updates.append(chunk)
    return events, updates


def features(trigger, code_mode=True):
    return Features(code_mode=code_mode, compaction=True, compact_trigger_tokens=trigger)


# ---------------------------------------------------------------- unit


def test_truncate_keeps_key_error_lines_from_json_tool_result(lab_dirs):
    content = read_result("r1").content
    short, cut = truncate_tool_output(content, keep_lines=5)
    assert cut is True and len(short) < 1_500
    lines = short.splitlines()
    assert lines[0].startswith("[TRUNCATE: giữ 5/")
    assert all(("ERROR" in line or "WARN" in line) for line in lines[1:])
    assert truncate_tool_output('{"ok": true, "content": "ngắn"}') == ('{"ok": true, "content": "ngắn"}', False)


def test_prepass_keeps_users_truncates_outputs_and_drops_duplicate_calls(lab_dirs):
    messages = [
        HumanMessage(content="Ràng buộc: giữ API backward compatible."),
        read_call("r1"),
        read_result("r1"),
        read_call("r2"),  # đọc lại đúng file đó: trùng tool + args
        read_result("r2"),
    ]
    pruned, stats = selective_prepass(messages)
    assert pruned[0].content == messages[0].content
    assert [m.tool_call_id for m in pruned if isinstance(m, ToolMessage)] == ["r2"]
    assert stats == {"kept": 1, "truncated": 1, "dropped": 2}
    assert len(pruned[-1].content) < len(messages[-1].content) // 5


def test_find_split_keeps_current_question_and_latest_tool_round():
    history = long_history()
    assert find_split(history) == (8, 8)  # chưa có vòng tool nào trong lượt hiện tại
    history += [read_call("r3", "app-2026-10-13.log"), ToolMessage(content="{}", tool_call_id="r3", name="read_file")]
    assert find_split(history) == (9, 8)  # giữ cặp tool_call/tool_result mới nhất cùng nhau
    assert find_split([AIMessage(content="x")]) == (1, None)


def test_render_and_validate_checkpoint():
    checkpoint = Checkpoint(**CHECKPOINT_ARGS)
    text = render_checkpoint(checkpoint, 2)
    assert text.startswith("[CHECKPOINT #2]")
    assert "failed_attempts:\n- Tăng token TTL" in text and "next_action: Đọc data/src/session.ts" in text
    assert "decisions: []" in text
    assert validate_checkpoint(checkpoint) is None
    assert validate_checkpoint(Checkpoint(**{**CHECKPOINT_ARGS, "next_action": " "})) == "checkpoint thiếu next_action"
    assert validate_checkpoint(None) is not None


def test_parse_checkpoint_from_model_text():
    text = "Đây là checkpoint:\n```json\n" + json.dumps(CHECKPOINT_ARGS, ensure_ascii=False) + "\n```"
    assert parse_checkpoint(text) == Checkpoint(**CHECKPOINT_ARGS)
    assert parse_checkpoint("không có JSON") is None


# ---------------------------------------------------------------- middleware


def test_no_compaction_below_threshold(lab_dirs):
    model = ScriptedChatModel(responses=[AIMessage(content="Trả lời")])
    events, _ = stream(build_agent(model, features(trigger=10_000_000)), long_history())
    assert [e["event"] for e in events if e["event"].startswith("compaction")] == []
    assert len(model.received) == 1 and len(model.received[0]) == 1 + len(long_history())


def test_compaction_replaces_history_with_checkpoint(lab_dirs):
    model = ScriptedChatModel(responses=[checkpoint_reply(), AIMessage(content="Đang điều tra ERR_TOKEN_REFRESH_RACE...")])
    events, updates = stream(build_agent(model, features(trigger=4_000)), long_history())

    summarizer_input, main_input = model.received
    assert '"next_action"' in summarizer_input[0].content  # JSON Schema nằm trong prompt của summarizer
    assert "TRUNCATE" in summarizer_input[1].content  # pre-pass chạy trước khi gửi summarizer
    assert "Giữ API backward compatible" in summarizer_input[1].content or "backward compatible" in summarizer_input[1].content

    roles = [m.type for m in main_input]
    assert roles == ["system", "human", "human"]  # system prompt + checkpoint + câu hỏi hiện tại
    assert main_input[1].content.startswith("[CHECKPOINT #1]")
    assert "Tăng token TTL 900s -> 1800s không giảm lỗi" in main_input[1].content
    assert main_input[2].content == "Đang điều tra lỗi gì, đã thử gì, bước tiếp là gì?"
    assert "REFRESH_CONCURRENT endpoint" not in "".join(str(m.content) for m in main_input)  # raw log đã rời context

    compaction = next(e["data"] for e in events if e["event"] == "compaction")
    assert compaction["number"] == 1
    assert compaction["before_tokens"] > 4_000 > compaction["after_tokens"]
    assert (compaction["before_messages"], compaction["after_messages"]) == (9, 2)
    assert compaction["checkpoint"]["next_action"] == CHECKPOINT_ARGS["next_action"]
    archive = paths.TRACES_DIR / compaction["archive"]
    assert archive.is_file() and len(archive.read_text(encoding="utf-8").splitlines()) == 8

    removal = next(u for u in updates if any(k.startswith("CompactionMiddleware") for k in u))
    assert removal  # update có RemoveMessage(REMOVE_ALL) + history mới, app dùng add_messages để áp


def test_mid_turn_compaction_keeps_unseen_tool_round_and_pairs(lab_dirs):
    history = long_history() + [read_call("r3", "app-2026-10-13.log"), read_result("r3", "app-2026-10-13.log")]
    model = ScriptedChatModel(responses=[checkpoint_reply(), AIMessage(content="Xong.")])
    stream(build_agent(model, features(trigger=4_000)), history)
    main_input = model.received[1]
    assert [m.type for m in main_input] == ["system", "human", "human", "ai", "tool"]
    assert main_input[3].tool_calls[0]["id"] == "r3" and main_input[4].tool_call_id == "r3"
    assert "app-2026-10-13" in main_input[4].content  # vòng tool mới nhất (model chưa xem) giữ nguyên


def test_second_compaction_updates_previous_checkpoint(lab_dirs):
    first = ScriptedChatModel(responses=[checkpoint_reply(), AIMessage(content="Trả lời 1")])
    agent_ = build_agent(first, features(trigger=4_000))
    history = long_history()
    _, updates = stream(agent_, history)
    compacted = first.received[1][1:] + [AIMessage(content="Trả lời 1")]
    assert is_checkpoint(compacted[0])

    more = compacted + [HumanMessage(content="Đọc tiếp log 2026-10-13."), read_call("r3", "app-2026-10-13.log"),
                        read_result("r3", "app-2026-10-13.log"), AIMessage(content="Đã đọc."), HumanMessage(content="Bước tiếp?")]
    second = ScriptedChatModel(responses=[checkpoint_reply(current_state="Đã đọc 3 log"), AIMessage(content="Trả lời 2")])
    events, _ = stream(build_agent(second, features(trigger=4_000)), more)
    assert "[checkpoint cũ]" in second.received[0][1].content  # incremental: checkpoint cũ là input của lần sau
    assert second.received[1][1].content.startswith("[CHECKPOINT #2]")
    assert sum(1 for m in second.received[1] if is_checkpoint(m)) == 1


def test_failed_checkpoint_keeps_history_unchanged(lab_dirs):
    model = ScriptedChatModel(responses=[AIMessage(content="không theo schema"), AIMessage(content="Trả lời")])
    events, _ = stream(build_agent(model, features(trigger=4_000)), long_history())
    failed = [e["data"] for e in events if e["event"] == "compaction_failed"]
    assert len(failed) == 1 and "schema" in failed[0]["reason"]
    assert not any(e["event"] == "compaction" for e in events)
    assert len(model.received[1]) == 1 + len(long_history())  # request vẫn đủ history gốc

    missing = ScriptedChatModel(responses=[checkpoint_reply(next_action=""), AIMessage(content="Trả lời")])
    events, _ = stream(build_agent(missing, features(trigger=4_000)), long_history())
    assert [e["data"]["reason"] for e in events if e["event"] == "compaction_failed"] == ["checkpoint thiếu next_action"]

    invalid = ScriptedChatModel(responses=[AIMessage(content='{"goal": "x"}'), AIMessage(content="Trả lời")])
    events, _ = stream(build_agent(invalid, features(trigger=4_000)), long_history())
    reasons = [e["data"]["reason"] for e in events if e["event"] == "compaction_failed"]
    assert len(reasons) == 1 and reasons[0].startswith("ValidationError")


# ---------------------------------------------------------------- app


def test_app_applies_compaction_to_history_for_next_turns(configured_env, monkeypatch):
    monkeypatch.setenv("COMPACT_TRIGGER_TOKENS", "4000")
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "data/logs/app-2026-10-11.log"}, "id": "a1"},
                                              {"name": "read_file", "args": {"path": "data/logs/app-2026-10-12.log"}, "id": "a2"}]),
            AIMessage(content="Lỗi đi kèm REFRESH_CONCURRENT."),
            checkpoint_reply(),  # lượt 2: history vượt ngưỡng -> compact trước model call đầu tiên
            AIMessage(content="Đang điều tra ERR_TOKEN_REFRESH_RACE; đã thử tăng TTL; bước tiếp: đọc session.ts."),
            AIMessage(content="Lượt 3 trả lời."),
        ]
    )
    monkeypatch.setattr(agent, "build_model", lambda settings: model)
    at = AppTest.from_file(str(paths.PROJECT_ROOT / "app.py"), default_timeout=30).run()
    for text in ("Điều tra ERR_TOKEN_REFRESH_RACE trong 2 log đầu.", "Đang điều tra lỗi gì, đã thử gì, bước tiếp là gì?"):
        at.chat_input[0].set_value(text).run()
        assert not at.exception, at.exception

    history = at.session_state["model_history"]
    assert [m.type for m in history] == ["human", "human", "ai"]
    assert history[0].content.startswith("[CHECKPOINT #1]")
    turn = at.session_state["ui_history"][-1]
    assert turn["answer"].startswith("Đang điều tra ERR_TOKEN_REFRESH_RACE")
    assert [e["type"] for e in turn["events"]][:3] == ["user_submitted", "compaction", "model_request"]
    observer = at.session_state["observer"]
    assert observer.counters()["Compaction trong conversation"] == 1
    assert any(e.label.startswith("Compaction (1)") for e in at.expander)

    at.chat_input[0].set_value("Tiếp tục.").run()
    assert [m.type for m in model.received[-1]] == ["system", "human", "human", "ai", "human"]  # lượt 3 dùng history đã compact
