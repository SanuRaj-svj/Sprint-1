# orchestrator - run the whole pipeline hands-free

Adds three things **around** the squads. No squad file is edited, copied or renamed.

| File | What it does |
|---|---|
| `intake.py` | Uploads documents to Supabase and creates the case + document rows, so Squad 1 finds them by itself. Replaces the one-off "copy files into the database" cell. |
| `human_review.py`, `review_ui.py` | The "pending human review" step from the defence document (Q1). Cases with uncertain OCR text stop here until a person confirms / fixes / rejects each line. |
| `pipeline.py` | Runs Squad 1 -> review gate -> Squad 2 -> Squad 3 in order, repeating until nothing more can move. |
| `Sprint1_Full_Pipeline.ipynb` | The Colab notebook that ties it together (GPU for Squad 1). |

## Stages (claims_cases.pipeline_stage)

```
INTAKE_IN_PROGRESS -> PENDING_OCR -> [Squad 1] -> PENDING_PII -> [gate] -> PENDING_HUMAN_REVIEW
                                         |                           \-> (nothing uncertain) -> [Squad 2] -> PENDING_FHIR -> [Squad 3] -> COMPLETED
                                         \-> RFI_RESCAN_REQUIRED   (hospital sends a clearer scan: intake --replace -> PENDING_OCR)
                                         \-> OCR_FAILED            (Squad 1 failed twice in a row: stops retrying)
PENDING_HUMAN_REVIEW --(all lines decided)--> PENDING_PII
```
Only `INTAKE_IN_PROGRESS`, `PENDING_HUMAN_REVIEW` and `OCR_FAILED` are new. `pipeline_stage` is a plain varchar, so **no database migration is needed**.

## What a reviewer's decision changes
Per uncertain block, in the Squad 1 JSON (`claim_documents.raw_ocr_json`), only for documents still waiting for Squad 2:
- **Correct as shown / I fixed the text** -> `downstream_value` = the verified text, `blocked_downstream` = false, `verified_status` = `HUMAN_VERIFIED`, `calibrated_confidence` = 1.0 (the original value is kept in `human_review.original_calibrated_confidence`).
- **Unreadable** -> stays blocked, recorded as `REVIEWED_UNREADABLE`.
- **Send scan back** -> document `RFI_BLURRY_SCAN`, case `RFI_RESCAN_REQUIRED`.
The audit (`claim_documents.audit_summary.human_review`) stores the reviewer name, time and *counts* only - no patient text.
Stray marks (fewer than 2 letters/digits) are not shown to reviewers.

## Feeding several patients at once
Put each patient in a folder (`Patient_01_Name/docs/*.pdf` or `Patient_01_Name/*.pdf`) and use `intake.intake_inbox(...)` (notebook Step 4b). Each folder becomes one case named `Patient_01`, `Patient_02`, ...; the patient's name is never stored. A `docs/` sub-folder is preferred so a sibling `preprocessed/` folder is not uploaded; non-document files (json, txt) are ignored and listed.

## Run without Colab (Squad 2/3 only, or if you have your own GPU box)
```bash
export SUPABASE_URL=... SUPABASE_SERVICE_KEY=... GEMINI_API_KEY=...
cd orchestrator
python intake.py PA-2026-0001 ./scans/                 # feed a case
python intake.py --inbox ./inbox                       # or: inbox/<case_number>/<files>
python intake.py PA-2026-0001 ./clear.png --replace    # answer an RFI
```
(`pipeline.load_squad1` needs `paddleocr` + a GPU, so Squad 1 is normally run from the notebook.)

## Rules to keep it safe
- **Run only one pipeline at a time.** Two at once could process the same case twice (double Gemini cost).
- **Don't run Squad 2 by hand** while a case is waiting for review; the hold only works because Squad 2 looks at `PENDING_PII` only.
- Review decisions are read-modify-write on one JSON: one reviewer per case at a time.
- Files must be PDF/PNG/JPG, under 50 MB, and two files in one case must not have the same name once punctuation is stripped (`a b.png` / `a_b.png`). Intake checks all of this before writing anything.
