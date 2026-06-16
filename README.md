# PG Trees to 3D Tiles

Command-line helper to:

1. Read a PostGIS source table with tree attributes.
2. Clone it into an `i3dm` schema table shaped for i3dm.export (adds scale, rotation, model, tags, POINTZ geom, and a spatial index).
3. Run `tools/i3dm.export.exe` to generate 3D Tiles.

The tool is config-driven (`config.json`)

The pipeline have only been tested on Windows 11. The easiest way to run it is to use the the latest release from the releases section.

## Project layout

- `pg_trees_to_3dtiles/` – CLI and helpers
- `config.example.json` – sample configuration
- `tools/i3dm.export.exe` – provided exporter (already present next to this repo)

## External Tools
This pipeline uses i3dm.export (MIT):

- i3dm.export documentation: https://github.com/Geodan/i3dm.export

Binaries/downloads:

- i3dm.export releases: https://github.com/Geodan/i3dm.export/releases/


## Tree library
The trees used in this repo comes mainly from from this package: https://www.fab.com/listings/429c8749-123d-4dd4-9be8-1f86c461ad03

> [!IMPORTANT]  
> The tree models used in this repo can not be provided due to license restrictions.
> To reproduce, get the corresponding models from the URL specified above.

The original files have been optimized/changed for the purpose of being optimal for rendering in 3D tiles using Cesium.js.

There are two versions of the trees:

 - Two crossed rectangles (4 triangles per tree) which is being used as default - tree_models_mapping_cross.py (default)
 - A real 3D model of the tree (500-1500 triangles per tree) - tree_models_mapping_real3d.py
 
The crossed trees will give better perfromance for large datasets while the real 3D model will give a nicer visual representation.

The tree models have been stored in the data/ directory. See more details in the [data directory documentation](data/README.md).

Under the Fab Standard License, you are allowed to:

  - Use the assets commercially or privately
  - Modify and adjust the assets in order to incorporate them into your Projects
  - Commercially distribute your Projects with the Fab assets incorporated into it
  - Use the assets with any compatible tools (usage is not limited to Unreal Engine)
  - Share the asset (directly, via a private repository or in the Project) with your collaborators that are working on the Project with you

You may not:
- Resell or redistribute the asset for free on a standalone basis or allow others to do the same

## Known problems
When use_gpu_instancing picking metadata for the trees will not work correctly because of a bug in cesium.js. See this ticket in the Cesium.js repo: https://github.com/CesiumGS/cesium/issues/11683

So when using use_gpu_instancing disable picking on the tileset.

When not using use_gpu_instancing picking metadata works but sometimes trees can dissapear in certain camera angles. This is known issue in the Cesium.js repo: https://github.com/CesiumGS/cesium/issues/11176

## Install

- Python 3.10+ on Windows
- PostgreSQL/PostGIS reachable from your machine
- `i3dm.export.exe` located in `tools/` (already present)

Install in development mode:

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
```

## Config

Copy the example and edit:

```bash
cp config.example.json config.json
```

Key fields:

- `db`: connection parameters; a connection string is composed internally
- `source_table`: `schema.table` of the original trees
- `source_table_treemodels_column`: column containing the model key per tree (e.g. `tree_texture`)
- `target_schema` / `target_table`: where the cloned data is written (default `i3dm.cloned_table`)
- `tree_models_mapping_path`: path to a `.py` or `.json` file containing `tree_models_mapping` (default is tree_models_mapping_cross.py)
- `fallback_model_name` / `fallback_model_height_m`: used when a row has no mapping entry
- `fallback_model_path`: folder copied into `<output_dir>/content` (fallback assets)
- `fallback_epsg`: SRID to use if source SRID cannot be detected
- `export`: options for `i3dm.export.exe` (output folder, optional extra args)
  - `use_gpu_instancing`: if true, exporter generates 3D Tiles 1.1 using GPU instancing. (default is true)
  - `use_external_model`: if true, exporter references model files under `<output_dir>/content` (default is false)
- `recreate_target`: if true, drops and recreates the target table
- `run_export`: if false, only the clone step is performed

See `config.example.json` for the full shape.

## Usage

Run the release, exe version pipeline (clone + export):
```
pg_trees_to_3dtiles.exe --config config.json
```

Run the dev pipeline (clone + export):

```bash
python -m pg_trees_to_3dtiles.cli --config config.json
```

Skip export (clone only):

```bash
python -m pg_trees_to_3dtiles.cli --config config.json --skip-export
```

## **Build EXE (PyInstaller)**

- **Entry script:** Use `run.py` (absolute import) instead of `pg_trees_to_3dtiles/cli.py`.
- **Spec file:** `pg_trees_to_3dtiles.spec` is generated; you can customize it and build with `pyinstaller pg_trees_to_3dtiles.spec`.

- **One-folder build** (recommended to keep `tools/i3dm.export.exe` alongside the app):

```powershell
pyinstaller -n pg_trees_to_3dtiles -D run.py
```

Place exporter next to the EXE:
- `dist\pg_trees_to_3dtiles\tools\i3dm.export.exe`


## What the tool does

1. Validates config and connectivity to PostGIS.
2. Checks the source table exists.
3. Ensures `i3dm` schema and PostGIS extension exist; (optionally) recreates the target table with required columns.
4. Inserts rows:
  - `geom`: forced to `POINTZ` with SRID detected from the source table; falls back to `fallback_epsg` if detection fails
  - `scale`: `height / model_height_m_for_that_tree` (falls back to `fallback_model_height_m`)
  - `rotation`: random 0–360 degrees per row (ignored when GPU instancing is used)
  - `yaw`/`pitch`/`roll`: when GPU instancing is enabled, `yaw` is randomized `0–2π` and `pitch`/`roll` are `0`
  - `model`: per-row model name from mapping; falls back to `fallback_model_name`
   - `tags`: JSON with `tree_id`
5. Builds a GIST index on `geom`.
6. Invokes `i3dm.export.exe -c <conn> -t <target_table> -o <output>` (plus any extra args).

Model resolution notes:

- `export.use_external_model: false`:
  - DB `model` values are written as `content/<file>.glb`.
  - The tool stages model assets into `<output_dir>/content` so the exporter can read and embed them.

- `export.use_external_model: true`:
  - DB `model` values are written as plain filenames (e.g. `tree_test.glb`).
  - `i3dm.export.exe` places them under `content/` in the generated URIs.
  - The tool stages model assets into `<output_dir>/content`.

## Notes

- Assumes `source_table` has columns listed in the request. SRID is detected from the source table; `fallback_epsg` is used when detection fails.
- Uses `psycopg2` for database access.
- Random rotation uses Postgres `random()`; each run re-randomizes rotation unless you keep the table as-is.
