# OCR Reader (Squad 1, Sprint 1)

The OCR Reader is the first stage of the claims pipeline. It pulls medical
documents (PDF, PNG, JPG) from Supabase, cleans up each page image, runs
PaddleOCR on a GPU, and decides for every line of text whether it can be trusted
automatically or needs a human. The result is stored back in Supabase as one
JSON per document and one master bundle per patient case, ready for the PII
Shield stage.

Everything lives in a single notebook: `OCR.ipynb` (built for Google Colab with
a T4 GPU).

---

## What it does

```
Supabase (raw-medical-docs)
        |
        v
 Load pages ---> Quality gate ---> Stamp removal + preprocessing ---> PaddleOCR
 (PDF @ 300 DPI)  (blur/contrast/     (red stamp, shadows, noise,        (English, GPU)
                   brightness)         contrast, deskew)
        |
        v
 Confidence calibration ---> 3-tier routing ---> Page-level RFI gate
        |
        v
 Document JSON (claim_documents.raw_ocr_json)
        |
        v
 Case master bundle (claims_cases.master_ocr_bundle_json) ---> next stage: PII Shield
```

### 1. Page loading
- Accepts `.pdf`, `.png`, `.jpg`, `.jpeg`. Anything else is skipped and reported.
- PDFs are rendered to PNG at 300 DPI, one page at a time, to keep memory low.

### 2. Image quality gate
Each original page is scored on sharpness (Laplacian variance over the strongest
edge pixels), contrast (standard deviation) and brightness (mean). A page passes
only if:

| Metric     | Requirement     |
|------------|-----------------|
| Sharpness  | >= 25           |
| Contrast   | >= 8            |
| Brightness | between 35 and 252 |

### 3. Stamp removal and preprocessing
1. Red or pink stamp ink is detected in HSV and painted over with the local
   background colour. If the mask covers more than 35% of the page it is treated
   as a colour cast and left alone.
2. Grayscale conversion.
3. Uneven lighting (phone shadows) is normalised by morphological division.
4. Non-local-means denoising.
5. CLAHE contrast enhancement.
6. Deskew within +/-15 degrees (corrections under 0.3 degrees are ignored).

No binary thresholding is applied, so PaddleOCR still sees natural grey-level
strokes.

### 4. OCR
PaddleOCR (`lang="en"`, `gpu:0`) with document orientation, unwarping and
text-line orientation turned **off**, so bounding boxes stay in the same pixel
space as the preprocessed page image that snippets are cropped from.

### 5. Confidence calibration and 3-tier routing
Each text block gets a calibrated confidence (a small heuristic boost for
clinical terms and valid day-first dates, a penalty for noisy or punctuation-heavy
text). Blocks containing clinical terms or dates count as **critical** and face
stricter thresholds:

| Block type | AUTO         | SUGGEST_CONFIRM | HUMAN_REVIEW |
|------------|--------------|-----------------|--------------|
| Critical   | >= 0.90      | 0.60 to 0.90    | < 0.60       |
| Other      | >= 0.80      | 0.50 to 0.80    | < 0.50       |

Only `AUTO` text flows downstream. `SUGGEST_CONFIRM` and `HUMAN_REVIEW` blocks are
blocked (`blocked_downstream: true`) and get a cropped snippet image for the
reviewer.

### 6. Page-level RFI gate (request for a rescan)
A page is held for rescan (`TRIGGER_PAGE_RFI_RESCAN`) if any of these is true:
- it fails the image quality gate (`IMAGE_QUALITY_FAIL`)
- no text was detected (`NO_TEXT_DETECTED`)
- more than 40% of its blocks are `HUMAN_REVIEW`

On a held page even the `AUTO` blocks are blocked (`HELD_PAGE_RESCAN`).

### 7. Document classification
A weighted regex lexicon tags each document as `DISCHARGE_SUMMARY`,
`LAB_REPORT`, `PRESCRIPTION` or `CLINICAL_NOTE` (the fallback when the best
score is below 3).

### 8. Smart caching
If a document already has a finished `raw_ocr_json` and the file size in storage
still equals `source_size_bytes`, the stored result is reused and GPU OCR is
skipped. Rows that are `PENDING_OCR`, `OCR_PROCESSING` or `FAILED` are never
treated as cached. Pass `force_reprocess=True` to ignore the cache.

---

## How to run

1. Open `OCR.ipynb` in Google Colab and select **Runtime > Change runtime type >
   T4 GPU**.
