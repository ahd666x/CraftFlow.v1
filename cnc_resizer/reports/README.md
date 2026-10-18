# CNC diagnostic reports

`sample_diagnostics.json` is a reproducible, read-only baseline for the repository fixture `cnc_resizer/sample.cnc`. `t.16.1090x450.2set.baseline.json` is a compact baseline for the user-supplied target CNC file. It contains the raw-byte hash, modal/command inventory, line references, geometric bounds/depths, conservative feature candidates and the legacy parser baseline; the CNC source itself is intentionally not stored in the repository.

To regenerate the general stage-one diagnostics report:

```bash
python -m cnc_resizer.diagnostics /path/to/file.cnc --output /path/to/file.diagnostics.json
```

To regenerate the combined target report used for stages one/four:

```bash
python -m cnc_resizer.target_report /path/to/t.16.1090x450.2set.cnc --output cnc_resizer/reports/t.16.1090x450.2set.baseline.json
```

The target file does not declare G20/G21 units. Coordinates and its detected 14-unit raster pitch are therefore program units—not confirmed millimetres. It uses G54 and G43/H, has no explicit arc-plane selection, and includes controller-specific M141 plus compact tool-change tokens. These reports are geometric observations only, mark execution unsafe, and do not write executable output.
