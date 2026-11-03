from __future__ import annotations

from html import escape
from typing import Any


def _field(value: Any, snake: str, camel: str) -> Any:
    if isinstance(value, dict):
        return value.get(snake, value.get(camel))
    return getattr(value, snake, getattr(value, camel, None))


def format_skills_for_system_prompt(skills: list[Any]) -> str:
    visible = [skill for skill in skills if not _field(skill, "disable_model_invocation", "disableModelInvocation")]
    if not visible:
        return ""
    lines = [
        "The following skills provide specialized instructions for specific tasks.",
        "Read the full skill file when the task matches its description.",
        "When a skill file references a relative path, resolve it against the skill directory (parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
        "",
        "<available_skills>",
    ]
    for skill in visible:
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(str(_field(skill, 'name', 'name')), quote=True)}</name>",
                f"    <description>{escape(str(_field(skill, 'description', 'description')), quote=True)}</description>",
                f"    <location>{escape(str(_field(skill, 'file_path', 'filePath')), quote=True)}</location>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines).replace("&#x27;", "&apos;")


formatSkillsForSystemPrompt = format_skills_for_system_prompt

__all__ = ["formatSkillsForSystemPrompt", "format_skills_for_system_prompt"]
