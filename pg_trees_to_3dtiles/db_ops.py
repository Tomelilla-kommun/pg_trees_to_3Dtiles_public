import logging
from pathlib import Path
from typing import Tuple

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import connection

from .config import AppConfig

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    pass


def parse_table_name(qualified: str) -> Tuple[str, str]:
    parts = qualified.split(".")
    if len(parts) == 1:
        return "public", parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise DatabaseError(f"Invalid table reference: {qualified}")


def connect(cfg: AppConfig) -> connection:
    try:
        conn = psycopg2.connect(**cfg.db.to_kwargs())
        conn.autocommit = False
        return conn
    except Exception as exc:  # pragma: no cover - best-effort logging
        raise DatabaseError(f"Failed to connect to database: {exc}") from exc


def ensure_source_exists(conn: connection, source_table: str) -> None:
    schema, table = parse_table_name(source_table)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT to_regclass(%s)"),
            [f"{schema}.{table}"],
        )
        row = cur.fetchone()
        exists = (row is not None and row[0] is not None)
        if not exists:
            raise DatabaseError(f"Source table not found: {schema}.{table}")


def ensure_schema_and_extension(conn: connection, schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    conn.commit()


def detect_srid(conn: connection, cfg: AppConfig) -> int:
    """Determine SRID of source geometry column, or fall back to config."""

    src_schema, src_table = parse_table_name(cfg.source_table)
    geom_col = cfg.export.geometry_column
    # 1) Try Find_SRID
    with conn.cursor() as cur:
        cur.execute("SELECT Find_SRID(%s, %s, %s)", [src_schema, src_table, geom_col])
        row = cur.fetchone()
        if row and isinstance(row[0], int) and row[0] > 0:
            return int(row[0])
    # 2) Try ST_SRID from a non-null row
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT ST_SRID({geom}) FROM {schema}.{table} WHERE {geom} IS NOT NULL LIMIT 1").format(
                geom=sql.Identifier(geom_col),
                schema=sql.Identifier(src_schema),
                table=sql.Identifier(src_table),
            )
        )
        row = cur.fetchone()
        if row and row[0] and int(row[0]) > 0:
            return int(row[0])
    # 3) Fallback
    return int(cfg.fallback_epsg)


def prepare_target_table(conn: connection, cfg: AppConfig, drop_if_exists: bool, srid: int) -> None:
    target_schema, target_table = cfg.target_schema, cfg.target_table
    with conn.cursor() as cur:
        if drop_if_exists:
            cur.execute(
                sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                    sql.Identifier(target_schema), sql.Identifier(target_table)
                )
            )
        if cfg.export.use_gpu_instancing:
            # GPU instancing: keep rotation for compatibility; also add yaw/pitch/roll
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.{} (
                      id serial PRIMARY KEY,
                      geom geometry(POINTZ, {srid}),
                      scale double precision,
                      scale_non_uniform double precision[3],
                      rotation double precision,
                      yaw double precision DEFAULT 0,
                      pitch double precision DEFAULT 0,
                      roll double precision DEFAULT 0,
                      model varchar,
                      tags json
                    )
                    """
                ).format(
                    sql.Identifier(target_schema),
                    sql.Identifier(target_table),
                    srid=sql.SQL(str(int(srid))),
                )
            )
        else:
            # Non-GPU: rotation column (degrees); yaw/pitch/roll not required
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.{} (
                      id serial PRIMARY KEY,
                      geom geometry(POINTZ, {srid}),
                      scale double precision,
                      scale_non_uniform double precision[3],
                      rotation double precision,
                      model varchar,
                      tags json
                    )
                    """
                ).format(
                    sql.Identifier(target_schema),
                    sql.Identifier(target_table),
                    srid=sql.SQL(str(int(srid))),
                )
            )
    conn.commit()


