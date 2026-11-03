from __future__ import annotations

import os
from pathlib import Path

from aster_harness.harness.env import NodeExecutionEnv
from aster_harness.harness.prompt_templates import (
    format_prompt_template_invocation,
    load_prompt_templates,
    load_sourced_prompt_templates,
    parse_command_args,
)
from aster_harness.harness.skills import format_skill_invocation, load_skills, load_sourced_skills
from aster_harness.harness.system_prompt import format_skills_for_system_prompt
from aster_harness.harness.types import get_or_throw


def test_zero_positional_argument_does_not_wrap_to_last_python_list_item() -> None:
    from aster_harness.harness.prompt_templates import substitute_args

    assert substitute_args("$0 $1 $2", ["first", "second"]) == " first second"


async def test_prompt_templates_and_symlink(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    get_or_throw(await env.create_dir("a/nested"))
    get_or_throw(await env.write_file("a/one.md", "---\ndescription: One template\n---\nHello $1"))
    get_or_throw(await env.write_file("a/nested/ignored.md", "Ignored"))
    os.symlink(tmp_path / "a" / "one.md", tmp_path / "link.md")

    result = await load_prompt_templates(env, ["a", "link.md"])
    assert result["diagnostics"] == []
    assert [template["name"] for template in result["promptTemplates"]] == ["one", "link"]
    assert format_prompt_template_invocation(result["promptTemplates"][0], ["world"]) == "Hello world"
    assert parse_command_args("one 'two words' \"three words\"") == ["one", "two words", "three words"]

    get_or_throw(await env.write_file("emoji.md", "🙂" * 31))
    emoji = await load_prompt_templates(env, "emoji.md")
    assert emoji["promptTemplates"][0]["description"] == "🙂" * 30 + "..."


async def test_skills_recursive_root_files_ignore_and_sources(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    get_or_throw(await env.create_dir("skills/example"))
    get_or_throw(await env.create_dir("skills/ignored"))
    get_or_throw(
        await env.write_file(
            "skills/example/SKILL.md",
            "---\nname: example\ndescription: Example skill\ndisable-model-invocation: true\n---\nUse it.",
        )
    )
    get_or_throw(
        await env.write_file(
            "skills/ignored/SKILL.md", "---\nname: ignored\ndescription: Ignored skill\n---\nIgnore it."
        )
    )
    get_or_throw(await env.write_file("skills/.gitignore", "ignored/\n"))
    get_or_throw(await env.write_file("skills/root.md", "---\ndescription: Root skill\n---\nRoot body"))

    result = await load_skills(env, "skills")
    assert result["diagnostics"] == []
    assert [skill["name"] for skill in result["skills"]] == ["example", "skills"]
    sourced = await load_sourced_skills(env, [{"path": "skills", "source": {"type": "project"}}])
    assert sourced["skills"][0]["source"] == {"type": "project"}

    invocation = format_skill_invocation(result["skills"][0], "Do this now")
    assert "References are relative to" in invocation and invocation.endswith("Do this now")
    prompt = format_skills_for_system_prompt(result["skills"])
    assert "example" not in prompt
    assert "skills" in prompt


async def test_skill_metadata_uses_javascript_utf16_lengths_and_empty_name_fallback(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    get_or_throw(await env.create_dir("empty-name"))
    get_or_throw(
        await env.write_file(
            "empty-name/SKILL.md",
            f'---\nname: ""\ndescription: {"🙂" * 513}\n---\nBody',
        )
    )
    result = await load_skills(env, "empty-name")
    assert result["skills"][0]["name"] == "empty-name"
    assert result["diagnostics"][0]["code"] == "invalid_metadata"
    assert "1026" in result["diagnostics"][0]["message"]


async def test_frontmatter_uses_yaml_1_2_boolean_and_timestamp_scalars(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    get_or_throw(await env.create_dir("yes-skill"))
    get_or_throw(
        await env.write_file(
            "yes-skill/SKILL.md",
            "---\nname: yes-skill\ndescription: yes\ndisable-model-invocation: ON\n---\nBody",
        )
    )
    get_or_throw(await env.write_file("dated.md", "---\ndescription: 2026-01-01\n---\nBody"))

    skills = await load_skills(env, "yes-skill")
    templates = await load_prompt_templates(env, "dated.md")

    assert skills["skills"][0]["description"] == "yes"
    assert skills["skills"][0]["disableModelInvocation"] is False
    assert templates["promptTemplates"][0]["description"] == "2026-01-01"


async def test_sourced_prompt_diagnostic(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    get_or_throw(await env.write_file("broken.md", "---\ndescription: [unterminated\n---\nBody"))
    result = await load_sourced_prompt_templates(env, [{"path": "broken.md", "source": {"type": "user"}}])
    assert result["promptTemplates"] == []
    assert result["diagnostics"][0]["code"] == "parse_failed"
    assert result["diagnostics"][0]["source"] == {"type": "user"}
