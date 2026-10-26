"""Chạy một cuộc trò chuyện MỚI qua app.py thật của stage để sinh trace với model thật, rồi tổng hợp usage.

Dùng Streamlit AppTest: chạy đúng code của app.py (run_turn, Observer, TraceWriter, agent, middleware, tools),
không mock model, không sửa trace. Mỗi lần chạy script = một session mới = một conversation_id mới.

    .venv/Scripts/python run_case.py stage-05-context [--env CODE_MODE=0] [--label b2_native] "Lượt 1" ["Lượt 2" ...]

--env KEY=VAL   đặt biến môi trường trước khi chạy app (ưu tiên hơn .env), ví dụ CODE_MODE, COMPACTION, COMPACT_TRIGGER_TOKENS.
--label NAME    copy trace của lần chạy vào submission/traces/NAME__turnNN.jsonl và ghi tóm tắt usage vào
                submission/runs/NAME.json.
Cần .env của stage (OPENAI_API_KEY, MODEL_NAME, tùy chọn OPENAI_BASE_URL). Script không đọc hay in .env.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent
SUBMISSION = ROOT / "submission"
TIMEOUT = 900


def print_turn(observer, turn) -> None:
    print(f"=== conversation {observer.conversation_id[:8]} | lượt {turn['chat_turn']} | run {turn['run_id'][:8]}")
    print(f"USER: {turn['user']}")
    for event in observer.events:
        if event["run_id"] != turn["run_id"]:
            continue
        kind = event["type"]
        if kind == "model_response":
            usage = event.get("usage") or {}
            print(f"  #{event['sequence']} model call {event['model_call_index']}: {usage.get('input_tokens')} in / {usage.get('output_tokens')} out")
        elif kind == "tool_started":
            args = json.dumps(event["arguments"], ensure_ascii=False)
            print(f"  #{event['sequence']} tool_started {event['tool_name']} {args[:160]}{'…' if len(args) > 160 else ''}")
        elif kind == "tool_finished":
            result = event.get("result")
            ok = result.get("ok") if isinstance(result, dict) else None
            extra = ""
            if event["tool_name"] == "run_python" and isinstance(result, dict):
                extra = f" proxy_calls={result.get('proxy_calls')} runtime_bytes={result.get('runtime_bytes')} stdout_chars={len(result.get('stdout') or '')}"
            print(f"  #{event['sequence']} tool_finished {event['tool_name']} ok={ok}{extra}")
        elif kind == "compaction":
            print(f"  #{event['sequence']} COMPACTION #{event['number']}: ~{event['before_tokens']} -> ~{event['after_tokens']} tokens, "
                  f"{event['before_messages']} -> {event['after_messages']} messages, summarizer usage {event.get('summarizer_usage')}")
        elif kind == "compaction_failed":
            print(f"  #{event['sequence']} COMPACTION FAILED: {event.get('reason')}")


def summarize(observer) -> dict:
    """Usage theo từng model call (từ snapshot) + compaction. Chỉ dùng số provider trả về, không ước lượng."""
    calls = []
    for snap in observer.snapshots:
        usage = snap.get("usage") or {}
        calls.append(
            {
                "turn": snap["chat_turn"],
                "call": snap["model_call_index"],
                "messages": snap["message_count"],
                "chars": snap["system_prompt_chars"] + snap["message_chars"],
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read": (usage.get("input_token_details") or {}).get("cache_read"),
            }
        )
    compactions = [c for c in observer.compactions]
    summarizer_in = sum((c.get("summarizer_usage") or {}).get("input_tokens", 0) for c in compactions)
    summarizer_out = sum((c.get("summarizer_usage") or {}).get("output_tokens", 0) for c in compactions)
    tool_events = [e for e in observer.events if e["type"] == "tool_finished"]
    return {
        "conversation_id": observer.conversation_id,
        "model_calls": len(calls),
        "tool_calls": len(tool_events),
        "tool_calls_by_name": {name: sum(1 for e in tool_events if e["tool_name"] == name) for name in sorted({e["tool_name"] for e in tool_events})},
        "total_input_tokens": sum(c["input_tokens"] or 0 for c in calls),
        "total_output_tokens": sum(c["output_tokens"] or 0 for c in calls),
        "max_input_tokens": max((c["input_tokens"] or 0 for c in calls), default=0),
        "summarizer_input_tokens": summarizer_in,
        "summarizer_output_tokens": summarizer_out,
        "calls": calls,
        "compactions": compactions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage")
    parser.add_argument("messages", nargs="+")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VAL")
    parser.add_argument("--label")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    for item in args.env:
        key, _, value = item.partition("=")
        os.environ[key] = value
    stage = ROOT / args.stage
    sys.path.insert(0, str(stage))  # app.py import paths, agent, trace... của chính stage
    traces_dir = stage / "traces"
    before = set(traces_dir.glob("*.jsonl"))

    at = AppTest.from_file(str(stage / "app.py"), default_timeout=TIMEOUT).run()
    if at.exception:
        print(f"App lỗi khi render: {at.exception}")
        return 1
    if at.chat_input[0].disabled:
        print("Ô chat bị khóa: thiếu cấu hình model trong .env của stage.")
        return 1

    for text in args.messages:
        at.chat_input[0].set_value(text).run()
        if at.exception:
            print(f"App lỗi: {at.exception}")
            return 1
        turn = at.session_state["ui_history"][-1]
        observer = at.session_state["observer"]
        print_turn(observer, turn)
        if turn["error"]:
            print(f"LỖI: {turn['error']}")
            return 1
        print(f"ASSISTANT:\n{turn['answer']}\n")

    observer = at.session_state["observer"]
    summary = summarize(observer)
    summary.update({"label": args.label, "env": args.env, "messages": args.messages,
                    "answers": [t["answer"] for t in at.session_state["ui_history"]]})
    print("=== usage")
    for call in summary["calls"]:
        print(f"  lượt {call['turn']} call {call['call']}: {call['messages']} messages, {call['chars']} ký tự, "
              f"{call['input_tokens']} in / {call['output_tokens']} out (cache_read {call['cache_read']})")
    print(f"  TỔNG: {summary['model_calls']} model call, {summary['tool_calls']} tool call {summary['tool_calls_by_name']}, "
          f"{summary['total_input_tokens']} in / {summary['total_output_tokens']} out, max input {summary['max_input_tokens']}, "
          f"summarizer {summary['summarizer_input_tokens']} in / {summary['summarizer_output_tokens']} out")

    new_traces = sorted(set(traces_dir.glob("*.jsonl")) - before)
    for path in new_traces:
        print(f"TRACE: {path.relative_to(ROOT).as_posix()}")
    if args.label:
        (SUBMISSION / "traces").mkdir(parents=True, exist_ok=True)
        (SUBMISSION / "runs").mkdir(parents=True, exist_ok=True)
        summary["traces"] = []
        for index, path in enumerate(new_traces, start=1):
            target = SUBMISSION / "traces" / f"{args.label}__turn{index:02d}.jsonl"
            shutil.copyfile(path, target)
            summary["traces"].append(target.relative_to(ROOT).as_posix())
        for compaction in summary["compactions"]:
            if compaction.get("archive"):
                source = traces_dir / compaction["archive"]
                target = SUBMISSION / "traces" / f"{args.label}__compact{compaction['number']:02d}_archive.jsonl"
                if source.is_file():
                    shutil.copyfile(source, target)
                    compaction["archive_copy"] = target.relative_to(ROOT).as_posix()
        out = SUBMISSION / "runs" / f"{args.label}.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"SUMMARY: {out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
