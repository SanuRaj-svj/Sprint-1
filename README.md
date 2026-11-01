# Sprint 1: Medical Claims Document Pipeline

A pipeline that turns scanned or digital medical documents (discharge summaries,
lab reports, prescriptions) into clean, privacy-safe, structured clinical data
for insurance claims. Each stage is built by a separate team and hands its output
to the next one through a shared Supabase database.


---

## Pipeline overview

```
 Team 1                      Team 2                      Team 3
 OCR Reader          --->    PII Shield           --->   Extractor
 (OCR_reader/)               (pii_shield..py)            (extractor.py, main.py)

 Reads PDFs/images,          Masks personal data in      Pulls diagnoses, procedures,
 restores and OCRs them,     text and page images        and admission/discharge
 routes each text block      using the OCR boxes         dates using Gemini
 AUTO / CONFIRM / REVIEW

         \______________ Supabase (storage + Postgres) ______________/
```

Documents move through `claims_cases.pipeline_stage`:

`PENDING_OCR` -> `PENDING_PII` -> `PENDING_FHIR` . 
A case goes to `RFI_RESCAN_REQUIRED` when a page is too poor to read and the hospital must upload a clearer scan.

| Stage | Folder / file | What it does | Detailed docs |
|-------|---------------|--------------|---------------|
| 1. OCR Reader | `OCR_reader/` | Preprocesses pages, runs PaddleOCR on a Colab T4 GPU, scores confidence, writes one JSON per document and one master bundle per case | `OCR_reader/README.md` |
| 2. PII Shield | `pii_shield..py` | Masks names, addresses, phone, Aadhaar, ABHA, MRN, DOB, email and PAN in text and images | [PII Shield](#2-pii-shield) below |
| 3. Extractor | `extractor.py`, `pdf_reader.py`, `schemas.py`, `main.py` | Extracts diagnoses, procedures and admission/discharge dates as validated JSON | [Extractor](#3-extractor) below |

---

## Shared infrastructure: Supabase

**Storage buckets** (keep both private): `raw-medical-docs` for uploaded files,
`processed-previews` for preprocessed page images and review snippets.

**Tables** (created by `supabase/migrations/`):
- `claims_cases`: one row per patient case. Holds `pipeline_stage`,
  `master_ocr_bundle_json`, `sanitized_master_bundle_json`, audit summaries and
  claim details.
- `claim_documents`: one row per file. Holds `processing_status`, `raw_ocr_json`,
  `redacted_ocr_json`, `extracted_clinical_facts` and timestamps for each stage.

**Privacy triggers.** Two database triggers delete raw OCR text once its masked
version exists, so unmasked patient data does not linger:
- `purge_raw_pii_on_redaction`: clears `raw_ocr_json` on a document when it moves
  to `PENDING_FHIR` with `redacted_ocr_json` filled in.
- `purge_master_case_pii`: clears `master_ocr_bundle_json` and every child
  document's `raw_ocr_json` when the case moves to `PENDING_FHIR` with a
  sanitized bundle.

**Local setup:**

```bash
npm install                 # installs the Supabase CLI
npx supabase start          # local stack (needs Docker)
npx supabase db reset       # applies supabase/migrations/
```


---

## 1. OCR Reader

Runs in Google Colab (T4 GPU) from `OCR_reader/OCR.ipynb`. It picks up every case
at `PENDING_OCR`, restores each page (stamp removal, shadow and noise cleanup,
contrast, deskew), runs PaddleOCR, and sorts each text block into `AUTO`,
`SUGGEST_CONFIRM` or `HUMAN_REVIEW`. Pages that are too poor to trust are held for
a rescan. Output goes back to Supabase, and the case moves to `PENDING_PII`.


---

## 2. PII Shield

Takes the Reader's OCR JSON, masks personal data in **text and images**, and
produces clean text (plus redacted page images) for the Extractor.

### Input

Reader JSON files (`*_ocr.json`). Page images are found from
`pages[].preprocessed_image_path`, or from a sibling `preprocessed/` folder.
`full_raw_audit_text` is never used or copied because it contains unmasked PII.

### Output

A JSON with one entry per document: `doc_id`, `case_id`, `ready_for_extraction`,
`masked_text`, `total_redactions`, and per-page `masked_text`,
`masked_image_path` and `redaction_log`. Masked images are PNGs named
`{doc_id}_p{N}_masked.png`, with PII regions blacked out using the OCR bounding
boxes. Only the file name is stored in the JSON, so patient-name folders never
leak.

Placeholders: `[NAME] [ADDRESS] [PIN] [PHONE] [AADHAAR] [MRN] [DOB] [EMAIL]
[ABHA_ID] [PAN] [NUMERIC_ID]`.

### Masked vs kept

- **Masked:** patient and relative names, patient address and PIN, mobile and
  phone numbers, Aadhaar, ABHA, MRN/UHID, DOB, email, PAN.
- **Kept:** diagnosis, procedure, dates, lab values and ranges, ICD and package
  codes, age and sex, referring doctor name and registration numbers. Hospital or
  lab letterhead contacts stay visible.

### Safety behaviour

- Pages the Reader held for rescan emit no text, and no image unless
  `--include-held` is used.
- Blocks the Reader blocked (`blocked_downstream: true`) are skipped.
- The redaction log never stores the original value.
- Calibrated OCR confidence below 0.60 switches the page to strict mode and flags
  it `needs_human_review`.

### Results on the sample dataset (Patient_01 to Patient_05)

20 documents, all released by the Reader and masked: **60 redactions, 0 leaks**,
clinical facts preserved, 20 masked images in `masked_images_all.zip`.

---

## 3. Extractor

Uses Google Gemini to pull structured clinical facts from a document.

**Files**
- `pdf_reader.py`: `extract_pdf_text()` reads selectable text page by page with
  PyMuPDF. It raises an error for scanned PDFs with no text layer (those need the
  OCR Reader).
- `extractor.py`: `extract_clinical_facts()` sends the text to Gemini
  (`gemini-3.5-flash`, temperature 0) with a strict instruction prompt and a JSON
  schema, validates the reply, and retries up to 3 times with backoff on 429/5xx
  errors.
- `schemas.py`: the Pydantic models `ClinicalFact` and `ClinicalExtraction`.
- `main.py`: reads `sample_docs/discharge_summary.pdf`, extracts, prints the JSON
  and saves `extraction_output.json`.

**Extraction rules (enforced in the prompt)**
- Only explicitly documented diagnoses and procedures, with the exact supporting
  text from the document.
- Dates only when documented. Missing or ambiguous dates are `null`.
- Suspected diagnoses are not treated as confirmed, and a test is not treated as a
  diagnosis unless the document says so.
- Instructions embedded inside the document are ignored.