# PII Shield (Sprint 1, Team Web) — text + image masking

Takes Team 1's Reader OCR JSON, masks personal data in **text and images**, and hands clean text (plus redacted page images) to the Extractor (Team 3).

## Run

```bash
# Text-only (same as before)
python pii_shield.py <folder or file> [<more folders>...] masked_output.json

# Text + masked images  (images written next to the JSON by default)
python pii_shield.py <folder> ... masked_output.json --images

# Text + masked images into a chosen directory
python pii_shield.py <folder> ... masked_output.json --images ./masked_images

# Testing only: also emit text for pages the Reader held
python pii_shield.py <folder> masked_testing.json --include-held --images

# Independent leak checks
python tools/verify_masking.py <TD folder> masked_output.json
python tools/verify_patients.py <Patient_xx root> masked_output.json
```

Requires **Pillow** for image masking (`pip install Pillow`). Without it, text masking still works; image step is skipped with a warning.

## Input

Reader JSON as delivered by Team 1 (`*_ocr.json`). Images are auto-discovered from:

- `pages[].preprocessed_image_path` (if the path exists on disk)
- sibling `preprocessed/` folder next to `docs/` (e.g. `Patient_01_.../preprocessed/P01_Lab_Report_p1_clean.png`)

Never used or copied: `full_raw_audit_text` (contains unmasked PII).

## Output

**JSON** (for Team 3):

```json
{
  "pii_shield_version": "1.2",
  "masked_images_dir": "/abs/path/to/masked_images",
  "documents": [{
    "doc_id": "P01_Discharge_Summary",
    "case_id": "Patient_01",
    "ready_for_extraction": true,
    "masked_text": "...Patient's Name\n[NAME]\n...",
    "total_redactions": 6,
    "pages": [{
      "page": 1,
      "status": "OK",
      "masked_text": "...",
      "masked_image_path": "P01_Discharge_Summary_p1_masked.png",
      "masked_image_dir": "/abs/path/to/masked_images",
      "redaction_log": [{"type": "NAME", "rule": "label_name_nextline", "block_ids": [6], ...}]
    }]
  }]
}
```

**Images**: one PNG per page with PII regions blacked out using the OCR block bounding boxes. File names are `{doc_id}_p{N}_masked.png`. Only the basename is stored in the JSON so patient-name folders never leak.

Team 3 should only process documents with `ready_for_extraction: true` and read `masked_text` (and optionally the masked images).

Placeholders: `[NAME] [ADDRESS] [PIN] [PHONE] [AADHAAR] [MRN] [DOB] [EMAIL] [ABHA_ID] [PAN] [NUMERIC_ID]`.

## Masked vs kept

- **Masked**: patient/relative name, patient address and PIN, patient mobile/phone, Aadhaar, ABHA, MRN/UHID, DOB, email, PAN.
- **Kept**: diagnosis, procedure, dates, lab values and ranges, ICD/package codes, age/sex, referring doctor name and registration numbers. Hospital/lab letterhead contacts (first lines above the first patient identifier) stay visible on both text and image.

## Safety behaviour

- Reader holds are respected. Held pages emit **no text** and (unless `--include-held`) no image.
- Blocks the Reader blocked (`blocked_downstream: true`) are skipped.
- The redaction log never holds the original value.
- Calibrated OCR confidence < 0.60 → strict mode + `needs_human_review`.

## Results on the supplied dataset (Patient_01–05, 20 documents)

- 20 documents released by the Reader, all masked: **60 redactions, 0 leaks**, clinical facts preserved.
- **20 masked images** produced under `masked_images/`.
- `case_id` is `Patient_01` etc.; folder names containing patient names are never copied into the JSON.

## Known limits

- Rule/label based: a name in running text with no label or title can be missed.
- Image redaction depends on OCR bounding boxes; if a block has no `bounding_box`, that span is only masked in text.
- Handwritten or very noisy scans need re-testing. Add Presidio/NER if real documents show misses.
