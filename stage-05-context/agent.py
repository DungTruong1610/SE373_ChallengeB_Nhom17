"""LangChain agent của stage 05: tools file + run_python (Code Mode), compaction middleware, skill catalog."""

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_openai import ChatOpenAI

import paths
from compaction import CompactionMiddleware
from config import MODEL_CALL_LIMIT, TOOL_CALL_LIMIT, Features, Settings, load_features
from observer import ObserverMiddleware
from prompts import build_system_prompt
from skill_catalog import Catalog, render_catalog, scan_skills
from tools import list_files, read_file, run_python, write_file

NATIVE_TOOLS = [read_file, write_file, list_files]
TOOLS = [*NATIVE_TOOLS, run_python]


def active_tools(features: Features | None = None) -> list:
    """Hybrid: native tools luôn có; run_python chỉ có khi bật CODE_MODE. Baseline native khác đúng một tool."""
    features = features or load_features()
    return TOOLS if features.code_mode else NATIVE_TOOLS


def build_model(settings: Settings) -> ChatOpenAI:
    kwargs = {"model": settings.model_name, "api_key": settings.api_key}
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    return ChatOpenAI(**kwargs)


def load_catalog() -> Catalog:
    return scan_skills(paths.WORKSPACE_DIR)


def system_prompt(features: Features | None = None) -> str:
    features = features or load_features()
    return build_system_prompt(render_catalog(load_catalog()), code_mode=features.code_mode)


def capabilities() -> dict:
    catalog = load_catalog()
    features = load_features()
    return {
        "tools": [t.name for t in active_tools(features)],
        "skills": catalog.metadata(),
        "skill_diagnostics": catalog.diagnostics,
        "features": {
            "code_mode": features.code_mode,
            "compaction": features.compaction,
            "compact_trigger_tokens": features.compact_trigger_tokens,
        },
    }


def build_agent(model, features: Features | None = None):
    features = features or load_features()
    middleware = [
        ModelCallLimitMiddleware(run_limit=MODEL_CALL_LIMIT, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=TOOL_CALL_LIMIT),
    ]
    if features.compaction:
        # before_model chạy trước wrap_model_call của Observer: snapshot chụp request SAU khi compact
        middleware.append(CompactionMiddleware(model, trigger_tokens=features.compact_trigger_tokens))
    middleware.append(ObserverMiddleware())  # cuối danh sách = sát model/tool invocation nhất
    return create_agent(
        model=model,
        tools=active_tools(features),
        system_prompt=system_prompt(features),
        middleware=middleware,
    )
