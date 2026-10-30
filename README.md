# Sprint 1 pipeline

| Folder | Squad | Role |
|---|---|---|
| OCR_reader/ | 1 | OCR engine (Colab). Reads raw docs from Supabase, writes raw_ocr_json. |
| squad2_pii/ | 2 | PII masking. `python db_runner.py` reads raw_ocr_json, writes redacted_ocr_json. |
| squad3_extractor/ | 3 | Clinical fact extractor (not yet connected to the database). |
| supabase/ | - | Database migrations. |

Run order: Squad 1 notebook -> `cd squad2_pii && python db_runner.py`.
