from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal, TypeAlias

import pathspec

from .._utils import js_utf16_length, js_utf16_sort_key
from .prompt_templates import _parse_frontmatter, _resolve_kind
from .session.repo_utils import fs_call

SkillDiagnosticCode: TypeAlias = Literal[
    "file_info_failed", "list_failed", "read_failed", "parse_failed", "invalid_metadata"
]
SkillDiagnostic: TypeAlias = dict[str, Any]

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
IGNORE_FILE_NAMES = [".gitignore", ".ignore", ".fdignore"]


def _field(value: Any, snake: str, camel: str) -> Any:
    if isinstance(value, dict):
        return value.get(snake, value.get(camel))
    return getattr(value, snake, getattr(value, camel, None))


def _dirname(path: str) -> str:
    normalized = path.rstrip("/")
    index = normalized.rfind("/")
    return "/" if index <= 0 else normalized[:index]


def _basename(path: str) -> str:
    normalized = path.rstrip("/")
    return normalized[normalized.rfind("/") + 1 :]


def _relative(root: str, path: str) -> str:
    root = root.rstrip("/")
    path = path.rstrip("/")
    if path == root:
        return ""
    return path[len(root) + 1 :] if path.startswith(root + "/") else path.lstrip("/")


def _join(base: str, child: str) -> str:
    return base.rstrip("/") + "/" + child.lstrip("/")


def _diagnostic(code: str, message: str, path: str) -> dict[str, str]:
    return {"type": "warning", "code": code, "message": message, "path": path}


class _IgnoreMatcher:
    def __init__(self) -> None:
        self.patterns: list[str] = []

    def add(self, patterns: list[str]) -> None:
        self.patterns.extend(patterns)

    def ignores(self, path: str) -> bool:
        return pathspec.GitIgnoreSpec.from_lines(self.patterns).match_file(path) if self.patterns else False


def format_skill_invocation(skill: Any, additional_instructions: str | None = None) -> str:
    name = _field(skill, "name", "name")
    file_path = _field(skill, "file_path", "filePath")
    content = _field(skill, "content", "content")
    block = (
        f'<skill name="{name}" location="{file_path}">\n'
        f"References are relative to {_dirname(file_path)}.\n\n{content}\n</skill>"
    )
    return f"{block}\n\n{additional_instructions}" if additional_instructions else block


def _prefix_ignore_pattern(line: str, prefix: str) -> str | None:
    trimmed = line.strip()
    if not trimmed or (trimmed.startswith("#") and not trimmed.startswith("\\#")):
        return None
    pattern = line
    negated = False
    if pattern.startswith("!"):
        negated = True
        pattern = pattern[1:]
    elif pattern.startswith("\\!"):
        pattern = pattern[1:]
    if pattern.startswith("/"):
        pattern = pattern[1:]
    prefixed = prefix + pattern if prefix else pattern
    return "!" + prefixed if negated else prefixed


async def _add_ignore_rules(
    env: Any, matcher: _IgnoreMatcher, directory: str, root: str, diagnostics: list[dict[str, Any]]
) -> None:
    relative_dir = _relative(root, directory)
    prefix = relative_dir + "/" if relative_dir else ""
    for filename in IGNORE_FILE_NAMES:
        path = _join(directory, filename)
        info = await fs_call(env, "file_info", "fileInfo", path)
        if not info["ok"]:
            if info["error"].code != "not_found":
                diagnostics.append(_diagnostic("file_info_failed", str(info["error"]), path))
            continue
        if info["value"]["kind"] != "file":
            continue
        content = await fs_call(env, "read_text_file", "readTextFile", path)
        if not content["ok"]:
            diagnostics.append(_diagnostic("read_failed", str(content["error"]), path))
            continue
        patterns = [
            pattern
            for line in re.split(r"\r?\n", content["value"])
            if (pattern := _prefix_ignore_pattern(line, prefix)) is not None
        ]
        matcher.add(patterns)


def _validate_name(name: str, parent: str) -> list[str]:
    errors: list[str] = []
    if name != parent:
        errors.append(f'name "{name}" does not match parent directory "{parent}"')
    name_length = js_utf16_length(name)
    if name_length > MAX_NAME_LENGTH:
        errors.append(f"name exceeds {MAX_NAME_LENGTH} characters ({name_length})")
    if not re.fullmatch(r"[a-z0-9-]+", name):
        errors.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name must not start or end with a hyphen")
    if "--" in name:
        errors.append("name must not contain consecutive hyphens")
    return errors


