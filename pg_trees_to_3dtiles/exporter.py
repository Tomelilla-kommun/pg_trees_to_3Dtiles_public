import logging
import os
import subprocess
import sys
from pathlib import Path
import shutil
from typing import Iterable, List

from .config import AppConfig

logger = logging.getLogger(__name__)


class ExportError(Exception):
    pass


def _candidate_tool_paths() -> Iterable[Path]:
    candidates: List[Path] = []

    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(getattr(sys, "_MEIPASS")) / "tools" / "i3dm.export.exe")

    candidates.append(Path(sys.executable).resolve().parent / "tools" / "i3dm.export.exe")

    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidates.append(ancestor / "tools" / "i3dm.export.exe")

    seen = set()
    for c in candidates:
        if c not in seen:
            seen.add(c)
            yield c


def find_exporter() -> Path:
    for cand in _candidate_tool_paths():
        if cand.exists():
            return cand
    raise ExportError("i3dm.export.exe not found; expected under a tools/ folder near the script/exe")


def _copy_folder_contents_into(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.exists() or not src_dir.is_dir():
        logger.warning("Model assets folder not found or not a folder: %s", src_dir)
        return

    dst_dir.mkdir(parents=True, exist_ok=True)

    for item in src_dir.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(src_dir)
        target = dst_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            logger.warning("Overwriting existing content file: %s", target)

        shutil.copy2(item, target)


def stage_model_assets(cfg: AppConfig) -> None:
    """Copy model assets into `<output_dir>/content` so external-model export can reference them."""

    content_dir = cfg.export.output_dir / "content"
    logger.info("Staging model assets into: %s", content_dir)

    # Always stage fallback assets
    _copy_folder_contents_into(cfg.fallback_model_path, content_dir)

    # Stage all mapped model folders
    if not cfg.tree_models_mapping:
        return

    for key, spec in cfg.tree_models_mapping.items():
        if not spec.model_path.exists():
            logger.warning(
                "Model path for %s does not exist: %s (DB rows may fall back to fallback_model_*)",
                key,
                spec.model_path,
            )
            continue
        _copy_folder_contents_into(spec.model_path, content_dir)


def run_export(cfg: AppConfig, conn_string: str) -> None:
    exporter = find_exporter()
    output_dir = cfg.export.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Ensure model assets exist on disk for i3dm.export.exe.
    # Even when embedding models, the exporter must read the files.
    stage_model_assets(cfg)

    table_ref = f"{cfg.target_schema}.{cfg.target_table}"

    cmd = [
        str(exporter),
        "-c",
        conn_string,
        "-t",
        table_ref,
        "-o",
        str(output_dir),
        "-g",
        str(cfg.export.geometric_error),
        "--geometrycolumn",
        cfg.export.geometry_column,
        "--max_features_per_tile",
        str(cfg.export.max_features_per_tile),
    ]

    if cfg.export.use_scale_non_uniform:
        cmd.append("--use_scale_non_uniform")

    # Some versions of i3dm.export expect an explicit boolean value.
    if cfg.export.use_gpu_instancing:
        cmd.extend(["--use_gpu_instancing", "true"])

    # External model export expects an explicit boolean value.
    if cfg.export.use_external_model:
        cmd.extend(["--use_external_model", "true"])

    if cfg.export.extra_args:
        cmd.extend(cfg.export.extra_args)

    logger.info("Running i3dm.export.exe for table %s", table_ref)
    logger.info("Command: %s", " ".join(cmd))

    env = os.environ.copy()
    # Resolve relative model paths (e.g. content/<file>.glb) relative to the tiles output.
    process = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(output_dir))

    if process.stdout:
        logger.info(process.stdout.strip())
    if process.stderr:
        logger.warning(process.stderr.strip())

    if process.returncode != 0:
        raise ExportError(f"i3dm.export.exe failed with exit code {process.returncode}")
