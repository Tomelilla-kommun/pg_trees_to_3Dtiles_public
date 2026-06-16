# Data directory

This directory previously contained licensed data files.
The files have been removed from this repository because they cannot be redistributed.

## Previous structure
```text
data/
├── cross/
│   ├── fallback/
│   │   ├── Lov_asp_1_cr.glb
│   │   └── Lov_asp_1_cr.png
│   ├── Gran_1/
│   │   ├── Gran_1_cr.glb
│   │   └── Gran_1_cr.png
│   ├── Gran_2/
│   ...
│   ├── Lov_ronn_10/
│   └── Tall_3/
└── real3d/
    ├── fallback/
    │   ├── Lov_asp_1_cr.glb
    │   └── Lov_asp_1_cr.png
    ├── Gran_1/
    │   ├── Gran_1_cr.glb
    │   └── Gran_1_cr.png
    ├── Gran_2/
    ...
    ├── Lov_ronn_10/
    └── Tall_3/

```
## Notes

The application in its current form expects the original directory hierarchy and naming convention to be preserved.

- `.glb` files were 3D model assets.
- `.png` files were corresponding preview or texture/image assets.
- `cross/` contained cross-section or derived assets.
- `real3d/` contained the original/full 3D asset structure.
- `fallback/` contained fallback assets used when a specific asset was unavailable.

## Using your own data

To use the project with data, provide your own licensed replacements using the same directory and file naming structure. Alternatively change the configuration in the mapping.py files.

