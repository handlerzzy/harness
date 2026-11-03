from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal, TypeAlias, cast

import yaml

from .._utils import js_utf16_length, js_utf16_slice, js_utf16_sort_key
from .session.repo_utils import fs_call

PromptTemplateDiagnosticCode: TypeAlias = Literal["file_info_failed", "list_failed", "read_failed", "parse_failed"]
PromptTemplateDiagnostic: TypeAlias = dict[str, Any]


class _Yaml12SafeLoader(yaml.SafeLoader):
    """PyYAML loader aligned with the YAML 1.2 core scalars used by yaml@2."""


_Yaml12SafeLoader.yaml_implicit_resolvers = {
    first: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
cast(Any, _Yaml12SafeLoader).add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _diagnostic(code: str, message: str, path: str) -> dict[str, str]:
    return {"type": "warning", "code": code, "message": message, "path": path}


def _basename(path: str) -> str:
    normalized = path.rstrip("/")
    return normalized[normalized.rfind("/") + 1 :]


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---"):
        return {}, normalized
    end_index = normalized.find("\n---", 3)
    if end_index == -1:
        return {}, normalized
    parsed = yaml.load(normalized[4:end_index], Loader=_Yaml12SafeLoader) or {}
    return (parsed if isinstance(parsed, dict) else {}), normalized[end_index + 4 :].strip()


async def _resolve_kind(env: Any, info: dict[str, Any], diagnostics: list[dict[str, Any]]) -> str | None:
    kind = info["kind"]
    if isinstance(kind, str) and kind in {"file", "directory"}:
        return kind
    canonical = await fs_call(env, "canonical_path", "canonicalPath", info["path"])
    if not canonical["ok"]:
        if canonical["error"].code != "not_found":
            diagnostics.append(_diagnostic("file_info_failed", str(canonical["error"]), info["path"]))
        return None
    target = await fs_call(env, "file_info", "fileInfo", canonical["value"])
    if not target["ok"]:
        if target["error"].code != "not_found":
            diagnostics.append(_diagnostic("file_info_failed", str(target["error"]), info["path"]))
        return None
    target_kind = target["value"]["kind"]
    return str(target_kind) if target_kind in {"file", "directory"} else None


async def _load_template_from_file(env: Any, file_path: str) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    raw = await fs_call(env, "read_text_file", "readTextFile", file_path)
    if not raw["ok"]:
        diagnostics.append(_diagnostic("read_failed", str(raw["error"]), file_path))
        return {"promptTemplate": None, "diagnostics": diagnostics}
    try:
        frontmatter, body = _parse_frontmatter(raw["value"])
    except BaseException as error:
        diagnostics.append(_diagnostic("parse_failed", str(error), file_path))
        return {"promptTemplate": None, "diagnostics": diagnostics}
    first_line = next((line for line in body.split("\n") if line.strip()), None)
    description = frontmatter.get("description") if isinstance(frontmatter.get("description"), str) else ""
    if not description and first_line:
        description = js_utf16_slice(first_line, 0, 60) + ("..." if js_utf16_length(first_line) > 60 else "")
    return {
        "promptTemplate": {
            "name": re.sub(r"\.md$", "", _basename(file_path), flags=re.IGNORECASE),
            "description": description,
            "content": body,
        },
        "diagnostics": diagnostics,
    }


async def _load_templates_from_dir(env: Any, directory: str) -> dict[str, Any]:
    templates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    entries = await fs_call(env, "list_dir", "listDir", directory)
    if not entries["ok"]:
        diagnostics.append(_diagnostic("list_failed", str(entries["error"]), directory))
        return {"promptTemplates": templates, "diagnostics": diagnostics}
    for entry in sorted(entries["value"], key=lambda item: js_utf16_sort_key(item["name"])):
        kind = await _resolve_kind(env, entry, diagnostics)
        if kind != "file" or not entry["name"].endswith(".md"):
            continue
        result = await _load_template_from_file(env, entry["path"])
        if result["promptTemplate"]:
            templates.append(result["promptTemplate"])
        diagnostics.extend(result["diagnostics"])
    return {"promptTemplates": templates, "diagnostics": diagnostics}


async def load_prompt_templates(env: Any, paths: str | list[str]) -> dict[str, Any]:
    templates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for path in [paths] if isinstance(paths, str) else paths:
        info = await fs_call(env, "file_info", "fileInfo", path)
        if not info["ok"]:
            if info["error"].code != "not_found":
                diagnostics.append(_diagnostic("file_info_failed", str(info["error"]), path))
            continue
        kind = await _resolve_kind(env, info["value"], diagnostics)
        if kind == "directory":
            result = await _load_templates_from_dir(env, info["value"]["path"])
        elif kind == "file" and info["value"]["name"].endswith(".md"):
            loaded = await _load_template_from_file(env, info["value"]["path"])
            result = {
                "promptTemplates": [loaded["promptTemplate"]] if loaded["promptTemplate"] else [],
                "diagnostics": loaded["diagnostics"],
            }
        else:
            continue
        templates.extend(result["promptTemplates"])
        diagnostics.extend(result["diagnostics"])
    return {"promptTemplates": templates, "diagnostics": diagnostics}


async def load_sourced_prompt_templates(
    env: Any,
    inputs: list[dict[str, Any]],
    map_prompt_template: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    templates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for input_value in inputs:
        result = await load_prompt_templates(env, input_value["path"])
        source = input_value["source"]
        for template in result["promptTemplates"]:
            templates.append(
                {
                    "promptTemplate": map_prompt_template(template, source) if map_prompt_template else template,
                    "source": source,
                }
            )
        diagnostics.extend({**diagnostic, "source": source} for diagnostic in result["diagnostics"])
    return {"promptTemplates": templates, "diagnostics": diagnostics}


def parse_command_args(args_string: str) -> list[str]:
    arguments: list[str] = []
    current = ""
    quote: str | None = None
    for character in args_string:
        if quote:
            if character == quote:
                quote = None
            else:
                current += character
        elif character in {'"', "'"}:
            quote = character
        elif character in {" ", "\t"}:
            if current:
                arguments.append(current)
                current = ""
        else:
            current += character
    if current:
        arguments.append(current)
    return arguments


def substitute_args(content: str, args: list[str]) -> str:
    def positional_arg(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return args[index - 1] if 1 <= index <= len(args) else ""

    result = re.sub(r"\$(\d+)", positional_arg, content)

    def slice_args(match: re.Match[str]) -> str:
        start = max(0, int(match.group(1)) - 1)
        length = int(match.group(2)) if match.group(2) else None
        return " ".join(args[start : start + length] if length is not None else args[start:])

    result = re.sub(r"\$\{@:(\d+)(?::(\d+))?\}", slice_args, result)
    all_args = " ".join(args)
    return result.replace("$ARGUMENTS", all_args).replace("$@", all_args)


def format_prompt_template_invocation(template: Any, args: list[str] | None = None) -> str:
    content = template["content"] if isinstance(template, dict) else template.content
    return substitute_args(content, args or [])


loadPromptTemplates = load_prompt_templates
loadSourcedPromptTemplates = load_sourced_prompt_templates
parseCommandArgs = parse_command_args
substituteArgs = substitute_args
formatPromptTemplateInvocation = format_prompt_template_invocation

__all__ = [
    "PromptTemplateDiagnostic",
    "PromptTemplateDiagnosticCode",
    "formatPromptTemplateInvocation",
    "format_prompt_template_invocation",
    "loadPromptTemplates",
    "loadSourcedPromptTemplates",
    "load_prompt_templates",
    "load_sourced_prompt_templates",
    "parseCommandArgs",
    "parse_command_args",
    "substituteArgs",
    "substitute_args",
]
