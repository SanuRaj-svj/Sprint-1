#!/usr/bin/env bash
# Run from the repo ROOT (the folder that contains OCR_reader/, supabase/, pii_shield..py).
# Only MOVES/RENAMES files. No file content is changed. Nothing is deleted.
# Usage: bash reorganise_repo.sh [path/to/db_runner.py]   (db_runner.py defaults to the same folder as this script)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="${1:-$HERE/db_runner.py}"

[ -f "pii_shield..py" ] || { echo "Run this from the repo root (pii_shield..py not found)."; exit 1; }
[ -f "$RUNNER" ]        || { echo "db_runner.py not found at $RUNNER"; exit 1; }

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then MV="git mv"; else MV="mv"; fi

mkdir -p squad2_pii/samples squad3_extractor

# ---- Squad 2 (PII masking) ----
$MV "pii_shield..py"      squad2_pii/pii_shield.py          # rename only: '..' made it un-importable
$MV README.md             squad2_pii/README.md              # this README documents PII Shield
$MV masked_output.json    squad2_pii/samples/masked_output.json
$MV masked_images_all.zip squad2_pii/samples/masked_images_all.zip

# ---- Squad 3 (extractor): parked untouched for later ----
$MV main.py extractor.py schemas.py pdf_reader.py extraction_output.json sample_docs tests squad3_extractor/

# ---- New files ----
cp "$RUNNER" squad2_pii/db_runner.py
printf 'Pillow\nsupabase\nhttpx==0.28.1\n' > squad2_pii/requirements.txt
cat > README.md <<'EOF'
# Sprint 1 pipeline

| Folder | Squad | Role |
|---|---|---|
| OCR_reader/ | 1 | OCR engine (Colab). Reads raw docs from Supabase, writes raw_ocr_json. |
| squad2_pii/ | 2 | PII masking. `python db_runner.py` reads raw_ocr_json, writes redacted_ocr_json. |
| squad3_extractor/ | 3 | Clinical fact extractor (not yet connected to the database). |
| supabase/ | - | Database migrations. |

Run order: Squad 1 notebook -> `cd squad2_pii && python db_runner.py`.
EOF
echo "Done. Layout:"; ls -1 . squad2_pii squad2_pii/samples squad3_extractor
