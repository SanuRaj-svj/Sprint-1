# Squad 3: Clinical fact extractor

Extracts diagnoses, procedures and admission/discharge dates from PII-masked document text using Gemini.

## Two ways to run

| Script | Purpose |
|---|---|
| `main.py` | Standalone: reads `sample_docs/discharge_summary.pdf`, writes `extraction_output.json`. Unchanged. |
| `db_runner.py` | Pipeline: reads from Supabase, runs the same `extract_clinical_facts()`, writes results back. |

## Database connector (`db_runner.py`)

```bash
pip install -r requirements.txt
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_KEY="<service_role key>"
export GEMINI_API_KEY="<gemini key>"

python db_runner.py                  # every case at pipeline_stage = PENDING_FHIR
python db_runner.py PA-2026-TEST01   # only these case numbers
python db_runner.py --dry-run        # extract, but write nothing
python db_runner.py --force          # re-extract documents that already have facts
```

**Reads** (written by Squad 2): cases at `claims_cases.pipeline_stage = 'PENDING_FHIR'`, and for each of their
documents at `processing_status = 'PENDING_FHIR'` the text in `claim_documents.redacted_ocr_json.masked_text`.
Only masked text is ever sent to Gemini.

**Writes**
- `claim_documents.extracted_clinical_facts`: the `ClinicalExtraction` JSON (same shape as `extraction_output.json`).
- `claim_documents.audit_summary.squad3_extraction`: diagnosis/procedure counts and timestamp (other keys kept).
- `claim_documents.processing_status` -> `COMPLETED` and `fhir_completed_at` set.
- `claims_cases.master_fhir_bundle_json`: case-level bundle (per-document facts + de-duplicated combined
  diagnoses/procedures with the source documents).
- `claims_cases.admission_date` / `discharge_date`: earliest admission and latest discharge across the case's
  documents, only where the column is currently empty (a differing existing value is reported, never overwritten).
- `claims_cases.pipeline_stage` -> `COMPLETED`, only when every document of the case has been extracted.

Documents that already have facts are not sent to Gemini again. If Gemini fails for a document, nothing is written
for it, the case stays at `PENDING_FHIR`, and a re-run retries only what is missing.
