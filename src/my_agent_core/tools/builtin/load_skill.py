"""Expose a scoped skill catalog through the ordinary tool execution path."""

from collections.abc import Sequence

from my_agent_core.skills import SkillManager
from my_agent_core.tools import DEFAULT_TOOL_MAX_BYTES, DEFAULT_TOOL_MAX_LINES, Tool, ToolResult, tool

LOAD_SKILL_TOOL_NAME = "load_skill"


def make_load_skill_tool(manager: SkillManager, names: Sequence[str] | None = None) -> Tool | None:
    allowed = {skill.name for skill in manager.model_skills(names)}
    if not allowed:
        return None

    @tool(
        name=LOAD_SKILL_TOOL_NAME,
        description=(
            "Load one available skill's full instructions by exact name before using it. "
            "Resolve relative references against the directory containing its SKILL.md."
        ),
        is_parallel_safe=True,
    )
    async def load_skill(name: str) -> ToolResult:
        if name not in allowed:
            return ToolResult(ok=False, error=f"Skill '{name}' is unavailable. Choose a name from the skill catalog.")
        content = manager.format_invocation(name)
        if len(content.encode("utf-8")) > DEFAULT_TOOL_MAX_BYTES or len(content.splitlines()) > DEFAULT_TOOL_MAX_LINES:
            return ToolResult(ok=False, error=f"Skill '{name}' is too large for one tool result. Ask the user to invoke it directly.")
        return ToolResult(ok=True, data=content)

    return load_skill