def ensure_gpu_columns(conn: connection, cfg: AppConfig) -> None:
    """When GPU instancing is used, ensure yaw/pitch/roll/rotation exist."""

    tgt_schema, tgt_table = cfg.target_schema, cfg.target_table
    with conn.cursor() as cur:
        # Add columns if they do not exist
        cur.execute(
            sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS yaw double precision DEFAULT 0").format(
                sql.Identifier(tgt_schema), sql.Identifier(tgt_table)
            )
        )
        cur.execute(
            sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS pitch double precision DEFAULT 0").format(
                sql.Identifier(tgt_schema), sql.Identifier(tgt_table)
            )
        )
        cur.execute(
            sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS roll double precision DEFAULT 0").format(
                sql.Identifier(tgt_schema), sql.Identifier(tgt_table)
            )
        )
        cur.execute(
            sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS rotation double precision").format(
                sql.Identifier(tgt_schema), sql.Identifier(tgt_table)
            )
        )
    conn.commit()


def warn_unmapped_tree_models(conn: connection, cfg: AppConfig, limit: int = 5000) -> None:
    """Best-effort warning if source data contains tree model keys missing from mapping."""

    if not cfg.tree_models_mapping:
        return

    src_schema, src_table = parse_table_name(cfg.source_table)
    key_col = cfg.source_table_treemodels_column

    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT DISTINCT {key} FROM {schema}.{table} WHERE {key} IS NOT NULL LIMIT %s"
                ).format(
                    key=sql.Identifier(key_col),
                    schema=sql.Identifier(src_schema),
                    table=sql.Identifier(src_table),
                ),
                [int(limit)],
            )
            values = [r[0] for r in cur.fetchall()]
    except Exception as exc:
        logger.warning("Unable to scan source table for unmapped tree models: %s", exc)
        return

    missing = sorted({str(v) for v in values if v is not None and str(v) not in cfg.tree_models_mapping})
    if missing:
        sample = ", ".join(missing[:25])
        more = "" if len(missing) <= 25 else f" (+{len(missing) - 25} more)"
        logger.warning(
            "Tree model mapping missing %s keys from column '%s'; these will use fallback_model_*: %s%s",
            len(missing),
            key_col,
            sample,
            more,
        )


