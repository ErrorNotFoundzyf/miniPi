"""技能数据、目录发现和提示词格式化。

SkillManager 为 Repository 形态：持有 dict 状态，构造即发现，查询/格式化全部
收编为实例方法（对应 pig-mono 的 SkillManager 类；模块级函数版本见历史）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import yaml


@dataclass(frozen=True)
class Skill:
    """已发现的技能，名称来自 frontmatter 或目录名。"""

    name: str
    description: str
    content: str  # frontmatter 之下的正文
    file_path: Path  # 清单不含路径，调用时提供以定位技能资源
    disable_model_invocation: bool = False


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """首部 --- 换行 YAML 换行 --- → (字段 dict, body)。无 frontmatter → ({}, 全文)。
    用 yaml.safe_load；坏 YAML → 降级 ({}, 全文)，不抛不告警。"""
    normalized = text.replace("\r\n", "\n")
    header = "---\n"
    if not normalized.startswith(header):
        return {}, text
    end = normalized.find("\n---\n", len(header))
    if end == -1:
        return {}, text
    block = normalized[len(header) : end]
    body = normalized[end + len("\n---\n") :].strip()
    try:
        fields = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fields, dict):
        return {}, text
    return {str(k): str(v) for k, v in fields.items()}, body


class SkillManager:
    """skill 仓储（Repository）：发现 + 按名索引 + 清单/调用文本生成。

    发现规则（pig-mono 同款）：只扫每个来源目录的一层子目录，认 <name>/SKILL.md；
    name 优先取 frontmatter，缺省用目录名。缺 description → 跳过该 skill。
    隐藏目录（. 开头）跳过。目录不存在 → 静默跳过。容错全静默，不抛不告警。
    """

    def __init__(
        self,
        dirs: Sequence[str | Path] | None = None,
        extra_dirs: Sequence[str | Path] | None = None,
    ):
        """构造即发现：None → 探测 <cwd>/.agents/skills（不存在 → 空，静默）；
        [] → 显式禁用；非空 list → 只扫这些目录。extra_dirs 追加额外发现目录（如 Plugin 解构技能目录）。"""
        self.skills: dict[str, Skill] = {}
        if dirs is None:
            dirs = [Path.cwd() / ".agents" / "skills"]
        for root in dirs:
            self._discover_dir(Path(root))
        if extra_dirs:
            for root in extra_dirs:
                self._discover_dir(Path(root))

    def _discover_dir(self, root: Path) -> None:
        """扫单一来源目录的一层子目录（认 <name>/SKILL.md，或根级 SKILL.md 单技能简写）。"""
        if not root.is_dir():
            return
        # 1. 根级单 SKILL.md 简写支持
        root_skill = root / "SKILL.md"
        if root_skill.is_file():
            skill = self._load_one(root_skill)
            if skill is not None:
                self.skills[skill.name] = skill

        # 2. 一层子目录 <child>/SKILL.md 扫描
        for child in sorted(root.iterdir()): # root.iterdir()遍历 root 目录下的所有直接子项
            if not child.is_dir() or child.name.startswith("."):
                continue
            skill_file = child / "SKILL.md"
            if not skill_file.is_file():
                continue
            skill = self._load_one(skill_file)
            if skill is not None:
                self.skills[skill.name] = skill

    def _load_one(self, path: Path) -> Skill | None:
        """读单个 SKILL.md → Skill；任何失败/缺 description → None（静默）。"""
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return None
        meta, body = parse_frontmatter(text)
        description = meta.get("description")
        if not description:
            return None
        name = (meta.get("name") or "").strip() or path.parent.name
        disabled = meta.get("disable-model-invocation", "").strip().lower() in {"true", "yes", "on", "1"}
        return Skill(name=name, description=description, content=body, file_path=path, disable_model_invocation=disabled)

    def get(self, name: str) -> Skill | None:
        """按名查询（Repository 主查询）。"""
        return self.skills.get(name)

    def list(self) -> list[Skill]:
        """全部 skills，发现顺序。"""
        return list(self.skills.values())

    def __len__(self) -> int:
        return len(self.skills)

    def __contains__(self, name: str) -> bool:
        return name in self.skills

    def model_skills(self, names: Sequence[str] | None = None) -> list[Skill]:
        """返回模型可调用的技能，可用名称进一步限制范围。"""
        if names is None:
            skills = self.skills.values()
        else:
            skills = [self.skills[name] for name in dict.fromkeys(names) if name in self.skills]
        return [skill for skill in skills if not skill.disable_model_invocation]

    def format_prompt(self, names: Sequence[str] | None = None, *, include_load_hint: bool = False) -> str:
        """仅将模型可调用的技能名称和描述放入系统提示词。"""
        skills = self.model_skills(names)
        if not skills:
            return ""
        parts = []
        if include_load_hint:
            parts.append("When a skill matches the task, call load_skill with its exact name before following its instructions.")
        parts.append("<available_skills>")
        for s in skills:
            parts.append("  <skill>")
            parts.append(f"    <name>{escape(s.name)}</name>")
            parts.append(f"    <description>{escape(s.description)}</description>")
            parts.append("  </skill>")
        parts.append("</available_skills>")
        return "\n".join(parts)

    def format_invocation(self, name: str, instructions: str = "") -> str:
        """按名取正文并包装 '<skill name="…" location="…">\\n{content}\\n</skill>'
        + 可选附言（\\n\\n 衔接）。未知名 → ValueError（列可用名字）。"""
        skill = self.get(name)
        if skill is None:
            available = ", ".join(sorted(self.skills)) or "(none)"
            raise ValueError(f"Unknown skill '{name}'. Available: {available}")
        block = f'<skill name={quoteattr(skill.name)} location={quoteattr(str(skill.file_path))}>\n{skill.content}\n</skill>'
        return f"{block}\n\n{instructions}" if instructions else block
