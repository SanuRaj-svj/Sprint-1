# Squad 1: Vision-Language OCR Reader (Sprint 1)

Reads medical documents (PDF/PNG) from Supabase, restores and OCRs them on a
Colab T4 GPU, routes every text block into AUTO / SUGGEST_CONFIRM / HUMAN_REVIEW,
and saves one JSON per document plus one master bundle per patient case.

## How to run
Open OCR.ipynb in Google Colab with a GPU runtime and run the cells in order:
install Paddle, init OCR, poppler, supabase install, Supabase connection,
loader/preprocess, OCR core, then the Cell 6 batch runner.

## Secrets (never commit these)
Add SUPABASE_URL and SUPABASE_SERVICE_KEY in Colab Secrets and turn on
Notebook access.

## Supabase
- Buckets: raw-medical-docs (input files), processed-previews (page images and
  review snippets). Keep both private.
- Tables: claims_cases (one row per patient case), claim_documents (one row per file).

## Input
Files in raw-medical-docs/{case_number}/{file_name}, cases at
claims_cases.pipeline_stage = 'PENDING_OCR'.

## Output
- Per document: claim_documents.raw_ocr_json. Unchanged files are served from
  cache using source_size_bytes.
- Per case: claims_cases.master_ocr_bundle_json and case_audit_summary, and
  pipeline_stage becomes PENDING_PII (or RFI_RESCAN_REQUIRED if any page fails
  the RFI gate).
- Example bundle: samples/Patient_02_Rajesh_Patel_master_bundle.json

## Notes
- Synthetic data only.
- Postgres JSONB does not keep key order; read fields by key name.