2. Add the secrets below in the Colab Secrets panel and switch on **Notebook
   access** for both.
3. Run the cells in order:

| Cell | Purpose |
|------|---------|
| 1 | Install PaddlePaddle GPU (CUDA 12.6) and PaddleOCR, verify the GPU |
| 2 | Smoke test PaddleOCR on a sample image |
| 3 | Install poppler, `pdf2image`, OpenCV |
| 4 | Install `supabase`, `httpx`, `pydantic` and print versions |
| 5 | Supabase connection and health check (secrets, client, test case, buckets) |
| 6 | Document loader and standalone preprocessing helpers |
| 7 | OCR core: quality gate, stamp removal, routing, `process_document_to_json()` |
| 8 | Batch runner: `run_supabase_batch_pipeline()` and `print_batch_summary()` |


### Dependencies

See `requirements.txt.txt`. Note that `paddlepaddle-gpu` must be installed from
the CUDA 12.6 index, not plain PyPI:

```bash
pip install paddlepaddle-gpu==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
pip install paddleocr pdf2image opencv-python-headless supabase httpx==0.28.1 pydantic==2.14.1
apt-get install -y poppler-utils
```

---

## Supabase setup

**Storage buckets** (keep both private):
- `raw-medical-docs`: input files
- `processed-previews`: preprocessed page images and review snippets

**Tables:**
- `claims_cases`: one row per patient case
- `claim_documents`: one row per uploaded file

## Input

- Cases with `claims_cases.pipeline_stage = 'PENDING_OCR'`, plus any case that
  has a `claim_documents` row still at `PENDING_OCR` (for example a re-uploaded
  file on a case waiting in `RFI_RESCAN_REQUIRED`).
- Each document is read from `raw-medical-docs` using its `raw_storage_path`.
  Rows with no path, or `PENDING_UPLOAD`, fail with a clear error.
- Two files with the same name stem in one case are skipped
  (`SKIPPED_DUPLICATE_ID`) because their IDs would collide. Rename one.

## Output

**Per document** (`claim_documents`):

| Column | Content |
|--------|---------|
| `raw_ocr_json` | Full document JSON (see below) |
| `source_size_bytes` | File size, used for caching |
| `processing_status` | `PENDING_PII`, or `RFI_BLURRY_SCAN` if any page needs a rescan, or `FAILED` |
| `preview_storage_paths` | Uploaded preprocessed page images |
| `avg_ocr_confidence`, `low_conf_line_count` | Quick quality stats |
| `is_confidence_acceptable`, `rfi_reason` | Rescan flag and human-readable reason |
| `ocr_completed_at` | UTC timestamp |

**Per case** (`claims_cases`):

| Column | Content |
|--------|---------|
| `master_ocr_bundle_json` | Combined bundle for the whole case |
| `case_audit_summary` | AUTO / SUGGEST_CONFIRM / HUMAN_REVIEW counts and documents needing rescan |
| `pipeline_stage` | `PENDING_PII`, or `RFI_RESCAN_REQUIRED` if any document failed the RFI gate. Left unchanged if a file errored or the case is empty, so the next run retries |

**Review images** are uploaded to `processed-previews`:
- `{case_number}/preprocessed/{doc_id}_p{N}_clean.png`
- `{case_number}/snippets/{doc_id}_p{N}_b{block_id}.png`

## Batch report

`run_supabase_batch_pipeline()` returns a manifest (run ID, timings, counts of
OCR'd / cached / failed / skipped files, per-case and per-document detail).
`print_batch_summary()` prints it as two tables: one row per document and one row
per case. One bad file never stops the rest of the case or batch; it is marked
`FAILED` and the run continues.

## Handing off to the next stage

The PII Shield should read **`safe_downstream_text`** and skip any block with
`blocked_downstream: true`. `full_raw_audit_text` and `combined_raw_audit_text`
hold unmasked patient data and are for audit only. Never pass them downstream.

## Known limits

- English only (`lang="en"`).
- Red-stamp removal is colour based, so red text can be affected on pages with
  heavy red content.
- Routing thresholds and the confidence calibration are heuristic, not trained.
  Re-tune them if real documents show too many or too few reviews.
- The document classifier is keyword based and falls back to `CLINICAL_NOTE`
  when the evidence is weak.
- Handwriting and very noisy scans need further testing.

## Notes

- Use synthetic data only.
- Postgres JSONB does not preserve key order, so read fields by key name.
- Temporary files are written under `/tmp/ocr_case_runner` and cleaned up after
  each document.