"""Workspace provisioning module for dbgpt-app pip package users.

Copies pilot template files to the user's workspace directory on first startup.
"""

import logging
import os
import shutil
import sys

logger = logging.getLogger(__name__)


def _template_dir_candidates():
    """Directories that may contain the packaged pilot template payload.

    The hatch build hook force-includes ``pilot/`` files into
    ``dbgpt_app/pilot_template``. Standard wheel installs place that payload
    next to the imported module, but editable installs keep it in the
    site-packages copy of the package, which is *not* where
    ``dbgpt_app.__file__`` points to. Both locations are therefore probed.
    """
    module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(module_root, "pilot_template")]
    for entry in sys.path:
        if entry:
            candidates.append(os.path.join(entry, "dbgpt_app", "pilot_template"))
    try:
        from importlib.metadata import distribution

        located = distribution("dbgpt-app").locate_file("dbgpt_app/pilot_template")
    except Exception:  # pragma: no cover - metadata may be unavailable
        located = None
    if located is not None:
        candidates.append(str(located))
    return candidates


def _resolve_template_dir():
    """Return the first existing pilot template directory, or None."""
    for candidate in _template_dir_candidates():
        if os.path.isdir(candidate):
            return candidate
    return None


def _ensure_pilot_workspace(dest_root: str) -> None:
    """Idempotently copy pilot workspace template files to dest_root.

    This function is safe to call multiple times — it will never overwrite
    existing files. On first run, it provisions the full pilot/ directory
    structure including alembic config and benchmark data.

    Seeding is best effort: a missing template payload or an unwritable
    destination (for example a read-only bind mount) is logged and skipped
    so that startup is never blocked by provisioning.

    Args:
        dest_root (str): The destination root directory (parent of meta_data/).
            Example: ~/.dbgpt/workspace/pilot/
    """
    template_dir = _resolve_template_dir()
    if template_dir is None:
        logger.debug("pilot_template payload not available, skipping seed.")
        return
    for src_dir, dirs, files in os.walk(template_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "versions")]
        for filename in files:
            if filename in (".DS_Store",) or filename.endswith((".pyc",)):
                continue
            src_file = os.path.join(src_dir, filename)
            rel_path = os.path.relpath(src_file, template_dir)
            dest_file = os.path.join(dest_root, rel_path)
            if os.path.exists(dest_file):
                logger.debug("Skipped (exists): %s", dest_file)
                continue
            try:
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                shutil.copy2(src_file, dest_file)
            except OSError as error:
                logger.warning(
                    "Pilot workspace provisioning skipped %s: %s", dest_file, error
                )
                continue
            logger.info("Provisioned: %s", dest_file)