async def _load_skill_from_file(env: Any, file_path: str) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    raw = await fs_call(env, "read_text_file", "readTextFile", file_path)
    if not raw["ok"]:
        diagnostics.append(_diagnostic("read_failed", str(raw["error"]), file_path))
        return {"skill": None, "diagnostics": diagnostics}
    try:
        frontmatter, body = _parse_frontmatter(raw["value"])
    except BaseException as error:
        diagnostics.append(_diagnostic("parse_failed", str(error), file_path))
        return {"skill": None, "diagnostics": diagnostics}
    parent = _basename(_dirname(file_path))
    description = frontmatter.get("description") if isinstance(frontmatter.get("description"), str) else None
    if not description or not description.strip():
        diagnostics.append(_diagnostic("invalid_metadata", "description is required", file_path))
    elif js_utf16_length(description) > MAX_DESCRIPTION_LENGTH:
        diagnostics.append(
            _diagnostic(
                "invalid_metadata",
                f"description exceeds {MAX_DESCRIPTION_LENGTH} characters ({js_utf16_length(description)})",
                file_path,
            )
        )
    raw_name = frontmatter.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else parent
    diagnostics.extend(_diagnostic("invalid_metadata", message, file_path) for message in _validate_name(name, parent))
    if not description or not description.strip():
        return {"skill": None, "diagnostics": diagnostics}
    return {
        "skill": {
            "name": name,
            "description": description,
            "content": body,
            "filePath": file_path,
            "disableModelInvocation": frontmatter.get("disable-model-invocation") is True,
        },
        "diagnostics": diagnostics,
    }


async def _load_skills_from_dir(
    env: Any,
    directory: str,
    include_root_files: bool,
    matcher: _IgnoreMatcher,
    root: str,
) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    info = await fs_call(env, "file_info", "fileInfo", directory)
    if not info["ok"]:
        if info["error"].code != "not_found":
            diagnostics.append(_diagnostic("file_info_failed", str(info["error"]), directory))
        return {"skills": skills, "diagnostics": diagnostics}
    if await _resolve_kind(env, info["value"], diagnostics) != "directory":
        return {"skills": skills, "diagnostics": diagnostics}
    await _add_ignore_rules(env, matcher, directory, root, diagnostics)
    entries_result = await fs_call(env, "list_dir", "listDir", directory)
    if not entries_result["ok"]:
        diagnostics.append(_diagnostic("list_failed", str(entries_result["error"]), directory))
        return {"skills": skills, "diagnostics": diagnostics}
    entries = entries_result["value"]
    for entry in entries:
        if entry["name"] != "SKILL.md":
            continue
        if await _resolve_kind(env, entry, diagnostics) != "file" or matcher.ignores(_relative(root, entry["path"])):
            continue
        result = await _load_skill_from_file(env, entry["path"])
        if result["skill"]:
            skills.append(result["skill"])
        diagnostics.extend(result["diagnostics"])
        return {"skills": skills, "diagnostics": diagnostics}

    for entry in sorted(entries, key=lambda item: js_utf16_sort_key(item["name"])):
        if entry["name"].startswith(".") or entry["name"] == "node_modules":
            continue
        kind = await _resolve_kind(env, entry, diagnostics)
        if not kind:
            continue
        relative = _relative(root, entry["path"])
        if matcher.ignores(relative + "/" if kind == "directory" else relative):
            continue
        if kind == "directory":
            result = await _load_skills_from_dir(env, entry["path"], False, matcher, root)
        elif kind == "file" and include_root_files and entry["name"].endswith(".md"):
            loaded = await _load_skill_from_file(env, entry["path"])
            result = {"skills": [loaded["skill"]] if loaded["skill"] else [], "diagnostics": loaded["diagnostics"]}
        else:
            continue
        skills.extend(result["skills"])
        diagnostics.extend(result["diagnostics"])
    return {"skills": skills, "diagnostics": diagnostics}


async def load_skills(env: Any, directories: str | list[str]) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for directory in [directories] if isinstance(directories, str) else directories:
        info = await fs_call(env, "file_info", "fileInfo", directory)
        if not info["ok"]:
            if info["error"].code != "not_found":
                diagnostics.append(_diagnostic("file_info_failed", str(info["error"]), directory))
            continue
        if await _resolve_kind(env, info["value"], diagnostics) != "directory":
            continue
        result = await _load_skills_from_dir(env, info["value"]["path"], True, _IgnoreMatcher(), info["value"]["path"])
        skills.extend(result["skills"])
        diagnostics.extend(result["diagnostics"])
    return {"skills": skills, "diagnostics": diagnostics}


async def load_sourced_skills(
    env: Any,
    inputs: list[dict[str, Any]],
    map_skill: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for input_value in inputs:
        result = await load_skills(env, input_value["path"])
        source = input_value["source"]
        skills.extend(
            {"skill": map_skill(skill, source) if map_skill else skill, "source": source} for skill in result["skills"]
        )
        diagnostics.extend({**diagnostic, "source": source} for diagnostic in result["diagnostics"])
    return {"skills": skills, "diagnostics": diagnostics}


formatSkillInvocation = format_skill_invocation
loadSkills = load_skills
loadSourcedSkills = load_sourced_skills

__all__ = [
    "SkillDiagnostic",
    "SkillDiagnosticCode",
    "formatSkillInvocation",
    "format_skill_invocation",
    "loadSkills",
    "loadSourcedSkills",
    "load_skills",
    "load_sourced_skills",
]
