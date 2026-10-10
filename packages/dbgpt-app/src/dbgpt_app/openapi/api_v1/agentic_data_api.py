import asyncio
import io
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import anyio
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from dbgpt._private.config import Config
from dbgpt._private.pydantic import BaseModel as _BaseModel
from dbgpt.agent.core.context import ContextBudgetConfig
from dbgpt.agent.resource.tool.base import tool
from dbgpt.agent.skill.manage import get_skill_manager
from dbgpt.component import ComponentType
from dbgpt.configs.model_config import SKILLS_DIR, resolve_root_path
from dbgpt.core import PromptTemplate
from dbgpt.model.cluster import WorkerManagerFactory
from dbgpt.util.json_utils import parse_or_raise_error
from dbgpt_app.openapi.api_view_model import (
    ConversationVo,
    Result,
)
from dbgpt_serve.datasource.manages import ConnectorManager
from dbgpt_serve.utils.auth import UserRequest, get_user_from_headers

from .attachment_react_adapter import (
    AttachmentInputError,
    SessionAttachmentContext,
    build_file_context,
    build_input_files_v2,
    prepare_react_attachments,
    react_state_patch,
    resolve_legacy_chat_file_path,
    scrub_react_history_for_share,
)
from .react_final import AgentFinalAnswer, FinalAnswerAssembler, TaskResult
from .react_task import (
    TaskReActAgent,
    incomplete_summary,
    report_requirements,
    task_result_for_reply,
)
from .subagent.dispatcher import DISPATCH_PROMPT_SECTION, make_dispatch_tool
from .subagent.history import (
    build_subagent_history_snapshot,
    fail_running_subagent_history,
    update_subagent_history,
)
from .terms.api import router as terms_router
from .terms.service import (
    TermClarifier,
    contains_term,
    explanation_only_request,
    install_execution_guards,
    legacy_ratio_names,
    make_clarify_term,
    make_json_llm,
    record_term_check_failure,
    requirements_from_input,
    term_scope_description,
)

router = APIRouter()
router.include_router(terms_router)
CFG = Config()
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from dbgpt.agent.core.memory.gpts import GptsMemory
    from dbgpt.agent.resource.connector.manager import ConnectorManager
    from dbgpt.agent.resource.tool.base import BaseTool

REACT_AGENT_MEMORY_CACHE: Dict[str, "GptsMemory"] = {}

DEFAULT_SKILLS_DIR = SKILLS_DIR
AUTO_DATA_MARKER_PATTERN = re.compile(
    r"###([A-Z0-9_]+)_START###\s*(.*?)\s*###\1_END###", re.DOTALL
)


def _validate_upload_filename(filename: str) -> str:
    if "\x00" in filename:
        raise ValueError("filename must not contain null bytes")

    posix_path = PurePosixPath(filename)
    windows_path = PureWindowsPath(filename)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or len(posix_path.parts) != 1
        or len(windows_path.parts) != 1
        or filename in {"", ".", ".."}
    ):
        raise ValueError("filename must be a plain file name")
    return filename


async def _resolve_model_context_tokens(
    llm_client: Any, model_name: Optional[str]
) -> Optional[int]:
    """Resolve model context window from runtime model metadata."""
    if not llm_client or not model_name:
        return None

    try:
        metadata = await llm_client.get_model_metadata(model_name)
        context_length = getattr(metadata, "context_length", None)
        if isinstance(context_length, int) and context_length > 0:
            return context_length
    except Exception:
        logger.debug(
            "Failed to resolve context window for model %s", model_name, exc_info=True
        )
    return None


async def _load_context_budget_config(
    llm_client: Any = None,
    model_name: Optional[str] = None,
) -> ContextBudgetConfig:
    """Build context budget config from app TOML and model metadata."""
    defaults = ContextBudgetConfig()

    def _value(agent_context: Any, field_name: str, default: Any) -> Any:
        if agent_context is None:
            return default
        value = getattr(agent_context, field_name, default)
        return default if value is None else value

    try:
        app_config = CFG.SYSTEM_APP.config.configs.get("app_config")
        web_config = getattr(getattr(app_config, "service", None), "web", None)
        agent_context = getattr(web_config, "agent_context", None)
        max_context_tokens = _value(
            agent_context, "max_context_tokens", defaults.max_context_tokens
        )
        return ContextBudgetConfig(
            max_context_tokens=max_context_tokens,
            warning_threshold=_value(
                agent_context, "warning_threshold", defaults.warning_threshold
            ),
            error_threshold=_value(
                agent_context, "error_threshold", defaults.error_threshold
            ),
            critical_threshold=_value(
                agent_context, "critical_threshold", defaults.critical_threshold
            ),
            reserved_tokens=_value(
                agent_context, "reserved_tokens", defaults.reserved_tokens
            ),
            min_keep_recent_rounds=_value(
                agent_context,
                "min_keep_recent_rounds",
                defaults.min_keep_recent_rounds,
            ),
            max_compact_failures=_value(
                agent_context,
                "max_compact_failures",
                defaults.max_compact_failures,
            ),
            max_observation_age_rounds=_value(
                agent_context,
                "max_observation_age_rounds",
                defaults.max_observation_age_rounds,
            ),
            truncated_observation_max_chars=(
                _value(
                    agent_context,
                    "truncated_observation_max_chars",
                    defaults.truncated_observation_max_chars,
                )
            ),
            min_keep_tokens=_value(
                agent_context,
                "min_keep_tokens",
                defaults.min_keep_tokens,
            ),
        )
    except Exception:
        logger.debug(
            "Failed to load agent context config; using defaults", exc_info=True
        )
        return defaults


