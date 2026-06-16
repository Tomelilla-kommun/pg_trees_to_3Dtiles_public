import json
import logging
import runpy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class DBConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str = "prefer"

    def to_conn_string(self) -> str:
        parts = [
            f"Host={self.host}",
            f"Username={self.user}",
            f"Password={self.password}",
            f"Database={self.dbname}",
            f"Port={self.port}",
            f"sslmode={self.sslmode}",
        ]
        return ";".join(parts)

    def to_kwargs(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "sslmode": self.sslmode,
        }


@dataclass
class ExportConfig:
    output_dir: Path
    geometric_error: int = 1000
    geometry_column: str = "geom"
    max_features_per_tile: int = 1000
    use_scale_non_uniform: bool = False
    use_gpu_instancing: bool = False
    use_external_model: bool = False
    extra_args: Optional[list[str]] = None


@dataclass
class TreeModelSpec:
    model_name: str
    model_height_m: float
    model_path: Path


@dataclass
class AppConfig:
    db: DBConfig
    source_table: str
    source_table_height_column: str = "height"
    source_table_treemodels_column: str = "tree_texture"
    target_schema: str = "i3dm"
    target_table: str = "cloned_table"
    fallback_model_name: str = "tree_glb"
    fallback_model_height_m: float = 8.4
    fallback_model_path: Path = Path("data/fallback")
    tree_models_mapping_path: Optional[Path] = None
    tree_models_mapping: Dict[str, TreeModelSpec] = None  # type: ignore[assignment]
    scale_multiplier: float = 1.0
    fallback_epsg: int = 3006
    tag_columns: Optional[list[str]] = None
    recreate_target: bool = True
    run_export: bool = True
    export: ExportConfig = None  # type: ignore[assignment]


class ConfigError(Exception):
    pass


def _require(obj: Dict[str, Any], key: str) -> Any:
    if key not in obj:
        raise ConfigError(f"Missing required config key: {key}")
    return obj[key]


def _resolve_path(base_dir: Path, raw_path: str | Path) -> Path:
    p = Path(raw_path)
    if p.is_absolute():
        return p
    return (base_dir / p).resolve()