def insert_transformed_rows(conn: connection, cfg: AppConfig, srid: int) -> int:
    src_schema, src_table = parse_table_name(cfg.source_table)
    tgt_schema, tgt_table = cfg.target_schema, cfg.target_table
    geom_col = cfg.export.geometry_column
    tag_cols = cfg.tag_columns or ["tree_id"]
    height_col = sql.Identifier(cfg.source_table_height_column)

    tree_model_col = sql.Identifier(cfg.source_table_treemodels_column)

    # Build CASE expressions for per-tree model + model height (used for scale)
    # CASE WHEN tree_model_col = 'Gran_1' THEN 'Gran_1.glb' ... ELSE fallback END
    model_whens = []
    height_whens = []
    def _model_value_for_db(model_name: str) -> str:
        # i3dm.export.exe appears to prefix external-model URIs with `content/`.
        # So when use_external_model=true, store just the filename to avoid `content/content/...`.
        if cfg.export.use_external_model:
            return model_name
        return f"content/{model_name}"

    if cfg.tree_models_mapping:
        for key, spec in cfg.tree_models_mapping.items():
            model_whens.append(
                sql.SQL("WHEN {col} = {key} THEN {val}").format(
                    col=tree_model_col,
                    key=sql.Literal(key),
                    val=sql.Literal(_model_value_for_db(spec.model_name)),
                )
            )
            height_whens.append(
                sql.SQL("WHEN {col} = {key} THEN {val}").format(
                    col=tree_model_col,
                    key=sql.Literal(key),
                    val=sql.Literal(float(spec.model_height_m)),
                )
            )

    # Postgres does not accept an empty searched CASE (e.g. `CASE ELSE ... END`).
    # When we have no mapping entries, fall back to a plain literal expression.
    fallback_model_expr = sql.Literal(_model_value_for_db(cfg.fallback_model_name))
    fallback_model_height_expr = sql.Literal(float(cfg.fallback_model_height_m))

    if model_whens:
        model_expr = sql.SQL("CASE {whens} ELSE {fallback} END").format(
            whens=sql.SQL(" ").join(model_whens),
            fallback=fallback_model_expr,
        )
    else:
        model_expr = fallback_model_expr

    if height_whens:
        model_height_expr = sql.SQL("CASE {whens} ELSE {fallback} END").format(
            whens=sql.SQL(" ").join(height_whens),
            fallback=fallback_model_height_expr,
        )
    else:
        model_height_expr = fallback_model_height_expr

    # Build JSON array of a single object: json_build_array(json_build_object(...))
    # Cast all values to text for compatibility with GPU instancing (string-only attributes)
    tag_parts = []
    for col in tag_cols:
        tag_parts.append(f"'{col}'")
        tag_parts.append(f"{sql.Identifier(col).as_string(conn)}::text")
    tags_obj_expr_str = "json_build_object(" + ", ".join(tag_parts) + ")"
    tags_expr_str = f"json_build_array({tags_obj_expr_str})"

    with conn.cursor() as cur:
        if cfg.export.use_gpu_instancing:
            # Insert with yaw/pitch/roll (radians); rotation kept for compatibility (set to NULL)
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {t_schema}.{t_table} (geom, scale, scale_non_uniform, rotation, yaw, pitch, roll, model, tags)
                    SELECT
                        ST_SetSRID(ST_Force3D({s_geom}), %s) AS geom,
                        CASE
                            WHEN {h_col} IS NULL THEN NULL
                            ELSE ({h_col} / {model_h}) * %s
                        END AS scale,
                        NULL::double precision[3] AS scale_non_uniform,
                        NULL::double precision AS rotation,
                        (random() * 2 * pi()) AS yaw,
                        0.0 AS pitch,
                        0.0 AS roll,
                        {model_expr} AS model,
                        {tags_expr} AS tags
                    FROM {s_schema}.{s_table}
                    """
                ).format(
                    t_schema=sql.Identifier(tgt_schema),
                    t_table=sql.Identifier(tgt_table),
                    s_schema=sql.Identifier(src_schema),
                    s_table=sql.Identifier(src_table),
                    s_geom=sql.Identifier(geom_col),
                    h_col=height_col,
                    model_h=model_height_expr,
                    model_expr=model_expr,
                    tags_expr=sql.SQL(tags_expr_str),
                ),
                [int(srid), cfg.scale_multiplier],
            )
        else:
            # Insert with rotation (degrees); yaw/pitch/roll not used
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {t_schema}.{t_table} (geom, scale, scale_non_uniform, rotation, model, tags)
                    SELECT
                        ST_SetSRID(ST_Force3D({s_geom}), %s) AS geom,
                        CASE
                            WHEN {h_col} IS NULL THEN NULL
                            ELSE ({h_col} / {model_h}) * %s
                        END AS scale,
                        NULL::double precision[3] AS scale_non_uniform,
                        (random() * 360.0) AS rotation,
                        {model_expr} AS model,
                        {tags_expr} AS tags
                    FROM {s_schema}.{s_table}
                    """
                ).format(
                    t_schema=sql.Identifier(tgt_schema),
                    t_table=sql.Identifier(tgt_table),
                    s_schema=sql.Identifier(src_schema),
                    s_table=sql.Identifier(src_table),
                    s_geom=sql.Identifier(geom_col),
                    h_col=height_col,
                    model_h=model_height_expr,
                    model_expr=model_expr,
                    tags_expr=sql.SQL(tags_expr_str),
                ),
                [int(srid), cfg.scale_multiplier],
            )
        inserted = cur.rowcount
    conn.commit()
    return inserted


def ensure_spatial_index(conn: connection, cfg: AppConfig) -> None:
    tgt_schema, tgt_table = cfg.target_schema, cfg.target_table
    index_name = f"{tgt_table}_geom_idx"
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} USING GIST (geom)").format(
                sql.Identifier(index_name),
                sql.Identifier(tgt_schema),
                sql.Identifier(tgt_table),
            )
        )
    conn.commit()
