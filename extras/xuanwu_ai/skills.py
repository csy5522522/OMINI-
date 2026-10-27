"""Agent Skills discovery and prompt loading for XuanwuAi."""
from __future__ import annotations

import re
from pathlib import Path


MAX_SKILL_BYTES = 128_000
MAX_ACTIVE_SKILL_CHARS = 80_000


def _roots(workspace: str, data_directory: Path):
    workspace_path = Path(workspace).expanduser()
    home = Path.home()
    candidates = [
        data_directory / "skills",
        workspace_path / ".agents" / "skills",
        workspace_path / ".claude" / "skills",
        workspace_path / ".minimax" / "skills",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".minimax" / "skills",
    ]
    found, seen = [], set()
    for root in candidates:
        try:
            resolved = root.resolve()
            if resolved not in seen and resolved.is_dir():
                seen.add(resolved)
                found.append(resolved)
        except OSError:
            continue
    return found


def _metadata(text: str, directory: Path):
    name = directory.name
    description = ""
    if text.startswith("---"):
        _, _, rest = text.partition("\n")
        header, separator, _ = rest.partition("\n---")
        if separator:
            for line in header.splitlines():
                match = re.match(r"^\s*(name|description)\s*:\s*(.*?)\s*$", line)
                if match:
                    value = match.group(2).strip().strip("\"'")
                    if match.group(1) == "name" and value:
                        name = value
                    elif match.group(1) == "description":
                        description = value
    if not description:
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            stripped = line.strip()
            if stripped:
                description = stripped[:240]
                break
    return name[:100], description[:500]


def discover_skills(workspace: str, data_directory: Path):
    skills, seen = [], set()
    for root in _roots(workspace, data_directory):
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        if (root / "SKILL.md").is_file():
            children = [root]
        for directory in children:
            if len(skills) >= 200:
                return skills
            if not directory.is_dir():
                continue
            document = directory / "SKILL.md"
            try:
                resolved = document.resolve(strict=True)
                if resolved in seen or not resolved.is_file() or resolved.stat().st_size > MAX_SKILL_BYTES:
                    continue
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            seen.add(resolved)
            name, description = _metadata(content, directory)
            skills.append({"id": str(resolved), "name": name, "description": description,
                           "path": str(resolved.parent), "source": str(root)})
    return skills


def load_enabled_skills(workspace: str, data_directory: Path, enabled):
    selected = set(enabled if isinstance(enabled, list) else [])
    if not selected:
        return ""
    documents = discover_skills(workspace, data_directory)
    blocks, remaining = [], MAX_ACTIVE_SKILL_CHARS
    for skill in documents:
        if skill["id"] not in selected or remaining <= 0:
            continue
        try:
            text = Path(skill["id"]).read_text(encoding="utf-8")[:remaining]
        except (OSError, UnicodeError):
            continue
        block = (f"\n\n<skill name={skill['name']!r} source={skill['path']!r}>\n"
                 f"{text}\n</skill>")
        blocks.append(block)
        remaining -= len(block)
    if not blocks:
        return ""
    return ("\n\n以下为用户在设置中启用的 Agent Skills。把它们作为本任务的操作说明；"
            "当 Skill 内容与当前用户请求冲突时，以用户请求为准。" + "".join(blocks))