def _load_tree_models_mapping(mapping_path: Path) -> Dict[str, TreeModelSpec]:
    if not mapping_path.exists():
        logger.warning(
            "tree_models_mapping_path not found: %s (all rows will use fallback_model_*)",
            mapping_path,
        )
        return {}

    try:
        if mapping_path.suffix.lower() == ".json":
            raw = json.loads(mapping_path.read_text(encoding="utf-8"))
        elif mapping_path.suffix.lower() == ".py":
            ns = runpy.run_path(str(mapping_path))
            raw = ns.get("tree_models_mapping")
        else:
            raise ValueError("Expected a .py or .json mapping file")
    except Exception as exc:
        logger.warning(
            "Failed to load tree model mapping from %s: %s (all rows will use fallback_model_*)",
            mapping_path,
            exc,
        )
        return {}

    if not isinstance(raw, dict):
        logger.warning(
            "tree model mapping file %s did not produce a dict named tree_models_mapping (all rows will use fallback_model_*)",
            mapping_path,
        )
        return {}

    out: Dict[str, TreeModelSpec] = {}
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            logger.warning("Invalid mapping entry for key=%s in %s (expected dict)", key, mapping_path)
            continue
        try:
            model_name = str(spec["model_name"])
            model_height_m = float(spec["model_height_m"])
            model_path = _resolve_path(mapping_path.parent, spec["model_path"])
        except Exception as exc:
            logger.warning("Invalid mapping entry for key=%s in %s: %s", key, mapping_path, exc)
            continue

        # Validate assets: if the folder or the referenced model file is missing, fall back.
        if not model_path.exists() or not model_path.is_dir():
            logger.warning(
                "Model path for %s does not exist or is not a folder: %s (will use fallback_model_*)",
                key,
                model_path,
            )
            continue

        model_file = model_path / model_name
        if not model_file.exists():
            logger.warning(
                "Model file for %s not found at %s (will use fallback_model_*)",
                key,
                model_file,
            )
            continue

        out[str(key)] = TreeModelSpec(
            model_name=model_name,
            model_height_m=model_height_m,
            model_path=model_path,
        )

    return out


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    db_raw = _require(raw, "db")
    db = DBConfig(
        host=_require(db_raw, "host"),
        port=int(_require(db_raw, "port")),
        dbname=_require(db_raw, "dbname"),
        user=_require(db_raw, "user"),
        password=_require(db_raw, "password"),
        sslmode=db_raw.get("sslmode", "prefer"),
    )

    base_dir = path.parent.resolve()

    export_raw = raw.get("export", {})
    output_dir = Path(export_raw.get("output_dir", "output/tiles"))
    if not output_dir.is_absolute():
        output_dir = (base_dir / output_dir).resolve()
    extra_args = list(export_raw.get("extra_args", []))

    # New config option: export.use_external_model (preferred)
    use_external_model = bool(export_raw.get("use_external_model", False))

    # Backward compatibility: allow setting --use_external_model true in extra_args
    # If present, we enable the option and remove the flag from extra_args to avoid duplicates.
    i = 0
    while i < len(extra_args):
        if extra_args[i] == "--use_external_model":
            next_val = extra_args[i + 1] if i + 1 < len(extra_args) else "true"
            if str(next_val).strip().lower() in {"1", "true", "yes", "y", "on"}:
                use_external_model = True
            # Remove flag + value (if value exists)
            del extra_args[i : min(i + 2, len(extra_args))]
            continue
        i += 1

    export = ExportConfig(
        output_dir=output_dir,
        geometric_error=int(export_raw.get("geometric_error", 1000)),
        geometry_column=export_raw.get("geometry_column", "geom"),
        max_features_per_tile=int(export_raw.get("max_features_per_tile", 1000)),
        use_scale_non_uniform=bool(export_raw.get("use_scale_non_uniform", False)),
        use_gpu_instancing=bool(export_raw.get("use_gpu_instancing", False)),
        use_external_model=use_external_model,
        extra_args=extra_args,
    )

    # Backward compatibility: allow old keys model_name/model_height_m
    fallback_model_name = raw.get("fallback_model_name")
    if fallback_model_name is None:
        fallback_model_name = raw.get("model_name", "tree_glb")

    fallback_model_height_m = raw.get("fallback_model_height_m")
    if fallback_model_height_m is None:
        fallback_model_height_m = raw.get("model_height_m", 8.4)

    fallback_model_path_raw = raw.get("fallback_model_path", "data/fallback")
    fallback_model_path = _resolve_path(base_dir, fallback_model_path_raw)

    mapping_path_raw = raw.get("tree_models_mapping_path")
    mapping_path: Optional[Path] = None
    mapping: Dict[str, TreeModelSpec] = {}
    if mapping_path_raw:
        mapping_path = _resolve_path(base_dir, mapping_path_raw)
        mapping = _load_tree_models_mapping(mapping_path)

    return AppConfig(
        db=db,
        source_table=_require(raw, "source_table"),
        source_table_height_column=raw.get("source_table_height_column", "height"),
        source_table_treemodels_column=raw.get("source_table_treemodels_column", "tree_texture"),
        target_schema=raw.get("target_schema", "i3dm"),
        target_table=raw.get("target_table", "cloned_table"),
        fallback_model_name=str(fallback_model_name),
        fallback_model_height_m=float(fallback_model_height_m),
        fallback_model_path=fallback_model_path,
        tree_models_mapping_path=mapping_path,
        tree_models_mapping=mapping,
        scale_multiplier=float(raw.get("scale_multiplier", 1.0)),
        fallback_epsg=int(raw.get("fallback_epsg", 3006)),
        tag_columns=raw.get("tag_columns", ["tree_id"]),
        recreate_target=bool(raw.get("recreate_target", True)),
        run_export=bool(raw.get("run_export", True)),
        export=export,
    )