def _extract_auto_data_markers(text: str) -> tuple[str, Dict[str, str]]:
    """Extract generic marker blocks from script output text.

    Marker format:
        ###KEY_START###...###KEY_END###
    """

    if not text or "###" not in text:
        return text, {}

    extracted: Dict[str, str] = {}

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        value = match.group(2).strip()
        if value:
            extracted[key] = value
        return ""

    cleaned = AUTO_DATA_MARKER_PATTERN.sub(_replace, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, extracted


def _parse_connector_ids(ext_info: Optional[Dict[str, Any]]) -> List[str]:
    """Extract connector IDs from ``ext_info``.

    Supports two shapes:
    - ``ext_info.connector_ids`` — a list of UUID strings (preferred).
    - ``ext_info.connector_id``  — a single UUID string (legacy back-compat).

    Returns an empty list when no valid IDs are found.
    """
    if not ext_info or not isinstance(ext_info, dict):
        return []
    raw_ids = ext_info.get("connector_ids")
    if isinstance(raw_ids, list):
        return [cid for cid in raw_ids if isinstance(cid, str) and cid]
    legacy = ext_info.get("connector_id")
    if isinstance(legacy, str) and legacy:
        return [legacy]
    return []


def _select_connector_tools(
    connector_ids: List[str],
    connector_manager: Optional["ConnectorManager"],
) -> Tuple[List["BaseTool"], List[str]]:
    """Resolve connector_ids to flat list of BaseTool ready for ToolPack injection.

    Internally each connector is an MCPToolPack containing multiple BaseTool
    (one per MCP server tool). We flatten here so callers can directly compose
    them into a parent ToolPack without nesting issues -- nested ResourcePack
    would otherwise be inserted as a single dict entry under pack.name,
    making prefixed tool names un-lookup-able from the outer ToolPack.

    Args:
        connector_ids: IDs the user selected in the frontend.
        connector_manager: A :class:`ConnectorManager` instance (or ``None``).

    Returns:
        A tuple of ``(tools, missing_ids)`` where *tools* is the flat list
        of :class:`BaseTool` objects (each one a single MCP tool with its
        server URL captured in the call closure) and *missing_ids* lists
        IDs that could not be resolved (e.g. deleted mid-conversation).
    """
    from dbgpt.agent.resource.tool.base import BaseTool

    tools: List["BaseTool"] = []
    missing_ids: List[str] = []
    if connector_manager is None or not connector_ids:
        return tools, missing_ids
    for cid in connector_ids:
        pack = connector_manager.get_connector_tools(cid)
        if pack is None:
            missing_ids.append(cid)
            continue
        # Flatten: extract BaseTool instances from the pack
        for sub_tool in pack.sub_resources:
            if isinstance(sub_tool, BaseTool):
                tools.append(sub_tool)
    return tools, missing_ids


async def _execute_skill_script_impl(
    skill_name: str, script_name: str, args: dict
) -> str:
    """Execute a script from a skill (implementation)."""
    skill_manager = get_skill_manager(CFG.SYSTEM_APP)
    result = await skill_manager.execute_script(skill_name, script_name, args)
    return result


@tool(
    description='执行技能中的脚本。参数: {"skill_name": "技能名称", '
    '"script_name": "脚本名称", "args": {参数}}'
)
async def execute_skill_script(skill_name: str, script_name: str, args: dict) -> str:
    """Execute a script from a skill."""
    return await _execute_skill_script_impl(skill_name, script_name, args)


@tool(
    description="获取技能资源文件内容。"
    "根据路径读取技能中的参考文档、配置文件等非脚本资源。"
    '参数: {"skill_name": "技能名称", "resource_path": "资源路径"}'
    "\\n示例:"
    '\\n- 读取参考文档: {"skill_name": "my-skill", '
    '"resource_path": "references/analysis_framework.md"}'
    "\n注意: 执行脚本请使用 shell_interpreter 工具"
)
async def get_skill_resource(
    skill_name: str, resource_path: str, args: Optional[dict] = None
) -> str:
    from dbgpt.agent.skill.manage import get_skill_manager

    try:
        sm = get_skill_manager(CFG.SYSTEM_APP)
        result = await sm.get_skill_resource(skill_name, resource_path, args or {})
        return result
    except Exception as e:
        import json

        return json.dumps(
            {"error": True, "message": f"Error: {str(e)}"},
            ensure_ascii=False,
        )


@tool(
    description="执行技能scripts目录下的脚本文件。参数: "
    '{"skill_name": "技能名称", "script_file_name": "脚本文件名", "args": {参数}}'
)
async def execute_skill_script_file(
    skill_name: str, script_file_name: str, args: Optional[dict] = None
) -> str:
    """Execute a script file from a skill's scripts directory."""
    from dbgpt.agent.skill.manage import get_skill_manager

    try:
        sm = get_skill_manager(CFG.SYSTEM_APP)
        result = await sm.execute_skill_script_file(
            skill_name, script_file_name, args or {}
        )
        return result
    except Exception as e:
        import json

        return json.dumps(
            {"chunks": [{"output_type": "text", "content": f"Error: {str(e)}"}]},
            ensure_ascii=False,
        )


@router.get("/v1/skills/list", response_model=Result)
async def list_skills(
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """List all available skills from the skills directory.

    Returns a list of skills with their metadata, including:
    - id: Unique identifier for the skill
    - name: Display name of the skill
    - description: Brief description of what the skill does
    - version: Skill version
    - author: Skill author
    - skill_type: Type of skill (e.g., data_analysis, chat, coding)
    - tags: List of tags for categorization
    - type: 'official' for claude/ directory, 'personal' for user/ directory
    - file_path: Relative path to the skill file
    """
    from dbgpt.agent.skill.loader import SkillLoader

    skills_data = []
    skills_dir = DEFAULT_SKILLS_DIR
    skills_dir_resolved = Path(skills_dir).expanduser().resolve()

    try:
        loader = SkillLoader()
        skills = loader.load_skills_from_directory(skills_dir, recursive=True)

        for skill in skills:
            if not skill or not skill.metadata:
                continue

            metadata = skill.metadata
            # Determine if the skill is official or personal based on file path
            file_path = getattr(metadata, "file_path", None) or ""
            if not file_path and hasattr(skill, "_config"):
                file_path = skill._config.get("file_path", "")

            # Convert absolute file_path to relative (relative to skills_dir)
            if file_path:
                try:
                    file_path = str(
                        Path(file_path)
                        .expanduser()
                        .resolve()
                        .relative_to(skills_dir_resolved)
                    )
                except Exception:
                    pass

            # Determine type based on directory structure
            skill_type_category = "official"
            if "user/" in file_path or "/user/" in file_path:
                skill_type_category = "personal"
            elif "claude/" in file_path or "/claude/" in file_path:
                skill_type_category = "official"

            # Get skill_type value
            skill_type_val = metadata.skill_type
            if hasattr(skill_type_val, "value"):
                skill_type_val = skill_type_val.value

            skill_info = {
                "id": metadata.name,
                "name": metadata.name,
                "description": metadata.description or "",
                "version": getattr(metadata, "version", "1.0.0") or "1.0.0",
                "author": getattr(metadata, "author", None),
                "skill_type": skill_type_val,
                "tags": getattr(metadata, "tags", []) or [],
                "type": skill_type_category,
                "file_path": file_path,
            }
            skills_data.append(skill_info)

        # Sort skills: official first, then by name
        skills_data.sort(key=lambda x: (0 if x["type"] == "official" else 1, x["name"]))

        return Result.succ(skills_data)
    except Exception as e:
        logger.exception("Failed to load skills from directory")
        return Result.failed(code="E5001", msg=f"Failed to load skills: {str(e)}")


@router.get("/v1/skills/detail", response_model=Result)
async def skill_detail(
    skill_name: str = Query("", description="Skill name"),
    file_path: str = Query("", description="Skill file path"),
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """Load a skill detail, including file tree and SKILL.md content."""
    if not file_path:
        return Result.failed(code="E4001", msg="file_path is required")

    skills_dir = Path(DEFAULT_SKILLS_DIR).expanduser().resolve()

    # Always treat file_path as relative to skills_dir.
    # If an absolute path was provided (legacy), try to make it relative first.
    fp = Path(file_path).expanduser()
    if fp.is_absolute():
        try:
            fp = fp.resolve().relative_to(skills_dir)
        except Exception:
            return Result.failed(code="E4002", msg="Invalid skill file path")
    target = (skills_dir / fp).resolve()

    # Security: ensure target is under skills_dir
    try:
        target.relative_to(skills_dir)
    except Exception:
        return Result.failed(code="E4002", msg="Invalid skill file path")

    if not target.exists():
        return Result.failed(code="E4040", msg="Skill file not found")

    root_dir = target if target.is_dir() else target.parent

    def build_tree(path: Path, base: Path) -> Dict[str, Any]:
        rel = path.relative_to(base)
        node: Dict[str, Any] = {
            "title": path.name,
            "key": str(rel),
        }
        if path.is_dir():
            children = sorted(
                [p for p in path.iterdir() if not p.name.startswith(".")],
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
            node["children"] = [build_tree(child, base) for child in children]
        return node

    tree = build_tree(root_dir, root_dir)

    skill_md_path = root_dir / "SKILL.md"
    frontmatter = ""
    instructions = ""
    raw_content = ""
    content_type = ""

    if skill_md_path.exists():
        raw_content = skill_md_path.read_text(encoding="utf-8")
        content_type = "skill_md"
        content = raw_content.strip()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1].strip()
                instructions = parts[2].strip()
            else:
                instructions = content
        else:
            instructions = content
    elif target.is_file():
        raw_content = target.read_text(encoding="utf-8")
        suffix = target.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            content_type = "yaml"
            frontmatter = raw_content
        elif suffix == ".json":
            content_type = "json"
            frontmatter = raw_content
        else:
            content_type = "text"
            instructions = raw_content

    metadata: Dict[str, Any] = {}
    try:
        from dbgpt.agent.skill.loader import SkillLoader

        loader = SkillLoader()
        skill = loader.load_skill_from_file(str(target))
        if skill and getattr(skill, "metadata", None):
            try:
                metadata = skill.metadata.to_dict()  # type: ignore[attr-defined]
            except Exception:
                metadata = {
                    "name": getattr(skill.metadata, "name", ""),
                    "description": getattr(skill.metadata, "description", ""),
                    "version": getattr(skill.metadata, "version", ""),
                    "author": getattr(skill.metadata, "author", ""),
                    "skill_type": getattr(skill.metadata, "skill_type", ""),
                    "tags": getattr(skill.metadata, "tags", []) or [],
                }
    except Exception:
        metadata = {}

    if not frontmatter and metadata:
        frontmatter = "\n".join(
            [
                f"name: {metadata.get('name', '')}",
                f"description: {metadata.get('description', '')}",
                f"version: {metadata.get('version', '')}",
                f"author: {metadata.get('author', '')}",
                f"skill_type: {metadata.get('skill_type', '')}",
            ]
        ).strip()

    display_path = str(target)
    display_root = str(root_dir)
    try:
        display_path = str(target.relative_to(skills_dir))
        display_root = str(root_dir.relative_to(skills_dir))
    except Exception:
        pass

    return Result.succ(
        {
            "skill_name": skill_name or metadata.get("name", ""),
            "file_path": display_path,
            "root_dir": display_root,
            "tree": tree,
            "frontmatter": frontmatter,
            "instructions": instructions,
            "raw_content": raw_content,
            "content_type": content_type,
            "metadata": metadata,
        }
    )


def _install_skill_from_dir(src_dir: Path, skill_name: str, user_dir: Path) -> str:
    """Copy an extracted skill directory into the user skills directory.

    Args:
        src_dir (Path): Directory containing the skill's files (already extracted).
        skill_name (str): Name to use for the skill directory under ``user_dir``.
        user_dir (Path): The ``skills/user/`` directory.

    Returns:
        str: Path of the installed skill directory relative to the skills root
             (i.e. ``user/<skill_name>``).
    """
    dest = user_dir / skill_name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src_dir, dest)
    # Return path relative to skills_dir (parent of user_dir)
    return dest.relative_to(user_dir.parent).as_posix()


@router.post("/v1/skills/upload", response_model=Result)
async def skill_upload(
    file: UploadFile = File(...),
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """Upload a skill package (.zip, .skill) or a single file to pilot/tmp/."""
    if not file.filename:
        return Result.failed(code="E4001", msg="No file provided")

    upload_dir = Path(resolve_root_path("pilot/tmp") or "pilot/tmp").resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)

    skills_dir = Path(DEFAULT_SKILLS_DIR).expanduser().resolve()
    user_dir = skills_dir / "user"
    user_dir.mkdir(parents=True, exist_ok=True)

    try:
        filename = _validate_upload_filename(file.filename)
    except ValueError as exc:
        return Result.failed(code="E4002", msg=str(exc))

    suffix = Path(filename).suffix.lower()
    stem = Path(filename).stem

    try:
        content_bytes = await file.read()

        tmp_file = upload_dir / filename
        tmp_file.write_bytes(content_bytes)

        is_archive = False
        if suffix == ".zip":
            is_archive = True
        elif suffix == ".skill":
            buf = io.BytesIO(content_bytes)
            is_archive = zipfile.is_zipfile(buf)

        if is_archive:
            # Reuse the robust _extract_skill_from_zip helper (same one used
            # by the GitHub import endpoint) to avoid the nested-directory bug
            # that the old inline extractall logic suffered from.
            #
            # strict=False: uploaded packages may not contain a SKILL.md yet.
            tmp_zip = upload_dir / f"{uuid.uuid4().hex}.zip"
            tmp_zip.write_bytes(content_bytes)
            try:
                with tempfile.TemporaryDirectory(dir=upload_dir) as tmp_extract:
                    dest_in_tmp = Path(tmp_extract) / "skill"
                    try:
                        dest_name = _extract_skill_from_zip(
                            tmp_zip, subpath=None, dest_dir=dest_in_tmp, strict=False
                        )
                    except ValueError as exc:
                        return Result.failed(code="E4002", msg=str(exc))

                    rel_path = _install_skill_from_dir(dest_in_tmp, dest_name, user_dir)
            finally:
                tmp_zip.unlink(missing_ok=True)

        else:
            dest = user_dir / stem
            dest.mkdir(parents=True, exist_ok=True)

            if suffix in (".md", ".skill"):
                target_name = "SKILL.md"
            else:
                target_name = filename
            target_file = dest / target_name

            target_file.write_bytes(content_bytes)

            rel_path = dest.relative_to(skills_dir).as_posix()

        return Result.succ(
            {
                "file_path": rel_path,
                "tmp_path": str(tmp_file),
                "message": f"Skill uploaded successfully: {rel_path}",
            }
        )
    except Exception as e:
        logger.exception("Failed to upload skill")
        return Result.failed(code="E5002", msg=f"Upload failed: {str(e)}")


def _parse_github_url(
    github_url: str,
) -> "tuple[str, str, str, Optional[str]]":
    """Parse a GitHub or skills.sh URL into (owner, repo, branch, subdir).

    Supported formats:
      - https://github.com/owner/repo
      - https://github.com/owner/repo/tree/<branch>[/optional/sub/dir]
      - https://github.com/owner/repo/blob/<branch>/path/to/FILE.md
      - https://skills.sh/owner/repo
      - https://skills.sh/owner/repo[/skill-name]

    Returns:
        tuple[str, str, str, Optional[str]]
          (owner, repo, branch, subdir) — branch is always a str (defaults to "main")

    Raises:
        ValueError: if the URL is not a recognisable GitHub/skills.sh repo URL.
    """
    parsed = urlparse(github_url)
    is_skills_sh = parsed.netloc in ("skills.sh", "www.skills.sh")
    is_github = parsed.netloc in ("github.com", "www.github.com")

    if not is_github and not is_skills_sh:
        raise ValueError(f"Not a GitHub URL: {github_url!r}")

    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Cannot extract owner/repo from URL: {github_url!r}")

    owner, repo = parts[0], parts[1]
    # Strip '.git' suffix if present
    if repo.endswith(".git"):
        repo = repo[:-4]

    branch: str = "main"
    subdir: Optional[str] = None

    if is_skills_sh:
        # skills.sh: /owner/repo[/skill-name[/more]]
        # Everything after owner/repo is treated as subpath
        if len(parts) >= 3:
            subdir = "/".join(parts[2:])
    else:
        # GitHub
        if len(parts) >= 4 and parts[2] == "tree":
            # /owner/repo/tree/<branch>[/path/to/subdir]
            branch = parts[3]
            if len(parts) >= 5:
                subdir = "/".join(parts[4:])
        elif len(parts) >= 4 and parts[2] == "blob":
            # /owner/repo/blob/<branch>/path/to/FILE.md — strip filename
            branch = parts[3]
            if len(parts) >= 6:
                # Keep everything except the last component (the filename)
                subdir = "/".join(parts[4:-1])
            # If exactly 5 parts: blob/<branch>/filename — no subdir

    return owner, repo, branch, subdir


def _construct_download_url(owner: str, repo: str, branch: str) -> str:
    """Return the GitHub archive ZIP download URL for the given branch.

    Args:
        owner (str): Repository owner/organisation.
        repo (str): Repository name.
        branch (str): Branch name.

    Returns:
        str: URL pointing to the ZIP archive for the branch.
    """
    return f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"


def _is_macos_junk(name: str) -> bool:
    """Return True if the archive entry is a macOS metadata artifact."""
    parts = name.split("/")
    return any(p == "__MACOSX" or p.startswith("._") for p in parts)


def _extract_skill_from_zip(
    zip_path: "Path",
    subpath: "Optional[str]",
    dest_dir: "Path",
    strict: bool = True,
) -> str:
    """Extract a skill from a ZIP archive into ``dest_dir``.

    The ZIP is expected to have a single top-level directory (e.g.
    ``repo-main/``).  That top-level directory is stripped when extracting so
    that the files inside it land directly in ``dest_dir``.

    When ``subpath`` is given, only the files under
    ``{top_dir}/{subpath}/`` are extracted (again, stripped to ``dest_dir``).

    macOS metadata entries (``__MACOSX/`` directories and ``._*`` files) are
    automatically filtered out before any directory-structure analysis so they
    do not cause spurious nested directories.

    Args:
        zip_path (Path): Path to the ZIP file on disk.
        subpath (Optional[str]): Relative sub-directory inside the archive
            (after stripping the top-level dir) that contains the skill.
            Pass ``None`` to use the root of the archive.
        dest_dir (Path): Directory into which the skill files are extracted.
            It is created if it does not exist; if it already exists its
            contents are removed before extraction.
        strict (bool): When ``True`` (default), raise ``ValueError`` if no
            ``SKILL.md`` is found in the archive.  When ``False``, skip the
            ``SKILL.md`` validation — useful for uploading generic skill
            packages that may not yet contain a ``SKILL.md``.

    Returns:
        str: The skill name derived from ``subpath`` (last component) or from
        the top-level archive directory name.

    Raises:
        ValueError: If the archive contains path-traversal or absolute
            entries.
        ValueError: If no ``SKILL.md`` is found after extraction (only when
            ``strict=True``).
        ValueError: If the archive root contains multiple sub-directories with
            ``SKILL.md`` files and no ``subpath`` was specified (the error
            message lists the available sub-directory names).
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        all_names = zf.namelist()

        # Security: reject any path-traversal or absolute entries
        for name in all_names:
            normalized = os.path.normpath(name)
            if (
                normalized.startswith("..")
                or ".." in normalized.split(os.sep)
                # Absolute entry names ("/tmp/x", "C:\x"): "dest_dir / rel"
                # discards the base entirely for absolute paths (pathlib
                # semantics), giving an arbitrary file write.
                or PurePosixPath(name).is_absolute()
                or PureWindowsPath(name).is_absolute()
                # "top//tmp/x" collapses under normpath, but the raw member is
                # sliced on skill_prefix below, leaving a leading "/" in rel.
                or "//" in name
                # Windows separators must not leak into host path joins.
                or "\\" in name
            ):
                raise ValueError(f"Unsafe path in archive: {name!r}")

        # Filter out macOS metadata artifacts before analysing structure
        valid_names = [n for n in all_names if not _is_macos_junk(n)]

        # Detect the single top-level directory (GitHub archives always have one)
        top_dirs = {n.split("/")[0] for n in valid_names if "/" in n}
        archive_root: Optional[str] = top_dirs.pop() if len(top_dirs) == 1 else None

        # Build the prefix inside the archive that maps to dest_dir
        if subpath:
            skill_prefix = (
                f"{archive_root}/{subpath}/" if archive_root else f"{subpath}/"
            )
            skill_name = subpath.split("/")[-1]
        else:
            skill_prefix = f"{archive_root}/" if archive_root else ""
            skill_name = archive_root or dest_dir.name

        # Check whether SKILL.md exists under the chosen prefix
        skill_md_entry = next(
            (n for n in valid_names if n == skill_prefix + "SKILL.md"),
            None,
        )

        if skill_md_entry is None and not subpath:
            # No SKILL.md at root — scan one level of subdirectories
            subdirs_with_skill = []
            for name in valid_names:
                if not name.startswith(skill_prefix):
                    continue
                rel = name[len(skill_prefix) :]
                parts = rel.split("/")
                if len(parts) == 2 and parts[1] == "SKILL.md":
                    subdirs_with_skill.append(parts[0])

            if len(subdirs_with_skill) > 1:
                raise ValueError(
                    "Multiple skills found. Specify a subpath. "
                    "Available: " + ", ".join(sorted(subdirs_with_skill))
                )

            # If exactly one sub-directory has SKILL.md, use it automatically
            if len(subdirs_with_skill) == 1:
                only_subdir = subdirs_with_skill[0]
                skill_prefix = f"{skill_prefix}{only_subdir}/"
                skill_name = only_subdir
                skill_md_entry = skill_prefix + "SKILL.md"

        if strict and skill_md_entry is None:
            raise ValueError(
                "No SKILL.md found in the archive"
                + (f" under '{subpath}'" if subpath else "")
                + ". Make sure the skill directory contains a SKILL.md file."
            )

        # Prepare dest_dir: remove existing content then (re-)create
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Extract valid members individually (no extractall) for security
        for member in valid_names:
            if not member.startswith(skill_prefix) or member == skill_prefix:
                continue
            rel = member[len(skill_prefix) :]
            if not rel:
                continue
            target = dest_dir / rel
            try:
                # Containment guard: even if a member name slipped past the
                # scan above, an entry whose join escapes dest_dir must be
                # rejected before anything is written.
                target.relative_to(dest_dir)
            except ValueError:
                raise ValueError(f"Unsafe path in archive: {member!r}") from None
            if member.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))

    return skill_name


@router.post("/v1/skills/import_github", response_model=Result)
async def skill_import_from_github_v2(
    request: Request,
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """Import a skill from a GitHub or skills.sh URL.

    Accepts ``{ "url": "..." }`` from the frontend, downloads the repository
    ZIP, extracts the skill, installs it to ``skills/user/<name>/``, and
    returns a success response.

    This endpoint:

    - Accepts a raw JSON body ``{ "url": "..." }`` (no Pydantic model).
    - Supports branch fallback: tries ``main`` first, then ``master`` if 404.
    - Enforces a 50 MB download size limit.
    - Delegates extraction/installation to the modular helpers
      ``_extract_skill_from_zip`` and ``_install_skill_from_dir``.

    Error codes:
        - ``E4001``: Empty URL.
        - ``E4003``: Malformed or non-GitHub/skills.sh URL.
        - ``E4004``: ``SKILL.md`` not found in the downloaded content.
        - ``E4005``: Download failed or size limit exceeded.
        - ``E5002``: Unexpected server-side error.
    """
    import httpx

    # --- parse JSON body --------------------------------------------------------
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return Result.failed(code="E4001", msg="URL must not be empty")

    # --- parse URL --------------------------------------------------------------
    try:
        owner, repo, branch, subpath = _parse_github_url(url)
    except ValueError as exc:
        return Result.failed(code="E4003", msg=str(exc))

    # --- resolve dirs -----------------------------------------------------------
    skills_dir = Path(DEFAULT_SKILLS_DIR).expanduser().resolve()
    user_dir = skills_dir / "user"
    user_dir.mkdir(parents=True, exist_ok=True)

    upload_dir = Path(resolve_root_path("pilot/tmp") or "pilot/tmp").resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)

    # --- download with branch fallback (main → master) --------------------------
    zip_path: Optional[Path] = None
    tmp_dir_obj = None  # tempfile.TemporaryDirectory kept alive until finally

    try:
        zip_url = _construct_download_url(owner, repo, branch)

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(120.0),
        ) as client:
            response = await client.get(zip_url)

            # Branch fallback: if the resolved branch gives 404, try "master"
            if response.status_code == 404 and branch == "main":
                fallback_branch = "master"
                fallback_url = _construct_download_url(owner, repo, fallback_branch)
                response = await client.get(fallback_url)
                if response.status_code == 200:
                    branch = fallback_branch
                    zip_url = fallback_url

            if response.status_code != 200:
                return Result.failed(
                    code="E4005",
                    msg=(
                        f"Failed to download {zip_url!r}: HTTP {response.status_code}"
                    ),
                )

            content_bytes = response.content

        # --- size limit check ---------------------------------------------------
        if len(content_bytes) > 50 * 1024 * 1024:
            return Result.failed(
                code="E4005",
                msg=(
                    f"Download size {len(content_bytes) // (1024 * 1024)} MB "
                    "exceeds the 50 MB limit"
                ),
            )

        # --- save raw zip to tmp ------------------------------------------------
        zip_filename = f"{repo}-{branch}.zip"
        zip_path = upload_dir / zip_filename
        zip_path.write_bytes(content_bytes)

        # --- extract into a temp directory, then install ------------------------
        tmp_dir_obj = tempfile.TemporaryDirectory(dir=upload_dir)
        dest_dir_in_temp = Path(tmp_dir_obj.name) / "skill"
        dest_dir_in_temp.mkdir(parents=True, exist_ok=True)

        try:
            skill_name = _extract_skill_from_zip(zip_path, subpath, dest_dir_in_temp)
        except ValueError as exc:
            err_msg = str(exc)
            if "SKILL.md" in err_msg:
                return Result.failed(code="E4004", msg=err_msg)
            return Result.failed(code="E4003", msg=err_msg)

        rel_path = _install_skill_from_dir(dest_dir_in_temp, skill_name, user_dir)

        return Result.succ(
            {
                "file_path": rel_path,
                "message": f"Skill imported successfully from GitHub: {rel_path}",
            }
        )

    except httpx.RequestError as exc:
        logger.exception("Network error while downloading skill from GitHub")
        return Result.failed(
            code="E4005", msg=f"Network error downloading skill: {str(exc)}"
        )
    except Exception as exc:
        logger.exception("Failed to import skill from GitHub (v2)")
        return Result.failed(code="E5002", msg=f"Import failed: {str(exc)}")
    finally:
        # Clean up temp zip file
        if zip_path is not None:
            try:
                zip_path.unlink(missing_ok=True)
            except Exception:
                pass
        # Clean up temp extraction directory
        if tmp_dir_obj is not None:
            try:
                tmp_dir_obj.cleanup()
            except Exception:
                pass


def _sse_event(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_react_history_payload(
    *,
    final_content: str,
    steps: List[Dict[str, Any]],
    task_plan: List[Dict[str, Any]],
    generated_images: List[Any],
    sub_agents: Any,
    input_files: List[Dict[str, Any]],
    citations: Optional[List[Dict[str, Any]]] = None,
    task_result: Optional[TaskResult] = None,
    term_definitions: Optional[List[Dict[str, Any]]] = None,
    task_requirements: Optional[Dict[str, Any]] = None,
    resource_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Serialize the persisted react-agent history payload (version 2).

    The success and error paths share this builder so both persist the same
    shape. ``input_files`` is the current turn's public snapshot produced by
    :func:`build_input_files_v2` — safe metadata only, never server paths,
    storage URIs, owner ids, hashes or inspection bodies. ``citations`` comes
    from the final-answer assembler and remains display-safe metadata.
    """
    return json.dumps(
        {
            "version": 2,
            "protocol_version": 2,
            "type": "react-agent",
            "final_content": final_content,
            "citations": citations or [],
            "steps": steps,
            "task_plan": task_plan,
            "generated_images": generated_images,
            "sub_agents": sub_agents,
            "input_files": input_files,
            "term_definitions": term_definitions or [],
            **(
                {"task_requirements": task_requirements}
                if task_requirements is not None
                else {}
            ),
            **(
                {
                    "resource_context": {
                        "database": (
                            {
                                "name": resource_context["database"]["name"],
                                "type": resource_context["database"]["type"],
                            }
                            if resource_context.get("database")
                            else None
                        )
                    }
                }
                if resource_context is not None
                else {}
            ),
            **({"task_result": task_result.to_dict()} if task_result else {}),
        },
        ensure_ascii=False,
    )


def _sse_event_type(event: Any) -> Optional[str]:
    """Read an SSE event type without trusting arbitrary streamed text."""
    if not isinstance(event, str):
        return None
    first_line = event.splitlines()[0] if event else ""
    if not first_line.startswith("data:"):
        return None
    try:
        payload = json.loads(first_line.removeprefix("data:").strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    event_type = payload.get("type") if isinstance(payload, dict) else None
    return event_type if isinstance(event_type, str) else None


def _react_terminal_events(
    storage_conv: Any,
    history_payload: str,
    final_answer: AgentFinalAnswer,
) -> Tuple[str, str]:
    """Persist one ReAct round without risking its terminal SSE events."""
    try:
        storage_conv.add_view_message(history_payload)
        storage_conv.end_current_round()
        storage_conv.save_to_storage()
    except Exception:
        logger.exception("Failed to persist ReAct agent history")

    return (
        _sse_event(final_answer.to_sse_payload()),
        _sse_event({"type": "done"}),
    )


async def _cancel_and_await_agent_task(task: "asyncio.Task[Any]") -> None:
    """Cancel a running agent task and always consume its terminal result."""
    was_done = task.done()
    if not was_done:
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        if not was_done:
            logger.exception("ReAct agent task failed during stream cleanup")


async def _react_agent_stream(
    dialogue: ConversationVo,
    tool_mode: str = "full",
    attachment_ctx: Optional[SessionAttachmentContext] = None,
) -> AsyncGenerator[str, None]:
    """Stream the ReAct agent turn, owning the attachment lifecycle.

    The resolved session-file attachment context (when ``file_ids`` were
    supplied) stays open for the whole turn and is closed exactly once when
    the stream completes, fails, or is closed early by the client.

    Args:
        dialogue: Conversation parameters (user input, model, ext_info, etc.).
        tool_mode: "full" (default) — all tools; "knowledge" — kb tools only.
        attachment_ctx: Pre-resolved attachment context for this turn (or
            ``None`` for pure-text / legacy ``file_path`` requests).
    """
    inner_stream = _react_agent_stream_inner(dialogue, tool_mode, attachment_ctx)
    try:
        async for event in inner_stream:
            yield event
    finally:
        # Explicitly close nested generators; async-for alone does not own them.
        with anyio.CancelScope(shield=True):
            await inner_stream.aclose()
        if attachment_ctx is not None:
            try:
                attachment_ctx.close()
            except Exception:
                logger.warning(
                    "Failed to close session attachment context", exc_info=True
                )


def _legacy_upload_base_dir() -> str:
    """Return the base dir of the legacy ``python_uploads`` tree."""
    app = CFG.SYSTEM_APP
    work_dir = getattr(app, "work_dir", None) if app else None
    return work_dir or os.getcwd()


async def _open_turn_attachments(
    dialogue: ConversationVo, user_token: Optional[UserRequest]
) -> Optional[SessionAttachmentContext]:
    """Validate file input and resolve session attachments before streaming.

    Conflicting/malformed/too-many inputs raise 400; any unresolvable
    ``file_id`` raises one indistinguishable, non-enumerating 404. Errors
    always surface before the SSE stream (and the agent) is constructed.
    """
    owner_id = (user_token.user_id if user_token else None) or dialogue.user_name
    try:
        attachment_ctx = await prepare_react_attachments(dialogue, owner_id=owner_id)
        if attachment_ctx is None:
            # Legacy ``ext_info.file_path`` requests are confined to the
            # authenticated owner's ``python_uploads/<owner>`` root; invalid
            # input raises the same 400 and ownership failures the same
            # non-enumerating 404 as the file_ids flow.
            try:
                spec = dialogue.file_input_spec()
            except Exception:
                spec = None
            legacy_path = spec.file_path if spec is not None else None
            if legacy_path:
                resolved = await run_in_threadpool(
                    lambda: resolve_legacy_chat_file_path(
                        file_path=legacy_path,
                        owner_id=owner_id,
                        base_dir=_legacy_upload_base_dir(),
                    )
                )
                dialogue.ext_info = dict(dialogue.ext_info or {})
                dialogue.ext_info["file_path"] = resolved
        return attachment_ctx
    except AttachmentInputError as error:
        raise HTTPException(
            status_code=error.status_code, detail=error.message
        ) from error


def _close_turn_attachments_quietly(
    attachment_ctx: Optional[SessionAttachmentContext],
) -> None:
    """Close the turn attachment context, logging instead of raising."""
    if attachment_ctx is None:
        return
    try:
        attachment_ctx.close()
    except Exception:
        logger.warning("Failed to close session attachment context", exc_info=True)


async def _react_agent_stream_inner(
    dialogue: ConversationVo,
    tool_mode: str = "full",
    attachment_ctx: Optional[SessionAttachmentContext] = None,
) -> AsyncGenerator[str, None]:
    """Stream ReAct events while owning the lifetime of its background task."""
    agent_task_holder: List["asyncio.Task[Any]"] = []
    final_emitted = False
    done_emitted = False
    implementation = _react_agent_stream_impl(
        dialogue,
        tool_mode=tool_mode,
        attachment_ctx=attachment_ctx,
        agent_task_holder=agent_task_holder,
    )
    try:
        async for event in implementation:
            event_type = _sse_event_type(event)
            final_emitted = final_emitted or event_type == "final"
            done_emitted = done_emitted or event_type == "done"
            yield event
    except Exception:
        logger.exception("ReAct agent stream failed before normal completion")
        if not final_emitted and not done_emitted:
            yield _sse_event(
                AgentFinalAnswer(
                    content="执行发生错误，任务未完成，请重试。",
                    task_result=TaskResult("failed", "runtime_error"),
                ).to_sse_payload()
            )
        if not done_emitted:
            yield _sse_event({"type": "done"})
    finally:
        with anyio.CancelScope(shield=True):
            await implementation.aclose()
            if agent_task_holder:
                await _cancel_and_await_agent_task(agent_task_holder[0])


class _AgentStreamingResponse(StreamingResponse):
    """Streaming response that explicitly closes its owned body iterator."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if callable(close):
                try:
                    await close()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Failed to close ReAct agent stream iterator")


async def _react_agent_stream_impl(
    dialogue: ConversationVo,
    tool_mode: str = "full",
    attachment_ctx: Optional[SessionAttachmentContext] = None,
    agent_task_holder: Optional[List["asyncio.Task[Any]"]] = None,
) -> AsyncGenerator[str, None]:
    """Core ReAct agent streaming logic.

    Args:
        dialogue: Conversation parameters (user input, model, ext_info, etc.).
        tool_mode: "full" (default) — all tools (skills, shell, sql, html, code, kb...).
                   "knowledge" — only knowledge base tools + todowrite + terminate,
                   optimized for pure knowledge-chat scenarios.
        attachment_ctx: Pre-resolved session attachment context for this turn.
    """
    from dbgpt.agent import AgentContext, AgentMemory, AgentMessage
    from dbgpt.agent.claude_skill import get_registry, load_skills_from_dir
    from dbgpt.agent.core.memory.gpts import (
        DefaultGptsPlansMemory,
        GptsMemory,
    )
    from dbgpt.agent.expand.actions.react_action import Terminate
    from dbgpt.agent.resource import ToolPack
    from dbgpt.agent.resource.manage import get_resource_manager
    from dbgpt.agent.util.llm.llm import LLMConfig, LLMStrategyType
    from dbgpt.agent.util.react_parser import ReActOutputParser
    from dbgpt.core import StorageConversation
    from dbgpt.model.cluster.client import DefaultLLMClient
    from dbgpt_serve.agent.agents.db_gpts_memory import MetaDbGptsMessageMemory
    from dbgpt_serve.conversation.serve import Serve as ConversationServe

    step = 0
    user_input = dialogue.user_input
    if not isinstance(user_input, str):
        user_input = str(user_input or "")

    file_path = None
    knowledge_space = None
    skill_name = None
    database_name = None
    if dialogue.ext_info and isinstance(dialogue.ext_info, dict):
        file_path = dialogue.ext_info.get("file_path")
        skill_name = dialogue.ext_info.get("skill_name")
        # Support multiple field names for knowledge space
        knowledge_space = (
            dialogue.ext_info.get("knowledge_space")
            or dialogue.ext_info.get("knowledge_space_name")
            or dialogue.ext_info.get("knowledge_space_id")
        )
        database_name = dialogue.ext_info.get("database_name")

    # Connector selection (Task C): only inject user-selected connectors.
    connector_ids: List[str] = _parse_connector_ids(dialogue.ext_info)

    def _has_code_graph(knowledge_space_id: str) -> bool:
        """Check if the knowledge space has a built code graph index.

        Used to conditionally expose codegraph tools (kb_codegraph_*) to the
        agent. Returns True only when the graph meta record exists and has a
        non-zero vertex count.
        """
        if not knowledge_space_id:
            return False
        try:
            from dbgpt_serve.rag.models.code_graph_db import CodeGraphMetaDao
            from dbgpt_serve.rag.tools.kb_file_tools import _resolve_space_name

            space_name = _resolve_space_name(knowledge_space_id)
            meta = CodeGraphMetaDao().get_by_knowledge_id(space_name)
            return bool(meta and (meta.vertex_count or 0) > 0)
        except Exception as e:
            logger.warning(
                f"Failed to check code graph status for {knowledge_space_id}: {e}"
            )
            return False

    code_graph_available = (
        _has_code_graph(knowledge_space) if knowledge_space else False
    )

    def build_step(title: str, detail: str, phase: str = None):
        nonlocal step
        step += 1
        step_id = f"step-{step}"
        event_data = {
            "type": "step.start",
            "step": step,
            "id": step_id,
            "title": title,
            "detail": detail,
        }
        if phase:
            event_data["phase"] = phase
        return step_id, _sse_event(event_data)

    def step_output(detail: str):
        return _sse_event({"type": "step.output", "step": step, "detail": detail})

    def step_chunk(step_id: str, output_type: str, content: Any):
        return _sse_event(
            {
                "type": "step.chunk",
                "id": step_id,
                "output_type": output_type,
                "content": content,
            }
        )

    def step_done(step_id: str, status: str = "done"):
        return _sse_event({"type": "step.done", "id": step_id, "status": status})

    def step_meta(
        step_id: str,
        thought: Optional[str],
        action: Optional[str],
        action_input: Optional[str],
        title: Optional[str] = None,
        action_intention: Optional[str] = None,
        action_reason: Optional[str] = None,
        todo_meta: Optional[Dict[str, Any]] = None,
    ):
        payload = {
            "type": "step.meta",
            "id": step_id,
            "thought": thought,
            "action_intention": action_intention,
            "action_reason": action_reason,
            "action": action,
            "action_input": action_input,
            "title": title,
        }
        if todo_meta:
            payload["todo_meta"] = todo_meta
        return _sse_event(payload)

    def chunk_text(text: str, max_len: int = 800) -> List[str]:
        if not text:
            return []
        chunks: List[str] = []
        start = 0
        while start < len(text):
            chunks.append(text[start : start + max_len])
            start += max_len
        return chunks

    def emit_tool_chunks(step_id: str, content: Any) -> List[str]:
        raw_chunks: List[str] = []
        if content is None:
            return raw_chunks
        parsed = None
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except Exception:
                parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("chunks"), list):
            for item in parsed["chunks"]:
                if not isinstance(item, dict):
                    continue
                output_type = item.get("output_type") or "text"
                payload = item.get("content")
                if output_type in ["code", "markdown"] and isinstance(payload, str):
                    # Send code and markdown as a single chunk — never split it.
                    raw_chunks.append(step_chunk(step_id, output_type, payload))
                elif output_type in ["text"] and isinstance(payload, str):
                    for chunk in chunk_text(payload, max_len=800):
                        raw_chunks.append(step_chunk(step_id, output_type, chunk))
                else:
                    raw_chunks.append(step_chunk(step_id, output_type, payload))
            return raw_chunks
        if isinstance(content, str) and content:
            for chunk in chunk_text(content, max_len=800):
                raw_chunks.append(step_chunk(step_id, "text", chunk))
        return raw_chunks

    def normalize_display_text(value: Optional[str]) -> Optional[str]:
        """Normalize a model-provided display field."""
        if not value:
            return None

        text = re.sub(r"\s+", " ", value).strip()
        text = re.sub(
            r"^(phase|status|状态|action\s+intention|action\s+reason)\s*:\s*",
            "",
            text,
            flags=re.I,
        ).strip()
        text = text.strip(" .,:;，。；：")
        if not text:
            return None
        return text

    def summarize_thought(
        thought: Optional[str], action: Optional[str] = None
    ) -> Optional[str]:
        """Fallback compressor when the model does not provide a short status."""
        if not thought:
            return None

        text = re.sub(r"\s+", " ", thought).strip()
        text = re.sub(r"^(thought|phase)\s*:\s*", "", text, flags=re.I).strip()
        if not text:
            return None

        split_markers = [
            r"\baction\b\s*:",
            r"\bobservation\b\s*:",
            r"\bphase\b\s*:",
            r"\bnow i need to\b",
            r"\bnext,?\b",
            r"\bthen\b",
            r"现在需要",
            r"下一步",
            r"接下来",
            r"然后",
        ]
        marker_pattern = "|".join(split_markers)
        text = re.split(marker_pattern, text, maxsplit=1, flags=re.I)[0].strip(
            " .,:;，。；："
        )

        prefixes = [
            "the user wants me to ",
            "i need to ",
            "i should ",
            "let me ",
            "i will ",
            "现在我需要",
            "我需要",
            "接下来我需要",
            "让我",
            "现在开始",
            "好的，",
            "好，",
        ]
        lowered = text.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix.lower()):
                text = text[len(prefix) :].strip(" .,:;，。；：")
                lowered = text.lower()
                break

        action_lower = (action or "").lower()
        if action_lower == "sql_query":
            return "正在查询数据库信息"
        if action_lower == "code_interpreter":
            return "正在生成分析代码"
        if action_lower == "html_interpreter":
            return "正在生成并渲染 HTML 报告"
        if action_lower == "todowrite":
            return "正在更新任务计划"
        if action_lower in {"execute_skill_script", "execute_skill_script_file"}:
            return "正在执行分析脚本"

        return text

    skills_dir = DEFAULT_SKILLS_DIR
    registry = get_registry()

    # Step 1: Pre-load skills
    load_skills_from_dir(skills_dir, recursive=True)
    all_skills = registry.list_skills()

    # Step 2: Get business tools from ResourceManager
    rm = get_resource_manager(CFG.SYSTEM_APP)
    business_tools: List[Any] = []
    try:
        # Get all registered tool resources from ResourceManager
        tool_resources = rm._type_to_resources.get("tool", [])
        for reg_resource in tool_resources:
            if reg_resource.resource_instance is not None:
                business_tools.append(reg_resource.resource_instance)
    except Exception:
        pass  # If no business tools, continue with empty list

    # Step 3: Load knowledge space resource if specified in ext_info
    knowledge_resources: List[Any] = []
    knowledge_context = ""
    if knowledge_space:
        try:
            from dbgpt_serve.agent.resource.knowledge import (
                KnowledgeSpaceRetrieverResource,
            )

            knowledge_resource = KnowledgeSpaceRetrieverResource(
                name=f"knowledge_space_{knowledge_space}",
                space_name=knowledge_space,
                top_k=4,
                system_app=CFG.SYSTEM_APP,
            )
            knowledge_resources.append(knowledge_resource)
            codegraph_tools_desc = (
                """
  - kb_codegraph_explore: Query code structure (classes, call chains, inheritance)
  - kb_codegraph_call_chain: Trace who calls / is called by a function
  - kb_codegraph_class_hierarchy: Trace class inheritance and implementations
"""
                if code_graph_available
                else ""
            )
            knowledge_context = f"""
## Knowledge Base
- Knowledge space: {knowledge_resource.retriever_name or knowledge_space}
- Description: {knowledge_resource.retriever_desc or "Knowledge retrieval available"}
- Available tools:
  - kb_ls: List files and directories in the knowledge base
  - kb_glob: Search files by name or glob pattern
  - kb_grep: Search file contents by keyword (prefer for exact matches)
  - kb_cat: Read the content of a specific file
  - semantic_search: Semantic search (use when kb_grep returns insufficient results){codegraph_tools_desc}
"""
            logger.info(
                f"Loaded knowledge space resource: {knowledge_space} "
                f"(name: {knowledge_resource.retriever_name})"
            )
        except Exception as e:
            logger.warning(f"Failed to load knowledge space resource: {e}", exc_info=e)
            knowledge_context = f"""
## Knowledge Base
- Warning: Failed to load knowledge space '{knowledge_space}'. Error: {str(e)}
"""

    # Step 4: Load database connector if specified in ext_info
    database_connector = None
    database_context = ""
    if database_name:
        try:
            local_db_manager = ConnectorManager.get_instance(CFG.SYSTEM_APP)
            database_connector = local_db_manager.get_connector(database_name)
            table_names = list(database_connector.get_table_names())
            table_info = database_connector.get_table_info_no_throw()
            database_context = f"""
## 数据库信息
- 数据库名: {database_name}
- 可用表: {", ".join(table_names)}
- 表结构:
{table_info}
- 使用 'sql_query' 工具执行 SQL 查询
- **只允许 SELECT 查询，禁止 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE**
"""
            logger.info(
                f"Loaded database connector: {database_name} "
                f"(tables: {', '.join(table_names)})"
            )
        except Exception as e:
            logger.warning(f"Failed to load database connector: {e}", exc_info=e)
            database_context = f"""
## 数据库
- 警告: 加载数据库 '{database_name}' 失败。错误: {str(e)}
"""

    react_state: Dict[str, Any] = {
        "skills_loaded": True,  # Skills are pre-loaded now
        "matched": None,
        "skill_prompt": None,
        "file_path": file_path,
        "term_datasource": database_name or "",
        "resource_context": {
            "database": (
                {
                    "name": database_name,
                    "type": dialogue.ext_info.get("database_type")
                    or (database_connector.dialect if database_connector else ""),
                }
                if database_name
                else None
            )
        },
        "original_user_input": user_input,
        "term_check_pending": tool_mode == "full",
        "term_explanation_only": explanation_only_request(user_input),
    }
    if tool_mode == "full":
        from .tools.query_quality import require_explicit_ratio_results

        require_explicit_ratio_results(react_state)
    requires_report, requires_profile = report_requirements(user_input)
    react_state["requires_report"] = requires_report and tool_mode == "full"
    react_state["requires_database_profile"] = requires_profile and tool_mode == "full"

    # Pre-select skill if skill_name provided in ext_info
    pre_matched_skill = None
    if skill_name:
        pre_matched_skill = registry.get_skill(skill_name)
        if not pre_matched_skill:
            # Try case-insensitive match
            for s in registry.list_skills():
                if s.name.lower() == skill_name.lower():
                    pre_matched_skill = registry.get_skill(s.name)
                    break
        if pre_matched_skill:
            react_state["matched"] = pre_matched_skill
            react_state["skill_prompt"] = pre_matched_skill.get_prompt()
            logger.info(f"Pre-selected skill from ext_info: {skill_name}")

    # Build skills_context based on whether skill is pre-selected
    if pre_matched_skill:
        # User specified a skill: show only the selected skill
        skills_context = (
            f"- {pre_matched_skill.metadata.name}: "
            f"{pre_matched_skill.metadata.description}"
        )
    else:
        # User did not specify a skill: show all available skills
        skills_context = (
            "\n".join([f"- {s.name}: {s.description}" for s in all_skills])
            if all_skills
            else "No skills available."
        )

    def _mentions_excel(text: str) -> bool:
        lowered = text.lower()
        keywords = [
            "excel",
            "xlsx",
            "xls",
            "spreadsheet",
            "workbook",
            "sheet",
            "工作表",
            "表格",
            "电子表格",
        ]
        return any(keyword in lowered for keyword in keywords)

    def _is_excel_skill(meta) -> bool:
        name = (meta.name or "").lower()
        desc = (meta.description or "").lower()
        tags = [tag.lower() for tag in (meta.tags or [])]
        return any(
            token in name or token in desc or token in tags
            for token in ["excel", "xlsx", "xls", "spreadsheet"]
        )

    @tool(
        description="Select the most relevant skill based on user query from the "
        "available skills list in system prompt."
    )
    def select_skill(query: str) -> str:
        match_input = query or ""
        if react_state.get("file_path"):
            match_input = f"{match_input} excel xlsx spreadsheet file"
        matched = registry.match_skill(match_input)
        if (
            matched
            and _is_excel_skill(matched.metadata)
            and not (_mentions_excel(query) or react_state.get("file_path"))
        ):
            matched = None
        react_state["matched"] = matched
        if matched:
            detail = (
                f"Matched: {matched.metadata.name} - {matched.metadata.description}"
            )
            return json.dumps(
                {"chunks": [{"output_type": "text", "content": detail}]},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "chunks": [
                    {
                        "output_type": "text",
                        "content": "No skill matched; proceed without skill",
                    }
                ]
            },
            ensure_ascii=False,
        )

    @tool(
        description="Load skill content by skill name and file path. "
        "Returns the SKILL.md content of the specified skill. "
        '参数: {"skill_name": "技能名称", "file_path": "技能文件路径"}'
    )
    def load_skill(skill_name: str, file_path: str) -> str:
        """Load the skill content (SKILL.md) by skill name and file path.

        Args:
            skill_name: The name of the skill to load.
            file_path: The file path of the skill.
        """
        from dbgpt.agent.claude_skill import get_registry

        # Try to get skill from registry
        registry = get_registry()
        matched = registry.get_skill(skill_name)

        # If not found, try case-insensitive match
        if not matched:
            for s in registry.list_skills():
                if s.name.lower() == skill_name.lower():
                    matched = registry.get_skill(s.name)
                    break

        if not matched:
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": f"Skill '{skill_name}' not found",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        # Update react_state for compatibility with existing logic
        react_state["matched"] = matched
        react_state["skill_prompt"] = matched.get_prompt()

        # Build response content
        chunks = [
            {
                "output_type": "text",
                "content": f"Skill: {matched.metadata.name}",
            },
            {
                "output_type": "text",
                "content": f"File path: {file_path}",
            },
            {"output_type": "text", "content": "---"},
        ]

        # Add skill content/prompt
        if matched.instructions:
            chunks.append({"output_type": "markdown", "content": matched.instructions})
        elif matched.prompt_template:
            prompt_text = (
                matched.prompt_template.template
                if hasattr(matched.prompt_template, "template")
                else str(matched.prompt_template)
            )
            chunks.append({"output_type": "markdown", "content": prompt_text})

        return json.dumps({"chunks": chunks}, ensure_ascii=False)

    @tool(description="Load uploaded file info if provided.")
    def load_file() -> str:
        if not react_state.get("file_path"):
            return json.dumps(
                {"chunks": [{"output_type": "text", "content": "No file uploaded"}]},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "chunks": [
                    {"output_type": "text", "content": react_state["file_path"]},
                    {
                        "output_type": "text",
                        "content": "File path provided by user upload",
                    },
                ]
            },
            ensure_ascii=False,
        )

    @tool(description="Execute quick analysis on uploaded Excel/CSV file.")
    async def execute_analysis() -> str:
        from dbgpt.util.code.server import get_code_server

        matched = react_state.get("matched")
        if not react_state.get("file_path"):
            return json.dumps(
                {"chunks": [{"output_type": "text", "content": "No file to analyze"}]},
                ensure_ascii=False,
            )
        if matched and not _is_excel_skill(matched.metadata):
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": "Selected skill is not for Excel analysis",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        code_server = await get_code_server(CFG.SYSTEM_APP)
        analysis_code = """
import json
import pandas as pd

file_path = r"{file_path}"
if file_path.lower().endswith((".xls", ".xlsx")):
    df = pd.read_excel(file_path)
else:
    df = pd.read_csv(file_path)
summary = {{
    "shape": list(df.shape),
    "columns": list(df.columns),
    "dtypes": {{col: str(dtype) for col, dtype in df.dtypes.items()}},
    "head": df.head(5).to_dict(orient="records"),
}}
print(json.dumps(summary, ensure_ascii=False))
""".format(file_path=react_state["file_path"])
        result = await code_server.exec(analysis_code, "python")
        output_text = (
            result.output.decode("utf-8") if isinstance(result.output, bytes) else ""
        )
        chunks: List[Dict[str, Any]] = [
            {"output_type": "code", "content": analysis_code.strip()}
        ]
        if output_text:
            try:
                summary = json.loads(output_text)
                chunks.append({"output_type": "json", "content": summary})
                head_rows = summary.get("head")
                columns = summary.get("columns")
                if isinstance(head_rows, list) and isinstance(columns, list):
                    chunks.append(
                        {
                            "output_type": "table",
                            "content": {
                                "columns": [
                                    {"title": col, "dataIndex": col, "key": col}
                                    for col in columns
                                ],
                                "rows": head_rows,
                            },
                        }
                    )
                numeric_columns = [
                    col
                    for col, dtype in (summary.get("dtypes") or {}).items()
                    if "int" in dtype or "float" in dtype
                ]
                if numeric_columns and isinstance(head_rows, list):
                    series_col = numeric_columns[0]
                    data = [
                        {"x": idx + 1, "y": row.get(series_col)}
                        for idx, row in enumerate(head_rows)
                        if row.get(series_col) is not None
                    ]
                    if data:
                        chunks.append(
                            {
                                "output_type": "chart",
                                "content": {
                                    "data": data,
                                    "xField": "x",
                                    "yField": "y",
                                },
                            }
                        )
            except Exception:
                chunks.append({"output_type": "text", "content": output_text})
        return json.dumps({"chunks": chunks}, ensure_ascii=False)

    @tool(description="Resolve required tools for the selected skill.")
    def load_tools() -> str:
        from dbgpt.agent.resource.base import AgentResource, ResourceType

        matched = react_state.get("matched")
        rm = get_resource_manager(CFG.SYSTEM_APP)
        required_tools = matched.metadata.required_tools if matched else []
        if not required_tools:
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": "No required tools specified",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        loaded = []
        failed = []
        for tool_name in required_tools:
            try:
                rm.build_resource_by_type(
                    ResourceType.Tool.value,
                    AgentResource(type=ResourceType.Tool.value, value=tool_name),
                )
                loaded.append(tool_name)
            except Exception as e:
                failed.append(f"{tool_name} ({e})")
        chunks = []
        if loaded:
            chunks.append(
                {"output_type": "text", "content": f"Loaded: {', '.join(loaded)}"}
            )
        if failed:
            chunks.append(
                {"output_type": "text", "content": f"Failed: {', '.join(failed)}"}
            )
        return json.dumps({"chunks": chunks}, ensure_ascii=False)

    @tool(description="Execute a tool by name with JSON args.")
    async def execute_tool(tool_name: str, args: dict) -> str:
        from dbgpt.agent.resource.base import AgentResource, ResourceType

        try:
            from dbgpt.agent.resource.connector.confirmation import (
                _PENDING_CONFIRMATIONS,
            )
            from dbgpt.agent.resource.connector.manager import (
                ConnectorManager as _ConnectorManager,
            )

            _cm = CFG.SYSTEM_APP.get_component(
                "connector_manager", _ConnectorManager, default_component=None
            )
            if _cm is not None:
                _interceptor = _cm.get_confirmation_interceptor()
                _registry = _cm.get_confirmation_registry()
                if _interceptor.should_confirm(tool_name, args):
                    import asyncio as _asyncio
                    import uuid as _uuid

                    _confirm_id = str(_uuid.uuid4())
                    _registry.register(_confirm_id)
                    _PENDING_CONFIRMATIONS[_confirm_id] = {
                        "confirm_id": _confirm_id,
                        "tool_name": tool_name,
                        "args_summary": _interceptor._summarize_args(args),
                        "message": f"即将执行写操作 {tool_name}，是否确认？",
                        "timeout": 300,
                    }
                    try:
                        _approved = await _asyncio.wait_for(
                            _registry.wait_for(_confirm_id), timeout=300
                        )
                    except _asyncio.TimeoutError:
                        _approved = False
                    finally:
                        _PENDING_CONFIRMATIONS.pop(_confirm_id, None)
                    if not _approved:
                        return json.dumps(
                            {
                                "chunks": [
                                    {
                                        "output_type": "text",
                                        "content": "用户拒绝了此操作，工具执行已取消。",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
        except Exception:
            pass

        rm = get_resource_manager(CFG.SYSTEM_APP)
        try:
            # Primary path: lookup via ResourceManager (lazy-registered business tools)
            tool_resource = rm.build_resource_by_type(
                ResourceType.Tool.value,
                AgentResource(type=ResourceType.Tool.value, value=tool_name),
            )
            tool_pack = ToolPack([tool_resource])
            result = await tool_pack.async_execute(resource_name=tool_name, **args)
            return json.dumps(
                {"chunks": [{"output_type": "text", "content": str(result)}]},
                ensure_ascii=False,
            )
        except Exception as primary_exc:
            # Fallback: if ResourceManager doesn't have the tool, try
            # ConnectorManager active packs.  This handles the case where LLM
            # mistakenly routes an MCP connector tool through execute_tool
            # instead of calling it directly.
            try:
                from dbgpt.agent.resource.connector.manager import (
                    ConnectorManager as _ConnectorManager,
                )

                _cm = CFG.SYSTEM_APP.get_component(
                    "connector_manager",
                    _ConnectorManager,
                    default_component=None,
                )
                if _cm is not None:
                    for _cid, _pack in _cm._active_packs.items():
                        if tool_name in _pack._resources:
                            result = await _pack.async_execute(
                                resource_name=tool_name, **args
                            )
                            logger.info(
                                "execute_tool dispatched '%s' via "
                                "ConnectorManager fallback (connector=%s). "
                                "Prefer direct Action call for connector "
                                "tools.",
                                tool_name,
                                _cid,
                            )
                            return json.dumps(
                                {
                                    "chunks": [
                                        {
                                            "output_type": "text",
                                            "content": str(result),
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
            except Exception as fallback_exc:
                logger.warning(
                    "execute_tool fallback to ConnectorManager failed for '%s': %s",
                    tool_name,
                    fallback_exc,
                )
                # When fallback found the tool but execution failed, surface
                # that error to the LLM (more actionable than the
                # ResourceManager primary error)
                return json.dumps(
                    {
                        "chunks": [
                            {
                                "output_type": "text",
                                "content": (
                                    f"Tool execute failed: {fallback_exc} "
                                    f"(primary lookup error: {primary_exc})"
                                ),
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            # Both lookups returned None — primary path's tool-not-found error wins
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": f"Tool execute failed: {primary_exc}",
                        }
                    ]
                },
                ensure_ascii=False,
            )

    @tool(
        description="Retrieve relevant information from the knowledge base. "
        "Use this tool when the user question involves content that may be "
        'in the knowledge base. Parameters: {{"query": "search query"}}'
    )
    async def knowledge_retrieve(query: str) -> str:
        if not knowledge_resources:
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": "No knowledge base available",
                        }
                    ]
                },
                ensure_ascii=False,
            )

    # ── Import built-in tools from tools/ directory ──
    from dbgpt_app.openapi.api_v1.tools import (
        make_code_interpreter,
        make_database_profile,
        make_execute_analysis,
        make_execute_skill_script_file,
        make_execute_tool,
        make_html_interpreter,
        make_kb_tools,
        make_knowledge_retrieve,
        make_load_file,
        make_load_skill,
        make_load_tools,
        make_question,
        make_read_file,
        make_shell_interpreter,
        make_sql_query,
        make_todowrite,
    )
    # ── Build tool instances via factory functions ──────────────────────────
    # (Inline @tool definitions have been moved to tools/ directory.)
    # Local helper aliases used by the SSE loop are defined below.

    # ── Stream queue (created early so question tool can use it) ────────
    stream_queue: asyncio.Queue = asyncio.Queue()

    async def stream_callback(event_type: str, payload: Dict[str, Any]) -> None:
        await stream_queue.put({"type": event_type, **payload})

    # ── Build tool instances from tools/ directory ───────────────────────
    _todo_list: List[Dict[str, str]] = []
    react_state["task_plan"] = _todo_list
    # Reuse one visible plan card across all todowrite updates in this round.
    # The mutable single-item list acts as a closure cell.
    _todo_step_holder: List[str] = []
    load_skill_tool = make_load_skill(react_state)
    load_file_tool = make_load_file(react_state)
    execute_analysis_tool = make_execute_analysis(react_state)
    load_tools_tool = make_load_tools(react_state)
    execute_tool_tool = make_execute_tool(react_state)
    # Knowledge tools: use kb_tools (kb_ls, kb_glob, kb_grep, kb_cat, semantic_search)
    # when a knowledge space is connected, otherwise fall back to knowledge_retrieve
    if knowledge_space:
        kb_tool_list = make_kb_tools(knowledge_space)
        # Filter out codegraph tools when the space has no built code graph,
        # so the agent never sees tools it cannot use successfully.
        # @tool decorator wraps the function; the tool name lives on `._tool.name`
        # (and `.__name__` via functools.wraps).
        if not code_graph_available:
            kb_tool_list = [
                t
                for t in kb_tool_list
                if not getattr(getattr(t, "_tool", t), "name", "").startswith(
                    "kb_codegraph"
                )
            ]
    else:
        # No knowledge space connected — use legacy knowledge_retrieve (no-op without resources)
        kb_tool_list = [make_knowledge_retrieve(react_state, knowledge_resources)]
    sql_query_tool = make_sql_query(react_state, database_connector)
    database_profile_tool = make_database_profile(react_state, database_connector)
    code_interpreter_tool = make_code_interpreter(react_state)
    shell_interpreter_tool = make_shell_interpreter(react_state)
    html_interpreter_tool = make_html_interpreter(react_state, DEFAULT_SKILLS_DIR)
    todowrite_tool = make_todowrite(_todo_list, stream_callback, react_state)
    question_tool = make_question(react_state, stream_callback)
    # read_file lets the agent read back persisted tool results / snapshots
    # from disk when a <persisted-output> block references a file path.
    read_file_tool = make_read_file(react_state)
    # Keep local aliases for backward compatibility (SSE loop references these names)
    execute_skill_script_file_tool = make_execute_skill_script_file(react_state)

    _todo_action_history: Dict[int, List[str]] = {}

    def _active_todo_index() -> Optional[int]:
        for idx, item in enumerate(_todo_list):
            if item.get("status") == "in_progress":
                return idx
        return None

    def _normalize_text(value: Optional[str]) -> str:
        return (value or "").strip().lower()

    def _is_report_like(text: str) -> bool:
        keywords = [
            "report",
            "html",
            "dashboard",
            "visual",
            "visualization",
            "图表",
            "报告",
            "报表",
            "可视化",
            "渲染",
            "展示",
        ]
        return any(keyword in text for keyword in keywords)

    def should_advance_todo(
        action_name: Optional[str],
        thought: Optional[str] = None,
        observation_text: Optional[str] = None,
    ) -> bool:
        """Heuristically decide whether the current todo is actually complete."""
        if not _todo_list:
            return False

        active_idx = _active_todo_index()
        if active_idx is None:
            return False

        action_lower = _normalize_text(action_name)
        thought_lower = _normalize_text(thought)
        observation_lower = _normalize_text(observation_text)
        current_todo = _normalize_text(_todo_list[active_idx].get("content"))
        next_todo = (
            _normalize_text(_todo_list[active_idx + 1].get("content"))
            if active_idx + 1 < len(_todo_list)
            else ""
        )

        history = _todo_action_history.setdefault(active_idx, [])
        if action_lower:
            history.append(action_lower)

        transition_markers = [
            "next step",
            "now i need",
            "now i should",
            "now let me",
            "现在需要",
            "下一步",
            "接下来",
            "然后",
            "接着",
            "接下来我将",
        ]
        if next_todo and any(marker in thought_lower for marker in transition_markers):
            if any(token and token in thought_lower for token in next_todo.split()):
                return True

        if action_lower == "html_interpreter":
            return True

        if action_lower in {
            "load_skill",
            "execute_skill_script",
            "execute_skill_script_file",
        }:
            if next_todo and next_todo in thought_lower:
                return True
            if _is_report_like(next_todo) and _is_report_like(thought_lower):
                return True

        if action_lower == "sql_query":
            sql_calls = sum(1 for item in history if item == "sql_query")
            if sql_calls < 3:
                return False

            if next_todo and any(
                token and token in thought_lower for token in next_todo.split()
            ):
                return True

            if _is_report_like(next_todo) and (
                "summary" in thought_lower
                or "summarize" in thought_lower
                or "整理" in thought_lower
                or "汇总" in thought_lower
                or "报告" in thought_lower
            ):
                return True

            if current_todo and not _is_report_like(current_todo):
                completion_markers = [
                    "enough information",
                    "collected enough",
                    "gathered enough",
                    "completed metadata",
                    "obtained the overview",
                    "获取了足够",
                    "已经获取了足够",
                    "已完成",
                    "已获取",
                    "整理一下",
                ]
                if any(marker in thought_lower for marker in completion_markers):
                    return True

            return False

        if action_lower in {"code_interpreter", "execute_tool", "shell_interpreter"}:
            if _is_report_like(current_todo):
                return False
            if next_todo and any(
                token and token in thought_lower for token in next_todo.split()
            ):
                return True
            if observation_lower and _is_report_like(observation_lower):
                return True

        return False

    def advance_todo_list() -> Optional[List[Dict[str, str]]]:
        """Advance one todo when the current task appears substantively complete."""
        if not _todo_list:
            return None

        changed = False
        active_idx = _active_todo_index()

        if active_idx is not None:
            _todo_list[active_idx]["status"] = "completed"
            changed = True
            _todo_action_history.pop(active_idx, None)
            for next_item in _todo_list[active_idx + 1 :]:
                if next_item.get("status") == "pending":
                    next_item["status"] = "in_progress"
                    _todo_action_history.pop(active_idx + 1, None)
                    break
        else:
            for item in _todo_list:
                if item.get("status") == "pending":
                    item["status"] = "in_progress"
                    changed = True
                    break

        return list(_todo_list) if changed else None

    llm_client = DefaultLLMClient(
        CFG.SYSTEM_APP.get_component(
            ComponentType.WORKER_MANAGER_FACTORY, WorkerManagerFactory
        ).create(),
        auto_convert_message=True,
    )
    if dialogue.model_name:
        llm_config = LLMConfig(
            llm_client=llm_client,
            llm_strategy=LLMStrategyType.Priority,
            strategy_context=json.dumps([dialogue.model_name]),
        )
    else:
        llm_config = LLMConfig(llm_client=llm_client)

    term_clarifier = TermClarifier(
        react_state,
        stream_callback,
        make_json_llm(llm_client, dialogue.model_name),
        database_connector=database_connector,
    )
    clarify_term_tool = make_clarify_term(term_clarifier)

    conv_id = dialogue.conv_uid or str(uuid.uuid4())
    react_state["conv_id"] = conv_id
    if attachment_ctx is not None:
        # Public manifests for runtime tools plus the internal primary
        # materialized path / files_json mapping (execution-only values —
        # never written to prompts, logs or history).
        react_state.update(react_state_patch(attachment_ctx))
    # Public per-turn snapshot of input files for the persisted history
    # payload (v2). Only public metadata — never server paths, storage URIs,
    # owner ids, hashes or inspection bodies. Later turns resolve files
    # fresh from the registry; old payloads are never scanned for files.
    input_files_snapshot = build_input_files_v2(
        attachment_ctx.manifests if attachment_ctx is not None else ()
    )
    if conv_id in REACT_AGENT_MEMORY_CACHE:
        gpt_memory = REACT_AGENT_MEMORY_CACHE[conv_id]
    else:
        gpt_memory = GptsMemory(
            plans_memory=DefaultGptsPlansMemory(),
            message_memory=MetaDbGptsMessageMemory(),
        )
        gpt_memory.init(conv_id, enable_vis_message=False)
        REACT_AGENT_MEMORY_CACHE[conv_id] = gpt_memory
    agent_memory = AgentMemory(gpts_memory=gpt_memory)

    conv_serve = ConversationServe.get_instance(CFG.SYSTEM_APP)
    storage_conv = StorageConversation(
        conv_uid=conv_id,
        chat_mode=dialogue.chat_mode or "chat_react_agent",
        user_name=dialogue.user_name,
        sys_code=dialogue.sys_code,
        summary=dialogue.user_input,
        app_code=dialogue.app_code,
        conv_storage=conv_serve.conv_storage,
        message_storage=conv_serve.message_storage,
    )
    storage_conv.save_to_storage()
    storage_conv.start_new_round()
    # Load the full conversation history (user question + agent final answer)
    # before appending the current round, then pass it as historical_dialogues
    # so multi-turn follow-ups see the previous Q&A (mirrors hermes'
    # conversation_history passed into the loop).
    historical_dialogues: List[AgentMessage] = []
    for _msg in storage_conv.get_history_message():
        if _msg.type == "human":
            historical_dialogues.append(AgentMessage(content=_msg.content))
            react_state["previous_user_input"] = _msg.content
        elif _msg.type == "ai":
            historical_dialogues.append(AgentMessage(content=_msg.content))
        elif _msg.type == "view":
            # view 消息存的是 history_payload(JSON)，提取 final_content 作为 AI 回答
            _content = _msg.content
            try:
                _payload = (
                    json.loads(_content) if isinstance(_content, str) else _content
                )
                if isinstance(_payload, dict):
                    _content = _payload.get("final_content") or ""
                    previous_resources = _payload.get("resource_context")
                    previous_database = (
                        previous_resources.get("database")
                        if isinstance(previous_resources, dict)
                        else None
                    )
                    react_state["previous_resource_context"] = {
                        "database": (
                            {
                                "name": previous_database["name"],
                                "type": previous_database["type"],
                            }
                            if isinstance(previous_database, dict)
                            and isinstance(previous_database.get("name"), str)
                            and isinstance(previous_database.get("type"), str)
                            else None
                        )
                    }
                    if isinstance(_payload.get("task_requirements"), dict):
                        react_state["task_requirements"] = _payload["task_requirements"]
                        react_state.pop("legacy_requirements_unresolved", None)
                    for previous_term in _payload.get("term_definitions", []):
                        if previous_term.get("datasource", "") == (database_name or ""):
                            confirmed = react_state.setdefault("confirmed_terms", [])
                            confirmed[:] = [
                                t
                                for t in confirmed
                                if t["term"] != previous_term["term"]
                            ]
                            confirmed.append(previous_term)
                    if not isinstance(_payload.get("task_requirements"), dict):
                        react_state["task_requirements"] = requirements_from_input(
                            react_state.get("previous_user_input", ""),
                            react_state.get("confirmed_terms", []),
                            database_name or "",
                        )
                        react_state["legacy_requirements_unresolved"] = not react_state[
                            "task_requirements"
                        ]["ratios"] and bool(
                            legacy_ratio_names(
                                react_state.get("previous_user_input", "")
                            )
                        )
            except Exception:
                pass
            if _content:
                historical_dialogues.append(AgentMessage(content=_content))
    storage_conv.add_user_message(user_input)
    context = AgentContext(
        conv_id=conv_id,
        gpts_app_code="react_agent",
        gpts_app_name="ReAct",
        language="zh",
        temperature=dialogue.temperature or 0.2,
        enable_context_management=True,
        enable_native_function_calling=False,
    )

    # file_ids requests use the public manifest block; legacy file_path and
    # pure-text requests keep their existing wording byte-for-byte.
    file_context = build_file_context(attachment_ctx, file_path)

    skill_prompt_context = ""
    execution_instruction = ""
    if pre_matched_skill and react_state.get("skill_prompt"):
        skill_template = react_state["skill_prompt"]
        skill_text = (
            skill_template.template
            if hasattr(skill_template, "template")
            else str(skill_template)
        )
        skill_prompt_context = f"""
## 已加载技能指令（{pre_matched_skill.metadata.name}）
以下是用户选择的技能的完整指令，请严格按照这些指令进行操作：

{skill_text}
"""
        execution_instruction = f"""
## 执行要求
1. 用户已明确选择技能：{pre_matched_skill.metadata.name}
2. 你必须严格按照上述技能指令的步骤执行
3. 阅读技能指令，理解每一步需要调用的工具
4. 按顺序执行工具调用，完成技能目标
"""

    # Build a hint listing all images currently available in
    # STATIC_MESSAGE_IMG_PATH so the LLM can reference them correctly in
    # html_interpreter.
    # NOTE: This is the initial hint at prompt build time. Images generated
    # during the session are tracked in react_state["generated_images"] and
    # appended to html_interpreter output dynamically.
    available_images_hint = ""

    # Check if skill is pre-selected to use simplified prompt
    is_skill_mode = pre_matched_skill is not None
    _skill_name = pre_matched_skill.metadata.name if pre_matched_skill else "skill"

    # Inject connector tools — only the ones the user explicitly selected.
    connector_tool_extras: List[Any] = []
    _connector_manager = None
    try:
        from dbgpt.agent.resource.connector.manager import (
            ConnectorManager as _ConnectorManager,
        )

        _connector_manager = CFG.SYSTEM_APP.get_component(
            "connector_manager", _ConnectorManager, default_component=None
        )
        if _connector_manager is not None and connector_ids:
            connector_tool_extras, _missing = _select_connector_tools(
                connector_ids, _connector_manager
            )
            for _mid in _missing:
                logger.warning(
                    "_react_agent_stream: connector_id %s not active, skipping",
                    _mid,
                )
            if connector_tool_extras:
                logger.info(
                    "_react_agent_stream: injected %d connector tool pack(s) "
                    "(selected: %d)",
                    len(connector_tool_extras),
                    len(connector_ids),
                )
    except Exception:
        pass  # graceful degradation — connector tools are optional

    # Parallel sub-agent dispatch is exposed only in the full-tool branch
    # below. It shares the main stream queue so child lifecycle events and
    # question events use the same ordered SSE channel.
    _max_parallel_subagents = 3
    try:
        _app_cfg = CFG.SYSTEM_APP.config.configs.get("app_config")
        _web_cfg = getattr(getattr(_app_cfg, "service", None), "web", None)
        _agent_ctx = getattr(_web_cfg, "agent_context", None)
        _cfg_val = getattr(_agent_ctx, "max_parallel_subagents", None)
        if isinstance(_cfg_val, int) and _cfg_val > 0:
            _max_parallel_subagents = _cfg_val
    except Exception:
        logger.debug("Failed to read max_parallel_subagents; using default 3")

    async def _emit_subagent_event(payload: Dict[str, Any]) -> None:
        await stream_queue.put(payload)

    dispatch_parallel_tasks = make_dispatch_tool(
        parent_conv_id=conv_id,
        llm_client=llm_client,
        sub_model_name=dialogue.model_name,
        database_connector=database_connector,
        knowledge_resources=knowledge_resources,
        connector_tool_extras=connector_tool_extras,
        connector_manager=_connector_manager,
        emit_event=_emit_subagent_event,
        max_parallel=_max_parallel_subagents,
    )

    if is_skill_mode:
        # Simplified prompt for skill mode - only skill-related tools +
        # html_interpreter
        workflow_prompt = f"""
You are the DB-GPT intelligent assistant, executing the skill task selected by the user.
Please always response in the same language as the user's input language.

## Autonomous Decision Principles
1. Strictly follow the instructions of the loaded skill.
2. For each step, output Thought -> Action Intention -> Action Reason -> Action
   -> Action Input.
3. Wait for the system to return Observation before deciding on the next step.
4. **[Mandatory Rule] If the task requires generating an analysis report, you MUST
call `html_interpreter` for HTML rendering.** By default, generate complete HTML
code yourself and pass it via the `html` parameter (include DOCTYPE, html, head,
body, styles, and all content). Only use `template_path` mode if the skill
explicitly provides HTML templates in its `templates/` directory and its
documentation references them. When using template mode, provide ALL required
placeholders in the `data` dictionary.
5. If the task does not require generating a report, directly call terminate to
return the final result. The Action Input format must be
{{"output": "final answer"}}.

{skill_prompt_context}
{execution_instruction}

## Skill Execution Norms
### Resource Usage
- **Need to execute skill script** -> Use `execute_skill_script_file` with
parameters {{"skill_name": "skill name", "script_file_name": "script file name",
"args": {{parameters}}}}. This tool will automatically handle image copying and
data recording.
- **Need to understand indicator definitions/analysis framework** -> Use
`get_skill_resource` and specify the `references/xxx.md` path to read the
reference document.
- **Encounter image file** -> If the model does not support image input, it will
return an error prompt.
- **Need to generate report** -> Call `html_interpreter`. **Default: directly pass
complete HTML via the `html` parameter** — you generate the full HTML code
yourself (including `<!DOCTYPE html>`, `<html>`, `<head>`, `<body>`, styles,
content). The HTML can be as long as needed. **Only use `template_path` if the
skill explicitly provides HTML templates in its `templates/` directory and its
documentation tells you to use them.** Do not use `code_interpreter` to generate
the report.

## Available Tools Description
1. **execute_skill_script_file** (recommended for executing skill scripts): Execute
script files in the skills scripts directory, automatically handling
post-processing such as copying images to the static directory and recording
calculation results.
   Parameters: {{"skill_name": "skill name", "script_file_name": "script file
name", "args": {{parameters}}}}
   - Example: {{"skill_name": "{_skill_name}",
"script_file_name": "calculate_ratios.py",
"args": {{"input_data": "..."}}}}
   - **Must use this tool when executing skill scripts**, do not use
shell_interpreter.
2. **get_skill_resource**: Read reference documents, configurations, templates, and
other non-script resource files in the skill.
   Parameters: {{"skill_name": "skill name", "resource_path": "resource path"}}
   - Read reference document: {{"skill_name": "{_skill_name}",
"resource_path": "references/analysis_framework.md"}}
   - Note: For generating reports, prefer using html_interpreter directly with the
`html` parameter. Only use template_path if the skill explicitly provides
templates.
3. **execute_skill_script**: Execute the inline script defined in the skill
(backup). Parameters: {{"skill_name": "skill name", "script_name": "script name",
"args": {{"parameter name": "parameter value"}}}}
4. **shell_interpreter**: Execute shell/bash commands (only for non-skill script
system commands, such as ls, cat, etc.).
   Parameters: {{"code": "shell command"}}
   - Each call is independent and does not retain state. If multi-step operations
are needed, use `&&` or `;` to connect commands.
   - **Note: Do not use this tool to execute skill scripts**, as it will not
automatically handle images and data recording.
5. **html_interpreter**: Render HTML as an interactive web report. This is the ONLY
way to display reports on the right panel.
   **Default usage (recommended)**: {{"html": "<html>your complete HTML code</html>",
"title": "report title"}}
   - Generate complete HTML yourself (DOCTYPE, html, head, body, CSS styles,
content). No length limit.
   - **Do not** use code_interpreter to write HTML. Directly pass the HTML string
to this tool.
   **Template mode (only when skill has templates/)**: {{"template_path":
"skill-name/templates/template.html", "data": {{"KEY": "value"}}, "title": "title"}}
   - Only use this if the skill's documentation explicitly provides template paths.
If template_path returns "Template not found", immediately switch to the default
`html` parameter usage.
   {available_images_hint}
6. **sql_query**: Execute a read-only SQL query against the selected database.
Parameters: {{"sql": "SELECT statement"}}
7. **todowrite**: Create and manage a structured task list. Use for complex tasks
(3+ steps) to plan and track progress. Pass the FULL list every time. Each item:
{{"content": "description", "status": "pending|in_progress|completed|cancelled",
"priority": "high|medium|low"}}. Only ONE task in_progress at a time.
IMPORTANT: You MUST call todowrite again after EACH task completes to update status.
The user sees progress in real time — never skip an update.
Parameters: {{"todos": [{{...}}]}}
8. **question**: Ask the user a question and wait for their response. Use this tool
   when you need user input, clarification, or a decision to proceed. The tool blocks
   until the user answers.
   Parameters: {{"questions": [{{"question": "...", "header": "...", "options": [
   {{"label": "...", "description": "..."}}, ...]}}]}}. Set multiple=true to allow
   multiple selections. The tool returns the user's selected answers.
9. **terminate**: Return the final answer when the task is completed. Action Input
must be {{"result": "your final answer content"}}.

## Task Management
For complex tasks that require 3 or more steps, use the `todowrite` tool to create
a structured task plan BEFORE starting work. This helps users track your progress.
- Call `todowrite` with the FULL todo list (all items) each time you update.
- Mark exactly ONE task as `in_progress` at a time.
- Mark tasks `completed` immediately after finishing each one.
- Do NOT use todowrite for simple single-step tasks.

CRITICAL: You MUST call `todowrite` to update the task list at EVERY transition:
1. BEFORE starting a task: mark it `in_progress` (call todowrite)
2. AFTER finishing a task: mark it `completed` AND mark the next one
   `in_progress` (call todowrite)
3. Never skip updating — the user sees this progress in real time.
Example flow for 3 tasks:
- Create plan: [task1=in_progress, task2=pending, task3=pending] → call todowrite
- Finish task1: [task1=completed, task2=in_progress, task3=pending] → call todowrite
- Finish task2: [task1=completed, task2=completed, task3=in_progress] → call todowrite
- Finish task3: [task1=completed, task2=completed, task3=completed] → call todowrite

{file_context}
{knowledge_context}
{database_context}
## ReAct Output Format
Must output for each interaction round:
Thought: Analyze current task status and think about what to do next
Action Intention: What this step will do, plain text, MUST be concise and fit in
<= 18 Chinese chars or <= 8 English words. If too long, rewrite shorter.
Do not use ellipsis.
Action Reason: Why this action is needed now, plain text, MUST be concise and fit in
<= 30 Chinese chars or <= 12 English words. If too long, rewrite shorter.
Do not use ellipsis.
Action: The selected tool name (must be one of the tools listed above)
Action Input: The JSON format of tool parameters

IMPORTANT: Never emit native tool-call markup such as
<|tool_calls_section_begin|>, <|tool_call_begin|>, <|tool_call_argument_begin|>
or any other <|...|> tokens. Tool calls are ONLY valid in the textual
Thought/Action/Action Input format shown above.
""".strip()

        if tool_mode == "knowledge":
            # Knowledge-chat mode: only kb tools + todowrite + terminate.
            # No skill/shell/sql/html/code tools to keep the agent focused.
            tool_pack = ToolPack(
                [todowrite_tool, question_tool, Terminate()]
                + business_tools
                + connector_tool_extras
            )
        else:
            tool_pack = ToolPack(
                [
                    execute_skill_script,
                    get_skill_resource,
                    execute_skill_script_file_tool,
                    shell_interpreter_tool,
                    html_interpreter_tool,
                    sql_query_tool,
                    database_profile_tool,
                    todowrite_tool,
                    question_tool,
                    Terminate(),
                ]
                + business_tools
                + connector_tool_extras
            )
    else:
        # Full prompt with all tools when no skill is pre-selected
        codegraph_section = (
            "13.1. **kb_codegraph_explore**: Query the code knowledge graph for "
            "structural info (classes, call chains, inheritance).\n"
            'Parameters: {"query": "class/function name or \'who calls X\'"}\n'
            "13.2. **kb_codegraph_call_chain**: Trace callers/callees of a function.\n"
            'Parameters: {"function_name": "function name", "depth": 2, '
            '"direction": "callers or callees"}\n'
            "13.3. **kb_codegraph_class_hierarchy**: Trace class inheritance and "
            "implementations.\n"
            'Parameters: {"class_name": "class or interface name"}\n'
            if code_graph_available
            else ""
        )
        workflow_prompt = f"""
You are the DB-GPT intelligent assistant, capable of autonomously selecting tools
to solve problems based on user tasks.
Please always response in the same language as the user's input language.

## Autonomous Decision Principles
1. Carefully analyze the user's task requirements.
2. Autonomously select required tools based on requirements (do not follow a fixed
order, select as needed).
3. For each step, output Thought -> Action Intention -> Action Reason -> Action
   -> Action Input.
4. Wait for the system to return Observation before deciding on the next step.
5. When the task is completed, call the terminate tool to return the final result.
The Action Input format must be {{"output": "final answer"}}.
6. **[Mandatory Rule] If there is a requirement for an analysis report, you MUST call
`html_interpreter` for HTML rendering. When the user requests generating a webpage,
HTML report, or interactive report, the final presentation step must call
`html_interpreter` to render it. It is forbidden to output HTML using only
`code_interpreter` and then directly terminate. Correct process: code_interpreter
writes to .html file -> html_interpreter(file_path=...) renders -> terminate.**

## Task Management
For complex tasks that require 3 or more steps, use the `todowrite` tool to create
a structured task plan BEFORE starting work. This helps users track your progress.
- Call `todowrite` with the FULL todo list (all items) each time you update.
- Mark exactly ONE task as `in_progress` at a time.
- Mark tasks `completed` immediately after finishing each one.
- Do NOT use todowrite for simple single-step tasks.
- Parallel exception: when the task contains 2 or more mutually independent
  subtasks eligible for `dispatch_parallel_tasks`, use `todowrite` even when
  the overall task has fewer than 3 steps.
- After splitting tasks with `todowrite`, delegate independent items with no
  ordering dependency to `dispatch_parallel_tasks` (one sub-agent per item).
  `todowrite` decomposes and tracks work; `dispatch_parallel_tasks` only executes
  already-split items.
- The "exactly one in_progress" rule controls the visible todo state; it does
  not prevent dispatching other independent pending items in the same batch.

CRITICAL: You MUST call `todowrite` to update the task list at EVERY transition:
1. BEFORE starting a task: mark it `in_progress` (call todowrite)
2. AFTER finishing a task: mark it `completed` AND mark the next one
   `in_progress` (call todowrite)
3. Never skip updating — the user sees this progress in real time.
Example flow for 3 tasks:
- Create plan: [task1=in_progress, task2=pending, task3=pending] → call todowrite
- Finish task1: [task1=completed, task2=in_progress, task3=pending] → call todowrite
- Finish task2: [task1=completed, task2=completed, task3=in_progress] → call todowrite
- Finish task3: [task1=completed, task2=completed, task3=completed] → call todowrite

## Available Skills List (Pre-loaded)
{skills_context}

## Skill Execution Norms (Important)
When using a skill, the following rules must be followed:

### 1. Understand the Workflow
After loading the skill, carefully read the **Core Workflow** section in SKILL.md
and execute it in order. If a step explicitly states conditions to skip (such as
when user intent is clear), directly skip to the next step; do not force the
execution of every step. Prioritize producing results quickly, and perform
iterative optimization in subsequent steps.

### 2. Resource Usage Timing
- **Need to calculate/process data** -> Use `execute_skill_script_file` to execute
scripts in the skill's scripts directory (this tool automatically handles images
and data recording). Parameters are {{"skill_name": "skill name",
"script_file_name": "script.py", "args": {{parameters}}}}.
- **Need to understand indicator definitions/analysis framework** -> Use
`get_skill_resource` and specify the `references/xxx.md` path to read the
reference document.
- **Encounter image file** -> If the model does not support image input, it will
return an error prompt.

### 3. Execution Order
Complete each workflow step before moving to the next. Do not mix multiple tool
calls in the same step.

### 4. Special Scenarios
- For report generation: Same as the principle above, must finally call
`html_interpreter` to render.

## Available Tools Description
1. **load_skill**: Load skill content by skill name and file path.
Parameters: {{"skill_name": "skill name", "file_path": "skill file path"}}
2. **execute_skill_script_file**: Execute script files in the skill's scripts
directory. Parameters: {{"skill_name": "skill name",
"script_file_name": "script file name", "args": {{parameters}}}}
3. **get_skill_resource**: Read reference documents in the skill.
Parameters: {{"skill_name": "skill name", "resource_path": "resource path"}}
4. **execute_skill_script**: Execute the inline script defined in the skill.
Parameters: {{"skill_name": "skill name", "script_name": "script name",
"args": {{parameters}}}}
5. **shell_interpreter**: Execute shell/bash commands.
Parameters: {{"code": "shell command"}}
6. **code_interpreter**: Execute arbitrary Python code.
Parameters: {{"code": "python code string"}}
7. **load_file**: Load uploaded file info. Parameters: none.
8. **execute_analysis**: Execute quick analysis on uploaded Excel/CSV file.
Parameters: none.
9. **kb_ls**: List files and directories in the knowledge base.
Parameters: {{"path": "directory path (optional)"}}
10. **kb_glob**: Search files by name or glob pattern in the knowledge base.
Parameters: {{"pattern": "file name keyword or glob pattern"}}
11. **kb_grep**: Search file contents by keyword in the knowledge base. Prefer for exact matches.
Parameters: {{"query": "search keyword", "path": "directory filter (optional)", "file_pattern": "file pattern like *.py (optional)"}}
12. **kb_cat**: Read the content of a specific file in the knowledge base.
Parameters: {{"path": "file path like src/main.py", "start_line": "start line (optional)", "end_line": "end line (optional, 0 = to end)"}}
13. **semantic_search**: Semantic search in the knowledge base. Use when kb_grep returns insufficient results.
Parameters: {{"query": "search query in natural language", "top_k": "number of results (optional)"}}
{codegraph_section}14. **sql_query**: Execute a read-only SQL query against the selected database.
Parameters: {{"sql": "SELECT statement"}}
15. **load_tools**: Resolve required tools for the selected skill. Parameters: none.
16. **execute_tool**: Execute a tool by name with JSON args.
Parameters: {{"tool_name": "tool name", "args": {{parameters}}}}
17. **html_interpreter**: Render HTML as an interactive web report (the ONLY way
to display reports on the right panel). Default usage:
{{"html": "<html>complete HTML code</html>", "title": "title"}}. Template mode:
{{"template_path": "skill/templates/xxx.html", "data": {{...}}, "title": "title"}}.
File mode: {{"file_path": "/path/to/report.html"}}
14. **todowrite**: Create and manage a structured task list. Use for complex tasks
(3+ steps) to plan and track progress. Pass the FULL list every time. Each item:
{{"content": "description", "status": "pending|in_progress|completed|cancelled",
"priority": "high|medium|low"}}. Only ONE task in_progress at a time.
IMPORTANT: You MUST call todowrite again after EACH task completes to update status.
The user sees progress in real time — never skip an update.
Parameters: {{"todos": [{{...}}]}}
15. **dispatch_parallel_tasks**: Execute 2 or more mutually independent todo
items concurrently with isolated sub-agents. Each task needs a self-contained
goal and may include shared context and a display title.
Parameters: {{"tasks": [{{"goal": "...", "context": "...", "title": "..."}}]}}
16. **question**: Ask the user a question and wait for their response. Use this tool
   when you need user input, clarification, or a decision to proceed. The tool blocks
   until the user answers.
   Parameters: {{"questions": [{{"question": "...", "header": "...", "options": [
   {{"label": "...", "description": "..."}}, ...]}}]}}. Set multiple=true to allow
   multiple selections. The tool returns the user's selected answers.
17. **terminate**: Finish the task. Parameters: {{"output": "final answer"}}

{file_context}
{knowledge_context}
{database_context}

## ReAct Output Format
Must output for each interaction round:
Thought: Analyze current task status and think about what to do next
Action Intention: What this step will do, plain text, MUST be concise and fit in
<= 18 Chinese chars or <= 8 English words. If too long, rewrite shorter.
Do not use ellipsis.
Action Reason: Why this action is needed now, plain text, MUST be concise and fit in
<= 30 Chinese chars or <= 12 English words. If too long, rewrite shorter.
Do not use ellipsis.
Action: The selected tool name
Action Input: The JSON format of tool parameters

IMPORTANT: Never emit native tool-call markup such as
<|tool_calls_section_begin|>, <|tool_call_begin|>, <|tool_call_argument_begin|>
or any other <|...|> tokens. Tool calls are ONLY valid in the textual
Thought/Action/Action Input format shown above.
""".strip()

        workflow_prompt = workflow_prompt + "\n\n" + DISPATCH_PROMPT_SECTION

        if tool_mode == "knowledge":
            # Knowledge-chat mode (no pre-selected skill): only kb
            # tools + todowrite + terminate.  kb_tool_list already
            # contains kb_semantic_search, kb_ls, kb_glob, kb_grep,
            # kb_cat and optionally codegraph tools.
            tool_pack = ToolPack(
                kb_tool_list
                + [todowrite_tool, question_tool, Terminate()]
                + business_tools
                + connector_tool_extras
            )
        else:
            tool_pack = ToolPack(
                [
                    load_skill_tool,
                    load_tools_tool,
                ]
                + kb_tool_list
                + [
                    execute_skill_script,
                    get_skill_resource,
                    execute_skill_script_file_tool,
                    code_interpreter_tool,
                    load_file_tool,
                    execute_analysis_tool,
                    shell_interpreter_tool,
                    html_interpreter_tool,
                    sql_query_tool,
                    database_profile_tool,
                    read_file_tool,
                    todowrite_tool,
                    execute_tool_tool,
                    dispatch_parallel_tasks,
                    question_tool,
                    Terminate(),
                ]
                + business_tools
                + connector_tool_extras
            )

    # Debug: print all registered tools
    if tool_mode == "full":
        tool_pack.append(clarify_term_tool._tool)
        install_execution_guards(tool_pack, react_state)
        workflow_prompt += (
            "\n17. **clarify_term**：已注册的可调用工具。"
            "参数term是名词，reason是疑点，aspect默认meaning；计算口径使用formula。"
            "该工具提供当前模型解释、等待用户确认并保存共享词库。"
            "\n## 名词澄清\n系统在执行前检查名词。执行中发现新的多义词或口径疑点时，"
            "调用 clarify_term(term, reason)，等待用户确认；取消或超时停止任务。"
            "确认含义不代表数据库存在对应字段：继续检查口径和字段，缺数据要明确说明。"
            "常见明确指标直接使用常规或用户指定口径，并在结果说明；公式未经人工确认不构成补问理由。平均销售单价默认销售金额除以销售数量，不是各行单价算术平均。只有实际存在且上下文无法解决的口径冲突才补问，并提供evidence。"
            "采用用户指定、业务资料明确或前置筛选明确的定义；共享解释可由上下文确定，不因词库命中就询问。未知内部名词、无法确定的真实冲突仍需用户确认；缺数据明确说明，不让用户解释已知名词。"
            "已经通过确认卡片确认的解释，不要在最终回答中再次要求确认。"
            "不要用question重复询问已确认的名词含义；计算口径用clarify_term(aspect='formula')单独确认。"
            "名词解释使用当前模型已有知识，不调用联网搜索；"
            "不能用 shell、其他工具或网络请求绕过确认。"
        )
    logger.info(f"ToolPack resources: {list(tool_pack._resources.keys())}")
    if "execute_skill_script" not in tool_pack._resources:
        logger.error("execute_skill_script NOT in ToolPack!")

    # Combine tool_pack and knowledge_resources into a single ResourcePack
    all_resources = [tool_pack]
    if knowledge_resources:
        all_resources.extend(knowledge_resources)

    # --- Connector system prompt injection (T11) ---
    try:
        from dbgpt.agent.resource.connector.manager import (
            ConnectorManager as _ConnectorManager,
        )

        _cm = CFG.SYSTEM_APP.get_component(
            "connector_manager", _ConnectorManager, default_component=None
        )
        if _cm is not None and connector_ids:
            _active = _cm.list_active()
            # Only describe connectors the user explicitly selected.
            # Iterate connector_ids (not _active) so prompt order matches
            # user selection order and stays consistent with _select_connector_tools.
            _active_map = {c["connector_id"]: c for c in _active if isinstance(c, dict)}
            _selected = [
                _active_map[cid] for cid in connector_ids if cid in _active_map
            ]
            if _selected:
                _connector_lines = []
                for _c in _selected:
                    _tool_lines = []
                    for t in _c.get("tools", []):
                        _name = t.get("name", "unknown")
                        _desc = t.get("description", "") or "(no description)"
                        _args_schema = t.get("args", {}) or {}
                        # Render args schema as concise Parameters description
                        if _args_schema:
                            _param_parts = []
                            for _arg_name, _arg_meta in _args_schema.items():
                                if isinstance(_arg_meta, dict):
                                    _arg_type = _arg_meta.get("type", "any")
                                    _req = (
                                        "required"
                                        if _arg_meta.get("required")
                                        else "optional"
                                    )
                                    _arg_desc = _arg_meta.get("description", "")
                                    if _arg_desc:
                                        _trimmed_desc = (
                                            _arg_desc[:120] + "..."
                                            if len(_arg_desc) > 120
                                            else _arg_desc
                                        )
                                        _param_parts.append(
                                            f'"{_arg_name}": <{_arg_type}, {_req}, '
                                            f"{_trimmed_desc}>"
                                        )
                                    else:
                                        _param_parts.append(
                                            f'"{_arg_name}": <{_arg_type}, {_req}>'
                                        )
                                else:
                                    _param_parts.append(f'"{_arg_name}": <any>')
                            _params_str = "{" + ", ".join(_param_parts) + "}"
                        else:
                            _params_str = "{}"
                        _tool_lines.append(
                            f"  - **{_name}**: {_desc}\n    Parameters: {_params_str}"
                        )
                    _connector_lines.append(
                        f"### {_c.get('name', 'unknown')} "
                        f"({_c.get('connector_type', 'unknown')})\n"
                        f"{_c.get('description', '') or '(no description)'}\n\n"
                        f"Tools (call directly with `Action: <tool_name>`):\n"
                        + "\n".join(_tool_lines)
                        + "\n\nNote: Write operations require user confirmation."
                    )
                _connector_prompt = (
                    "\n\n## Available MCP Connector Tools\n"
                    "You have access to the following external MCP connectors. "
                    "All listed tools are pre-registered — invoke them directly "
                    "with `Action: <tool_name>`, NOT through `execute_tool`.\n"
                    + "\n\n".join(_connector_lines)
                )
                workflow_prompt += _connector_prompt
    except Exception:
        pass  # graceful degradation
    # --- End connector system prompt injection ---

    # Convert workflow_prompt to PromptTemplate so it is used as system prompt
    # Use jinja2 format to avoid issues with JSON braces { } in the prompt
    workflow_prompt_template = PromptTemplate(
        template=workflow_prompt
        + """

## Delivery and execution
There is no fixed step count or total-duration deadline for this main task.
Keep making progress until all requested deliverables exist. Individual tools
and model requests have deadlines; fix recoverable errors and continue.
For a database profile use database_profile first, then sql_query for aggregates.
sql_query previews at most 50 rows and saves ALL returned rows as JSON.
For an explicitly defined ratio, return the ratio plus its same-named numerator,
denominator and missing_input_count (an actual NULL check of required source inputs).
This is also required for Python calculations: print a final single-line JSON object
with columns and rows containing these same fields. rows must be arrays in columns
order, not dictionaries. source_result_ids are string result IDs returned by tools,
not sales row numbers. For database cost calculations, first query unjoined sales
details with source_sales_row_id for the selected customer/months. Cite that result
in source_result_ids, and cite a validated joined detail result in joined_result_id;
joined_sales_row_ids contains the actual joined sales row numbers, including missing
cost rows. QUERY_RESULTS lists the available result IDs, file paths, columns and tables.
Only the original full-population
ratio can satisfy the request; a partial/subset ratio does not complete it.
If inputs are missing, keep the full ratio NULL and missing_input_count positive.
For ERP销售 joins to 材料单价/工费单价, return e.rowid AS source_sales_row_id
in detail queries and ensure each source sales row occurs once. Aggregate joins
must return source_sales_row_count and joined_sales_row_count for each group;
use actual COUNT(DISTINCT e.rowid) and COUNT(*) to detect fanout. Deduplicate
only identical cost mappings by their actual join keys and prices, never sales rows.
Use SUM(row-level numerator)/SUM(denominator) for the same population. Do not let
separate SUM operations skip different NULL rows. Check all required cost inputs
before calculation; never treat missing costs as zero without user confirmation.
If required inputs are missing, return a NULL complete ratio, show the missing count
and available results, and state that complete delivery is unavailable. Python
reads QUERY_RESULT_PATH as {'columns': [...], 'rows': [...]}; uploaded files
remain in FILE_PATH. DATABASE_PROFILE_PATH contains the complete profile.
Each Python call starts a fresh process. _run.py contains the current Python
script; reading it does not discover database connections or data.
For financial reports, call html_interpreter with result_ids containing the validated
summary result and the real missing-input detail result, plus title. The renderer
builds the full HTML document from those results. Do not generate, write or read an
HTML file, and do not pass html, file_path, template_path or data for financial reports.
For non-financial reports, generate a full HTML document via html_interpreter.
Include table names, field names/types and actual row counts when a database profile
is requested.
If delivery validation fails, correct the stated omissions before terminate.
Follow the user's selected scope. Do not load a skill when the user declines
skills or when the available database facts already suffice for the request.
""",
        input_variables=[],
        template_format="jinja2",
    )

    agent_builder = (
        TaskReActAgent(task_state=react_state, max_retry_count=None, max_timeout=None)
        .bind(context)
        .bind(agent_memory)
        .bind(llm_config)
        .bind(tool_pack)
        .bind(workflow_prompt_template)
    )

    agent = await agent_builder.build()

    parser = ReActOutputParser()
    received = AgentMessage(content=user_input)
    # stream_queue and stream_callback were created earlier (before ToolPack)
    # so that the question tool can use them.

    # Wire up context-management status events into the SSE stream.
    async def _context_status_callback(status: Dict[str, Any]) -> None:
        await stream_queue.put({"type": "context.status", **status})

    agent.init_context_management(
        config=await _load_context_budget_config(
            llm_client=llm_client,
            model_name=dialogue.model_name,
        ),
        model_name=dialogue.model_name,
        on_status_event=_context_status_callback,
    )

    async def run_agent():
        try:
            if tool_mode == "full":
                await stream_callback("term.check.start", {})
                try:
                    business_context = ""
                    if knowledge_resources:
                        retrieve_terms = make_knowledge_retrieve(
                            react_state, knowledge_resources
                        )
                        business_context = await retrieve_terms(user_input)
                    ready = await term_clarifier.preflight(
                        user_input, database_context, business_context
                    )
                except Exception as error:
                    record_term_check_failure(react_state, error)
                    ready = False
                if not ready:
                    return AgentMessage(
                        content="必要名词未确认，任务未完成。",
                        success=False,
                        context={
                            "termination_reason": react_state.get(
                                "term_stop_reason", "term_unresolved"
                            )
                        },
                    )
                if react_state.get("confirmed_terms"):
                    react_state["term_explanation_only"] = react_state[
                        "term_explanation_only"
                    ] and any(
                        contains_term(user_input, name)
                        for term in react_state["confirmed_terms"]
                        for name in [term["term"], *term.get("aliases", [])]
                    )
                    scope_note = (
                        "本次仅需说明名词含义，不做实际数据计算；计算口径未确认不妨碍解释，"
                        "不要额外要求确认公式。"
                        if react_state["term_explanation_only"]
                        else "按用户确认或上下文明确的定义和口径继续，并说明口径；不要因公式未人工确认重复问。仅发现新的实际冲突且上下文无法消除时，使用clarify_term，aspect设为formula并提供evidence。"
                    )
                    received.content = (
                        "以下名词已通过前置筛选，包含用户确认项和上下文明确自动采用项；adopted_by=context表示自动采用，不要宣称用户确认了它。"
                        "用户选择或确认的项目也不能描述为自动采用；按实际确认记录说明来源，不推断确认方式。"
                        "adopted_by=user表示用户已选择或确认含义；来源以本次快照字段为准，不能让旧回答的自然语言描述覆盖确认记录。"
                        "不要因原文要求先选择而重复提问，按本次定义继续；出现新的实际冲突时再澄清。"
                        + scope_note
                        + "请继续原始需求的后续工作。\n原始需求：\n"
                        + user_input
                        + "\n\n本次采用的定义、口径及依据（包含自动采用项）：\n"
                        + json.dumps(react_state["confirmed_terms"], ensure_ascii=False)
                    )
                await stream_callback("term.check.done", {})
                requirements = react_state.get("task_requirements", {})
                received.content += (
                    "\n\n当前任务交付要求（继续任务时已继承，不能因取消计划步骤删除）：\n"
                    + json.dumps(requirements, ensure_ascii=False)
                )
                if requirements.get("ratios"):
                    from .tools.query_quality import ratio_delivery_hint

                    received.content += (
                        "\n本次是结构化财务计算。先完成SQL/Python结果校验，再生成报告。"
                        "不能只打印已格式化摘要；Python最终必须打印单行columns/rows JSON，"
                        "数值不提前round，完整指标缺输入时NULL并返回实际missing_input_count。"
                        "SQL与Python返回的实际result_id才可供html_interpreter引用。"
                        "财务报告只传result_ids和title，html为空；不要自行写HTML或模板中的数值。"
                        "缺失销售明细需source_sales_row_id、期间、产品编码及实际成本字段；"
                        "报告同时选择完整汇总结果和明细结果。"
                        "默认缺数据的说明属于交付，不要求补齐工费；仍需完成报告。\n"
                        + (ratio_delivery_hint(react_state) or "")
                    )
            if react_state.get("requires_database_profile"):
                await stream_callback("database.profile.start", {})
                profile_result = json.loads(await database_profile_tool())
                profile_text = "\n".join(
                    str(chunk["content"]) for chunk in profile_result.get("chunks", [])
                )
                workflow_prompt_template.template += (
                    (
                        "\n## 已获取的真实数据库画像\n"
                        + profile_text
                        + "\n画像事实已由系统获取。直接据此生成所需报告，"
                        "无需寻找连接、重复导出明细或加载无关技能。"
                    )
                    if profile_result.get("success")
                    else ("\n## 数据库画像获取失败\n" + profile_text)
                )
                await stream_callback("database.profile.result", profile_result)
            return await agent.generate_reply(
                received_message=received,
                sender=agent,
                stream_callback=stream_callback,
                historical_dialogues=historical_dialogues,
            )
        except asyncio.CancelledError:
            cancelled_result = TaskResult("cancelled", "user_cancelled")
            steps = list(history_steps)
            if current_history_step is not None:
                steps.append({**current_history_step, "status": "failed"})
            fail_running_subagent_history(subagent_history)
            payload = _build_react_history_payload(
                final_content="任务已取消，已完成的步骤和文件已保留。",
                steps=steps,
                task_plan=list(_todo_list),
                generated_images=react_state.get("generated_images", []),
                sub_agents=build_subagent_history_snapshot(subagent_history),
                input_files=input_files_snapshot,
                task_result=cancelled_result,
                task_requirements=react_state.get("task_requirements"),
                resource_context=react_state.get("resource_context"),
            )
            _react_terminal_events(
                storage_conv,
                payload,
                AgentFinalAnswer(content="任务已取消。", task_result=cancelled_result),
            )
            raise

    agent_task = asyncio.create_task(run_agent())
    if agent_task_holder is not None:
        agent_task_holder.append(agent_task)
    round_step_map: Dict[int, str] = {}
    pending_thoughts: Dict[
        int, List[str]
    ] = {}  # Buffer thinking content for delayed step creation
    pending_action_intentions: Dict[int, str] = {}
    pending_action_reasons: Dict[int, str] = {}
    # Emit a one-time "task preview" from the model's first "Plan: ..." line.
    task_plan_emitted = False
    # --- History persistence: collect step data during streaming ---
    history_steps: List[Dict[str, Any]] = []
    current_history_step: Optional[Dict[str, Any]] = None
    subagent_history: Dict[str, Dict[str, Any]] = {}
    final_answer_assembler = FinalAnswerAssembler()

    # Emit pre-loaded skill as an SSE step before agent starts processing
    if pre_matched_skill:
        skill_step_id, skill_step_event = build_step(
            f"Load Skill: {pre_matched_skill.metadata.name}",
            "Pre-loaded skill from user selection",
            phase="加载技能",
        )
        current_history_step = {
            "id": skill_step_id,
            "title": f"Load Skill: {pre_matched_skill.metadata.name}",
            "detail": "Pre-loaded skill from user selection",
            "phase": "加载技能",
            "thought": None,
            "action": None,
            "action_input": None,
            "outputs": [],
            "status": "done",
        }
        yield skill_step_event
        # Emit skill metadata as text chunk
        skill_desc = (
            f"Skill: {pre_matched_skill.metadata.name}"
            f" - {pre_matched_skill.metadata.description}"
        )
        yield step_chunk(skill_step_id, "text", skill_desc)
        current_history_step["outputs"].append(
            {"output_type": "text", "content": skill_desc}
        )
        # Emit skill instructions as markdown content (shows in right panel)
        if pre_matched_skill.instructions:
            yield step_chunk(skill_step_id, "markdown", pre_matched_skill.instructions)
            current_history_step["outputs"].append(
                {
                    "output_type": "markdown",
                    "content": pre_matched_skill.instructions,
                }
            )
        yield step_done(skill_step_id)
        history_steps.append(current_history_step)
        current_history_step = None

    profile_step_id: Optional[str] = None
    while True:
        if agent_task.done() and stream_queue.empty():
            break
        try:
            event = await asyncio.wait_for(stream_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue

        event_type = event.get("type")
        if event_type == "database.profile.start":
            profile_step_id, profile_start = build_step(
                "database_profile", "获取表结构、字段类型和实际行数"
            )
            current_history_step = {
                "id": profile_step_id,
                "title": "database_profile",
                "action": "database_profile",
                "status": "running",
                "outputs": [],
            }
            yield profile_start
        elif event_type == "database.profile.result" and profile_step_id:
            profile_status = "done" if event.get("success") else "failed"
            for chunk in event.get("chunks", []):
                yield step_chunk(
                    profile_step_id, chunk["output_type"], chunk["content"]
                )
            yield step_done(profile_step_id, profile_status)
            if current_history_step is not None:
                current_history_step["outputs"] = event.get("chunks", [])
                current_history_step["status"] = profile_status
                history_steps.append(current_history_step)
                current_history_step = None
        elif event_type == "context.status":
            # Forward context-management status to frontend as-is.
            yield _sse_event(event)
        elif event_type in (
            "question.asked",
            "question.replied",
            "question.rejected",
            "term.check.start",
            "term.check.done",
            "term.confirmed",
        ):
            # Forward human-in-the-loop question events to frontend as-is.
            yield _sse_event(event)
        elif event_type in (
            "agent.start",
            "agent.done",
            "agent.step",
            "subagent.artifacts",
        ):
            # Sub-agent progress uses a separate event channel so parallel
            # child rounds cannot collide with the lead agent's round map.
            update_subagent_history(subagent_history, event)
            yield _sse_event(event)
        elif event_type == "thinking":
            # Parse thinking content but don't create step yet
            # Step will be created when 'act' event arrives with confirmed
            # action
            round_num = int(event.get("round") or (len(round_step_map) + 1))
            llm_reply = event.get("llm_reply") or ""
            thought = None
            action_intention = None
            action_reason = None
            action = None
            action_input = None
            try:
                steps = parser.parse(llm_reply)
                if steps:
                    thought = steps[0].thought
                    action_intention = steps[0].action_intention
                    action_reason = steps[0].action_reason
                    action = steps[0].action
                    action_input = steps[0].action_input
            except Exception:
                pass

            # Store parsed thinking info in pending_thoughts for later use
            if round_num not in pending_thoughts:
                pending_thoughts[round_num] = []
            if thought:
                pending_thoughts[round_num].append(thought)
            intention_text = normalize_display_text(action_intention)
            if intention_text:
                pending_action_intentions[round_num] = intention_text
            reason_text = normalize_display_text(action_reason)
            if reason_text:
                pending_action_reasons[round_num] = reason_text
            # Don't emit anything yet - wait for 'act' event to create step

        elif event_type == "thinking_chunk":
            round_num = int(event.get("round") or (len(round_step_map) + 1))
            delta_thinking = event.get("delta_thinking") or ""
            delta_text = event.get("delta_text") or ""

            chunk = delta_thinking or delta_text
            if chunk:
                # Clean chunk: remove Action Input JSON to keep thought pure
                # Split on Action Input pattern and keep only thought part
                clean_chunk = re.split(
                    r"\n\s*Action\s*Input\s*:\s*\{", chunk, maxsplit=1
                )[0]
                # Also remove Action: lines
                clean_chunk = re.sub(r"\n\s*Action\s*:\s*\w+", "", clean_chunk)
                # Remove Thought: prefix if present
                if clean_chunk.startswith("Thought:"):
                    clean_chunk = clean_chunk[len("Thought:") :].strip()
                if clean_chunk:
                    if round_num not in pending_thoughts:
                        pending_thoughts[round_num] = []
                    pending_thoughts[round_num].append(clean_chunk)
                    if round_num not in round_step_map:
                        pending_step_id, pending_step_event = build_step(
                            "思考中",
                            "Thought/Action/Observation",
                        )
                        round_step_map[round_num] = pending_step_id
                        yield pending_step_event

        elif event_type == "act":
            # Create step ONLY when action is confirmed
            round_num = int(event.get("round") or (len(round_step_map) + 1))

            action_output = event.get("action_output") or {}
            thoughts = action_output.get("thoughts")
            action = action_output.get("action")
            action_input = action_output.get("action_input")
            action_input_data = None
            if action_input is not None:
                if isinstance(action_input, str):
                    try:
                        action_input_data = json.loads(action_input)
                    except Exception:
                        action_input_data = action_input
                else:
                    action_input_data = action_input

            # Skip step display for terminate action — its output will be
            # sent as a streaming "final" event instead of a step card.
            # Also skip emitting the thought for terminate since it's noise.
            # Note: TerminateAction.run() sets terminate=True but does NOT
            # set the action field, so we must check the terminate boolean.
            is_terminate = action_output.get("terminate") or (
                action and action.lower() == "terminate"
            )
            if is_terminate:
                pending_thoughts.pop(round_num, [])
                pending_action_intentions.pop(round_num, None)
                pending_action_reasons.pop(round_num, None)
                # derisk 参考：terminate 是「模拟 action」，不展示为真实工具 step。
                # 通知前端移除 terminate 这轮在 thinking_chunk 阶段提前创建的
                # 「思考中」占位 step（前端 step.meta 里 action=terminate 会过滤）。
                if round_num in round_step_map:
                    _stale_id = round_step_map.pop(round_num)
                    yield step_meta(_stale_id, None, "terminate", None, "terminate")
                # ── Auto-complete all remaining todos on terminate ──
                if _todo_list:
                    for t in _todo_list:
                        if t["status"] in ("pending", "in_progress"):
                            t["status"] = "completed"
                    yield _sse_event({"type": "plan.update", "tasks": list(_todo_list)})
                continue

            # ── TodoWrite: emit plan.update SSE and show step card ──
            if action and action.lower() == "todowrite":
                pending_thoughts.pop(round_num, [])
                pending_action_intentions.pop(round_num, None)
                pending_action_reasons.pop(round_num, None)
                # Extract todos from observation JSON
                obs_text = action_output.get("observations") or action_output.get(
                    "content"
                )
                todos_payload: List[Dict[str, str]] = []
                if obs_text:
                    try:
                        obs_json = (
                            json.loads(obs_text)
                            if isinstance(obs_text, str)
                            else obs_text
                        )
                        if isinstance(obs_json, dict):
                            todos_payload = obs_json.get("__todos__", [])
                    except Exception:
                        pass
                # Fallback: read from the closure variable
                if not todos_payload and _todo_list:
                    todos_payload = list(_todo_list)

                _td_total = len(todos_payload)
                _td_done = sum(
                    1 for t in todos_payload if t.get("status") == "completed"
                )
                if _td_done == 0:
                    todo_state = "init"
                elif _td_done == _td_total and _td_total > 0:
                    todo_state = "done"
                else:
                    todo_state = "progress"

                todo_meta = {
                    "state": todo_state,
                    "done": _td_done,
                    "total": _td_total,
                }
                _todo_step_title = (
                    f"TODO::{todo_state}:{_td_done}/{_td_total}"
                    if _td_total > 0
                    else f"TODO::{todo_state}"
                )

                # Emit or update the session-level task-plan card.
                # NOTE: Do NOT set phase — let it fall into the default
                # "Execution Steps" group so todowrite cards appear inline
                # alongside other action steps in chronological order.
                #
                # The lead agent calls todowrite repeatedly (create plan,
                # update progress, complete plan). All calls must update the
                # same card instead of creating TODO::init/progress/done cards.
                if _todo_step_holder:
                    todo_step_id = _todo_step_holder[0]
                else:
                    todo_step_id, _ = build_step(
                        _todo_step_title,
                        "todowrite",
                    )
                    _todo_step_holder.append(todo_step_id)
                round_step_map[round_num] = todo_step_id
                yield _sse_event(
                    {
                        "type": "step.start",
                        "step": step,
                        "id": todo_step_id,
                        "title": _todo_step_title,
                        "detail": "todowrite",
                        "todo_meta": todo_meta,
                    }
                )

                yield _sse_event({"type": "plan.update", "tasks": todos_payload})
                yield step_meta(
                    round_step_map[round_num],
                    None,
                    action,
                    None,
                    _todo_step_title,
                    todo_meta=todo_meta,
                )
                history_steps.append(
                    {
                        "id": round_step_map[round_num],
                        "title": _todo_step_title,
                        "detail": "todowrite",
                        "thought": None,
                        "action_intention": None,
                        "action_reason": None,
                        "action": action,
                        "action_input": None,
                        "outputs": [],
                        "status": "done",
                        "todo_meta": todo_meta,
                    }
                )
                yield step_done(round_step_map[round_num])
                continue

            # Collect buffered thoughts for history persistence
            # (already streamed to frontend via thinking_chunk handler)
            buffered_thoughts = pending_thoughts.pop(round_num, [])
            thought_text = None
            if buffered_thoughts:
                full_thought = "".join(buffered_thoughts)
                full_thought = re.split(r"\n\s*Action\s*:", full_thought, maxsplit=1)[
                    0
                ].strip()
                if full_thought.startswith("Thought:"):
                    full_thought = full_thought[len("Thought:") :].strip()
                if full_thought:
                    thought_text = full_thought
            action_intention = normalize_display_text(
                action_output.get("action_intention")
                or pending_action_intentions.pop(round_num, None)
                or action_output.get("phase")
            )
            # derisk-style task preview: the first action's notion/intention is a
            # one-time "接下来要做" overview shown to the user before execution.
            if not task_plan_emitted and action_intention:
                task_plan_emitted = True
                yield _sse_event(
                    {
                        "type": "task.preview",
                        "content": action_intention,
                        "round": round_num,
                    }
                )
            action_reason = normalize_display_text(
                action_output.get("action_reason")
                or pending_action_reasons.pop(round_num, None)
            )
            display_thought = (
                normalize_display_text(thoughts or thought_text) or action_intention
            )

            # 解析失败轮次（如 native tool_calls 退回 XML 导致 "No valid ReAct
            # step found"）：action 为空、无有效工具，跳过 step 展示（derisk 参考：
            # 无效 action 不展示为噪音 step），仅清理提前创建的「思考中」占位。
            if not action:
                pending_thoughts.pop(round_num, [])
                pending_action_intentions.pop(round_num, None)
                pending_action_reasons.pop(round_num, None)
                if round_num in round_step_map:
                    _stale_id = round_step_map.pop(round_num)
                    yield _sse_event(
                        {"type": "step.done", "id": _stale_id, "status": "done"}
                    )
                continue

            # Use the actual action name as the step title (Manus-style UI)
            action_title = action or f"ReAct Round {round_num}"
            if round_num in round_step_map:
                # Step already exists (from thinking) - update title with same id
                react_step_id = round_step_map[round_num]
                updated_event = _sse_event(
                    {
                        "type": "step.start",
                        "step": step,
                        "id": react_step_id,
                        "title": action_title,
                        "detail": "Thought/Action/Observation",
                    }
                )
                yield updated_event
            else:
                react_step_id, react_step_event = build_step(
                    action_title,
                    "Thought/Action/Observation",
                )
                round_step_map[round_num] = react_step_id
                yield react_step_event

            # --- History: create step record ---
            action_input_str = None
            if action_input is not None:
                action_input_str = (
                    action_input
                    if isinstance(action_input, str)
                    else json.dumps(action_input, ensure_ascii=False)
                )
            current_history_step = {
                "id": react_step_id,
                "title": action_title,
                "detail": "Thought/Action/Observation",
                "thought": display_thought,
                "action_intention": action_intention,
                "action_reason": action_reason,
                "action": action,
                "action_input": action_input_str,
                "outputs": [],
                "status": "running",
            }

            # Stream action code to frontend for right panel
            # (code_interpreter)
            code_payload = None
            if action == "code_interpreter" and isinstance(action_input_data, dict):
                code_payload = action_input_data.get("code")
            if isinstance(code_payload, str) and code_payload.strip():
                yield step_chunk(react_step_id, "code", code_payload)
                if current_history_step is not None:
                    current_history_step["outputs"].append(
                        {"output_type": "code", "content": code_payload}
                    )

            # Emit thinking metadata
            if thoughts or action or action_input:
                step_action_input = (
                    None if action == "code_interpreter" else action_input
                )
                yield step_meta(
                    react_step_id,
                    display_thought,
                    action,
                    step_action_input,
                    action_title,
                    action_intention=action_intention,
                    action_reason=action_reason,
                )

            # Emit observation (action execution result)
            observation_text = action_output.get("observations") or action_output.get(
                "content"
            )
            status = "done" if action_output.get("is_exe_success", True) else "failed"
            if observation_text:
                raw_chunks = emit_tool_chunks(react_step_id, observation_text)
                if raw_chunks:
                    for chunk in raw_chunks:
                        yield chunk
                else:
                    for chunk in chunk_text(str(observation_text), max_len=600):
                        yield step_chunk(react_step_id, "text", chunk)
                # --- History: collect the tool output for replay ---
                if current_history_step is not None:
                    parsed_obs = None
                    if isinstance(observation_text, str):
                        try:
                            parsed_obs = json.loads(observation_text)
                        except Exception:
                            pass
                    if isinstance(parsed_obs, dict) and isinstance(
                        parsed_obs.get("chunks"), list
                    ):
                        for item in parsed_obs["chunks"]:
                            if isinstance(item, dict):
                                content = item.get("content")
                                if isinstance(content, str):
                                    content = content.strip()
                                current_history_step["outputs"].append(
                                    {
                                        "output_type": item.get("output_type", "text"),
                                        "content": content,
                                    }
                                )
                    elif isinstance(observation_text, str) and observation_text:
                        current_history_step["outputs"].append(
                            {
                                "output_type": "text",
                                "content": observation_text,
                            }
                        )

                # Citations are opt-in: the assembler accepts only explicitly
                # supported knowledge tools and fails closed on malformed data.
                final_answer_assembler.observe(
                    action,
                    action_input_data,
                    observation_text,
                    succeeded=status == "done",
                )

            # Mark step as done and track as last completed
            yield step_done(react_step_id, status)
            if (
                status == "done"
                and action
                and action.lower() != "todowrite"
                and should_advance_todo(
                    action, thought_text or thoughts, observation_text
                )
            ):
                updated_todos = advance_todo_list()
                if updated_todos:
                    yield _sse_event({"type": "plan.update", "tasks": updated_todos})
            # --- History: finalize step ---
            if current_history_step is not None:
                current_history_step["status"] = status
                history_steps.append(current_history_step)
                current_history_step = None

    try:
        reply = await agent_task
    except Exception as e:
        err_msg = f"React agent failed: {e}"
        error_result = TaskResult("failed", "runtime_error")
        fail_running_subagent_history(subagent_history)
        error_payload = _build_react_history_payload(
            final_content=err_msg,
            steps=history_steps,
            task_plan=list(_todo_list),
            generated_images=react_state.get("generated_images", []),
            sub_agents=build_subagent_history_snapshot(subagent_history),
            input_files=input_files_snapshot,
            citations=[],
            task_result=error_result,
            task_requirements=react_state.get("task_requirements"),
            resource_context=react_state.get("resource_context"),
        )
        for terminal_event in _react_terminal_events(
            storage_conv,
            error_payload,
            AgentFinalAnswer(content=err_msg, task_result=error_result),
        ):
            yield terminal_event
        return

    task_result = task_result_for_reply(reply, react_state)
    if task_result.warnings:
        yield _sse_event({"type": "plan.update", "tasks": list(_todo_list)})
    if task_result.status == "succeeded" and reply.action_report:
        raw_content = reply.action_report.content or ""
        # The terminate ActionOutput.content may be the raw ReAct text, e.g.:
        # "Thought: ...\nAction: terminate\nAction Input: {"result": "..."}"
        # We need to extract the "result" value from Action Input.
        final_content = raw_content
        try:
            steps = parser.parse(raw_content)
            if steps:
                action_input = steps[0].action_input
                if action_input:
                    # action_input could be a string like '{"result": "..."}';
                    # tolerate trailing artifacts after the JSON object (the
                    # parser already strips known special tokens, this is the
                    # second line of defense for unknown ones).
                    if isinstance(action_input, str):
                        parsed_input = parse_or_raise_error(action_input)
                    else:
                        parsed_input = action_input
                    if isinstance(parsed_input, dict):
                        final_content = Terminate.final_content(parsed_input)
        except Exception:
            pass
        # native function calling 路径：terminate content 可能是 {"result":...}
        # (JSON) 或 {'result':...} (Python dict repr)，直接提取 result。
        if final_content == raw_content:
            try:
                _parsed = json.loads(raw_content)
                if isinstance(_parsed, dict):
                    final_content = Terminate.final_content(_parsed)
            except Exception:
                try:
                    import ast

                    _parsed = ast.literal_eval(raw_content)
                    if isinstance(_parsed, dict):
                        final_content = Terminate.final_content(_parsed)
                except Exception:
                    pass
    else:
        final_content = incomplete_summary(task_result, react_state)

    report = react_state.get("rendered_report")
    if task_result.status == "succeeded" and report and report.get("summary"):
        final_content = report["summary"]
    if task_result.status == "succeeded" and report:
        final_content += f"\n\n[查看报告]({report['url']})"

    terms = react_state.get("confirmed_terms", [])
    if terms:
        final_content += "\n\n本次采用的名词解释：\n" + "\n".join(
            f"- {t['term']}：{t['definition']}；{term_scope_description(t, markdown=True)}"
            + (
                "；来源：模型理解，未经资料核验"
                if t.get("source_type") == "model"
                else ""
            )
            + ("（已保存到共享术语库）" if t.get("saved") else "（仅本次使用）")
            + (f"；{t['save_error']}" if t.get("save_error") else "")
            for t in terms
        )

    final_answer = final_answer_assembler.finalize(final_content, task_result)

    # Persist AI reply with structured history payload
    history_payload = _build_react_history_payload(
        final_content=final_answer.content,
        steps=history_steps,
        task_plan=list(_todo_list),
        generated_images=react_state.get("generated_images", []),
        sub_agents=build_subagent_history_snapshot(subagent_history),
        input_files=input_files_snapshot,
        citations=[citation.to_dict() for citation in final_answer.citations],
        task_result=task_result,
        term_definitions=terms,
        task_requirements=react_state.get("task_requirements"),
        resource_context=react_state.get("resource_context"),
    )
    for terminal_event in _react_terminal_events(
        storage_conv,
        history_payload,
        final_answer,
    ):
        yield terminal_event


# ---------------------------------------------------------------------------
# Share link APIs
# ---------------------------------------------------------------------------


class ShareCreateRequest(_BaseModel):
    """Request body for creating a share link."""

    conv_uid: str


class ShareCreateResponse(_BaseModel):
    """Response body for share link creation."""

    token: str
    conv_uid: str
    share_url: str


class ShareConvResponse(_BaseModel):
    """Public payload returned when viewing a shared conversation."""

    conv_uid: str
    token: str
    messages: list  # list[{role, context, order}]


def _get_share_dao():
    """Lazily instantiate the ShareLinkDao (avoids import-time side-effects)."""
    from dbgpt_app.share.models import ShareLinkDao

    return ShareLinkDao()


def _get_conversation_service():
    """Return the ConversationServe Service component."""
    from dbgpt_serve.conversation.config import SERVE_SERVICE_COMPONENT_NAME
    from dbgpt_serve.conversation.service.service import Service

    return CFG.SYSTEM_APP.get_component(SERVE_SERVICE_COMPONENT_NAME, Service)


def _conversation_owner_user_name(conv_uid: str) -> Optional[str]:
    """Return the recorded owner (``user_name``) of a conversation.

    ``None`` means the conversation does not exist, so callers can fail
    closed. Legacy anonymous conversations return the stored blank value and
    keep their previous share behavior.
    """
    from dbgpt.storage.chat_history.chat_history_db import ChatHistoryEntity

    service = _get_conversation_service()
    with service.dao.session(commit=False) as session:
        entity = (
            session.query(ChatHistoryEntity)
            .filter(ChatHistoryEntity.conv_uid == conv_uid)
            .first()
        )
    return None if entity is None else (entity.user_name or "")


@router.post("/v1/chat/share", response_model=Result)
async def create_share_link(
    body: ShareCreateRequest = Body(),
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """Create (or return existing) share link for a conversation.

    The returned ``share_url`` is a relative path that the client should
    prepend with the current host to form an absolute URL.

    Ownership is verified first: a conversation recorded for another user
    cannot be shared by a foreign (or anonymous) caller, and a conversation
    that does not exist cannot be shared at all.
    """
    from fastapi import HTTPException

    requester = user_token.user_id if user_token else None
    owner = _conversation_owner_user_name(body.conv_uid)
    if owner is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if owner and owner != (requester or ""):
        raise HTTPException(status_code=403, detail="Not the conversation owner")
    dao = _get_share_dao()
    entity = dao.create_share(conv_uid=body.conv_uid, created_by=requester)
    if entity is None:
        return Result.failed(msg="Failed to create share link")
    return Result.succ(
        ShareCreateResponse(
            token=entity.token,
            conv_uid=entity.conv_uid,
            share_url=f"/share/{entity.token}",
        )
    )


@router.get("/v1/chat/share/{token}", response_model=Result)
async def get_share_conversation(token: str):
    """Public endpoint — no authentication required.

    Returns the full conversation history for the given share token so that the
    replay page can reconstruct and animate the session.
    """
    dao = _get_share_dao()
    link = dao.get_by_token(token)
    if link is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Share link not found")

    service = _get_conversation_service()
    from dbgpt_serve.conversation.api.schemas import ServeRequest

    history = service.get_history_messages(ServeRequest(conv_uid=link.conv_uid))

    # Public viewers get scrubbed react history: v2 payloads have their
    # ``input_files`` rewritten to non-resolvable public snapshots; v1
    # payloads and plain-text messages are passed through unchanged.
    messages = [
        {
            "role": m.role,
            "context": scrub_react_history_for_share(m.context),
            "order": m.order,
        }
        for m in (history or [])
    ]
    return Result.succ(
        ShareConvResponse(
            conv_uid=link.conv_uid,
            token=token,
            messages=messages,
        )
    )


@router.delete("/v1/chat/share/{token}", response_model=Result)
async def delete_share_link(
    token: str,
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """Revoke a share link.

    Only the recorded creator may delete a link; legacy anonymous shares
    (no recorded creator) remain revocable by anyone. Foreign users get a
    403 and unknown tokens a 404 — no share is silently dropped.
    """
    from fastapi import HTTPException

    dao = _get_share_dao()
    link = dao.get_by_token(token)
    if link is None:
        raise HTTPException(status_code=404, detail="Share link not found")
    requester = user_token.user_id if user_token else None
    if link.created_by and link.created_by != (requester or ""):
        raise HTTPException(status_code=403, detail="Not the share owner")
    deleted = dao.delete_by_token(token)
    if not deleted:
        raise HTTPException(status_code=404, detail="Share link not found")
    return Result.succ({"deleted": True, "token": token})


@router.get("/v1/agent/files/download")
async def download_agent_file(
    file_path: str = Query(..., description="Absolute path to the file to download"),
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """Download a file created by agent tools (shell_interpreter, code_interpreter).

    Only files under allowed directories (/tmp, PILOT_PATH/tmp/) can be downloaded.
    This prevents arbitrary file access on the server.
    """
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    from dbgpt.configs.model_config import PILOT_PATH

    # Agent tools write their output here, so relative paths resolve against it
    # rather than against the installation root.
    agent_tmp_dir = os.path.join(PILOT_PATH, "tmp")

    if not os.path.isabs(file_path):
        file_path = os.path.join(agent_tmp_dir, file_path)

    # Resolve to absolute path and prevent path traversal
    try:
        resolved = os.path.realpath(file_path)
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid file path")

    # Allowed base directories for agent-created files
    allowed_dirs = [
        os.path.realpath("/tmp"),
        os.path.realpath(agent_tmp_dir),
    ]

    if not any(resolved.startswith(d + os.sep) or resolved == d for d in allowed_dirs):
        raise HTTPException(
            status_code=403,
            detail="Access denied: file is not in an allowed directory",
        )

    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")

    filename = os.path.basename(resolved)
    return FileResponse(
        path=resolved,
        filename=filename,
        media_type="application/octet-stream",
    )


@router.get("/v1/agent/skills/download")
async def download_skill_package(
    skill_name: str = Query(..., description="Skill folder name"),
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """Download a skill folder as a .zip archive."""
    from fastapi import HTTPException

    if not skill_name:
        raise HTTPException(status_code=400, detail="skill_name is required")

    skills_dir = Path(DEFAULT_SKILLS_DIR).expanduser().resolve()
    skill_path = (skills_dir / skill_name).resolve()

    # Security: ensure path is under skills_dir
    try:
        skill_path.relative_to(skills_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if not skill_path.is_dir():
        raise HTTPException(status_code=404, detail="Skill not found")

    # Build zip in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(skill_path):
            for fname in files:
                abs_file = os.path.join(root, fname)
                arc_name = os.path.relpath(abs_file, skill_path)
                zf.write(abs_file, arcname=os.path.join(skill_name, arc_name))
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{skill_name}.zip"',
        },
    )


@router.post("/v1/chat/react-agent")
async def chat_react_agent(
    dialogue: ConversationVo = Body(),
    user_token: UserRequest = Depends(get_user_from_headers),
):
    logger.info(
        "chat_react_agent:%s,%s,%s",
        dialogue.chat_mode,
        dialogue.select_param,
        dialogue.model_name,
    )
    dialogue.user_name = user_token.user_id if user_token else dialogue.user_name
    # Pre-flight: 400/404 surface before the stream (and agent) is built.
    attachment_ctx = await _open_turn_attachments(dialogue, user_token)
    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked",
    }
    try:
        return _AgentStreamingResponse(
            _react_agent_stream(
                dialogue, tool_mode="full", attachment_ctx=attachment_ctx
            ),
            headers=headers,
            media_type="text/event-stream",
        )
    except Exception as e:
        # The streaming generator never started, so its own cleanup never
        # ran — drop the materialized turn files here before erroring out.
        _close_turn_attachments_quietly(attachment_ctx)
        logger.exception("React Agent Exception!%s", dialogue, exc_info=e)

        async def error_text(err_msg):
            yield f"data:{err_msg}\n\n"

        return StreamingResponse(
            error_text(str(e)),
            headers=headers,
            media_type="text/plain",
        )


@router.post("/v1/chat/knowledge-agent")
async def chat_knowledge_agent(
    dialogue: ConversationVo = Body(),
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """Knowledge-base chat agent — only kb tools + todowrite + terminate.

    Optimized for pure knowledge-chat scenarios (no skill/shell/sql/html tools).
    """
    logger.info(
        "chat_knowledge_agent:%s,%s,%s",
        dialogue.chat_mode,
        dialogue.select_param,
        dialogue.model_name,
    )
    dialogue.user_name = user_token.user_id if user_token else dialogue.user_name
    # Pre-flight: 400/404 surface before the stream (and agent) is built.
    attachment_ctx = await _open_turn_attachments(dialogue, user_token)
    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked",
    }
    try:
        return _AgentStreamingResponse(
            _react_agent_stream(
                dialogue, tool_mode="knowledge", attachment_ctx=attachment_ctx
            ),
            headers=headers,
            media_type="text/event-stream",
        )
    except Exception as e:
        # The streaming generator never started, so its own cleanup never
        # ran — drop the materialized turn files here before erroring out.
        _close_turn_attachments_quietly(attachment_ctx)
        logger.exception("Knowledge Agent Exception!%s", dialogue, exc_info=e)

        async def error_text(err_msg):
            yield f"data:{err_msg}\n\n"

        return StreamingResponse(
            error_text(str(e)),
            headers=headers,
            media_type="text/plain",
        )


# ── Human-in-the-Loop Question API ──────────────────────────────────────────


class _QuestionReplyBody(_BaseModel):
    answers: List[List[str]]
    conv_id: Optional[str] = None
    decisions: Dict[str, Any] = {}


@router.post("/v1/chat/question/{request_id}/reply", response_model=Result)
async def question_reply(
    request_id: str,
    body: _QuestionReplyBody,
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """User submits answers to a pending question, unblocking the agent tool."""
    from dbgpt_app.openapi.api_v1.tools.question_manager import question_manager

    try:
        question_manager.reply(request_id, body.answers, body.conv_id, body.decisions)
        return Result.succ({"success": True, "request_id": request_id})
    except KeyError as e:
        return Result.failed(msg=str(e))
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/v1/chat/question/{request_id}/reject", response_model=Result)
async def question_reject(
    request_id: str,
    conv_id: Optional[str] = Query(None),
    user_token: UserRequest = Depends(get_user_from_headers),
):
    """User dismisses a pending question, unblocking the agent tool with rejection."""
    from dbgpt_app.openapi.api_v1.tools.question_manager import question_manager

    try:
        question_manager.reject(request_id, conv_id)
        return Result.succ({"success": True, "request_id": request_id})
    except KeyError as e:
        return Result.failed(msg=str(e))
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
