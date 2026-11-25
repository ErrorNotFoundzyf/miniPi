"""测试 Subagent 子代理对可用工具集过滤后的专属 <tools> 与 <rules> 提示词生成。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from my_agent_core.registry import ToolRegistry
from my_agent_core.skills import SkillManager
from my_agent_core.subagent_tasks import _filter_tools, _system_for
from my_agent_core.subagents import Subagent
from my_agent_core.tools import Tool
from my_agent_core.tools.builtin.load_skill import make_load_skill_tool


class DummyParent:
    class DummySkillManager:
        def format_prompt(self, skills=None):
            return ""

    skill_manager = DummySkillManager()


def test_subagent_system_for_filtered_tools():
    """验证子代理仅包含其允许使用的工具提示词与规则。"""
    sub = Subagent(
        name="read_only_scout",
        description="Scout codebase without editing",
        content="You are a read-only scout.",
        file_path=Path("<test>"),
        tools=("read", "grep"),
        disallowed_tools=("write", "edit", "bash"),
    )

    def dummy_read(x: str = "") -> str:
        return x

    def dummy_grep(x: str = "") -> str:
        return x

    t_read = Tool(
        dummy_read,
        name="read",
        prompt_snippet="Read file contents",
        prompt_guidelines=["Use read to examine files instead of cat or sed."],
    )
    t_grep = Tool(
        dummy_grep,
        name="grep",
        prompt_snippet="Search file contents",
    )

    filtered_tools = [t_read, t_grep]
    prompt = _system_for(sub, DummyParent(), tools=filtered_tools)

    assert "You are a read-only scout." in prompt
    assert "<tools>" in prompt
    assert "- read: Read file contents" in prompt
    assert "- grep: Search file contents" in prompt
    # 绝不能出现 write 或 edit
    assert "- write:" not in prompt
    assert "- edit:" not in prompt

    assert "<rules>" in prompt
    assert "- Use read to examine files instead of cat or sed." in prompt
    assert "- Use edit for precise changes" not in prompt
    assert "- Use write only for new files" not in prompt


@pytest.mark.anyio
async def test_subagent_cannot_use_parent_skill_loader_outside_its_scope(tmp_path):
    for name in ("review", "other"):
        root = tmp_path / name
        root.mkdir()
        (root / "SKILL.md").write_text(f"---\ndescription: {name}\n---\nInstructions for {name}", encoding="utf-8")
    manager = SkillManager([tmp_path])
    parent = SimpleNamespace(skill_manager=manager, registry=ToolRegistry())
    parent_loader = make_load_skill_tool(manager)
    assert parent_loader is not None
    parent.registry.register(parent_loader)
    sub = Subagent("reviewer", "review", "Review changes", Path("reviewer.md"), skills=("review",))

    tools = _filter_tools(parent, sub)
    assert len(tools) == 1 and tools[0] is not parent_loader
    prompt = _system_for(sub, parent, tools)
    assert "<name>review</name>" in prompt
    assert "<name>other</name>" not in prompt
    allowed = await tools[0].execute({"name": "review"})
    assert allowed.ok and "Instructions for review" in allowed.data
    denied = await tools[0].execute({"name": "other"})
    assert not denied.ok and denied.data is None

    restricted = Subagent(
        "reviewer", "review", "Review changes", Path("reviewer.md"), skills=("review",), disallowed_tools=("load_skill",),
    )
    assert _filter_tools(parent, restricted) == []
    no_skills = Subagent("reviewer", "review", "Review changes", Path("reviewer.md"))
    assert _filter_tools(parent, no_skills) == []
