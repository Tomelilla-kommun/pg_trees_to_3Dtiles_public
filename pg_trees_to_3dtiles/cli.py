import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .db_ops import (
    DatabaseError,
    connect,
    ensure_schema_and_extension,
    ensure_source_exists,
    ensure_spatial_index,
    detect_srid,
    insert_transformed_rows,
    parse_table_name,
    prepare_target_table,
    warn_unmapped_tree_models,
)
from .exporter import ExportError, run_export

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("pg_trees_to_3dtiles")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clone PostGIS trees table and export to 3D tiles")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config JSON (default: config.json)",
    )
    parser.add_argument(
        "--skip-export",
        action="store_true",
        help="Only run the clone step, skip i3dm.export.exe",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        cfg = load_config(Path(args.config))
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 1

    # Normalize target reference if user provided a schema-qualified target_table
    if "." in cfg.target_table:
        try:
            schema, table = parse_table_name(cfg.target_table)
            cfg.target_schema = schema
            cfg.target_table = table
        except DatabaseError as exc:
            logger.error("Invalid target_table value: %s", exc)
            return 1

    if args.skip_export:
        cfg.run_export = False

    try:
        conn = connect(cfg)
    except DatabaseError as exc:
        logger.error(exc)
        return 1

    try:
        ensure_source_exists(conn, cfg.source_table)
        warn_unmapped_tree_models(conn, cfg)
        ensure_schema_and_extension(conn, cfg.target_schema)
        srid = detect_srid(conn, cfg)
        logger.info("Source data SRID detected as %s", srid)
        prepare_target_table(conn, cfg, drop_if_exists=cfg.recreate_target, srid=srid)
        # If we didn't drop the table and GPU instancing is on, enforce column shape
        if cfg.export.use_gpu_instancing and not cfg.recreate_target:
            from .db_ops import ensure_gpu_columns

            ensure_gpu_columns(conn, cfg)
        inserted = insert_transformed_rows(conn, cfg, srid=srid)
        ensure_spatial_index(conn, cfg)
        logger.info("Inserted %s rows into %s.%s", inserted, cfg.target_schema, cfg.target_table)
    except DatabaseError as exc:
        logger.error("Database error: %s", exc)
        conn.rollback()
        conn.close()
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Unexpected error: %s", exc)
        conn.rollback()
        conn.close()
        return 1

    conn.close()

    if not cfg.run_export:
        logger.info("Clone completed; export skipped by configuration/flag")
        return 0

    try:
        conn_string = cfg.db.to_conn_string()
        run_export(cfg, conn_string)
    except ExportError as exc:
        logger.error("Export failed: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Unexpected export error: %s", exc)
        return 1

    logger.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
