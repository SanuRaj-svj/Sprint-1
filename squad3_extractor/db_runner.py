"""
Squad 3 <-> Supabase connector  (reads PII-masked text, writes extracted clinical facts).

This file does NOT change extractor.py, schemas.py, pdf_reader.py or main.py. It only:
  1. READS   claim_documents.redacted_ocr_json -> masked_text   (written by Squad 2)
             for cases at pipeline_stage = PENDING_FHIR and documents at PENDING_FHIR,
  2. CALLS   extractor.extract_clinical_facts()  exactly as Squad 3 wrote it,
  3. WRITES  claim_documents.extracted_clinical_facts   (the ClinicalExtraction JSON, same
                                                         shape as extraction_output.json)
             claim_documents.audit_summary["squad3_extraction"]   (counts + timestamp, merged)
             claim_documents.processing_status -> COMPLETED, fhir_completed_at
             claims_cases.master_fhir_bundle_json   (all documents' facts combined)
             claims_cases.admission_date / discharge_date          (only if currently empty)
             claims_cases.pipeline_stage -> COMPLETED   (only when EVERY document is extracted)

Safe to re-run: documents that already have facts are not sent to Gemini again (they are just
finalised), and a case that is not fully extracted stays at PENDING_FHIR so it can be retried.

Run (from the squad3_extractor/ folder):
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_SERVICE_KEY="<service_role key>"
    export GEMINI_API_KEY="<gemini key>"
    python db_runner.py                       # every case at pipeline_stage = PENDING_FHIR
    python db_runner.py PA-2026-TEST01        # only these case numbers
    python db_runner.py --dry-run             # do everything except write to Supabase
    python db_runner.py --force               # re-extract documents that already have facts
"""
import sys
from datetime import datetime, timezone

STAGE_IN, STAGE_OUT = "PENDING_FHIR", "COMPLETED"   # case: in from Squad 2, out when extracted
DOC_IN, DOC_OUT = "PENDING_FHIR", "COMPLETED"       # document status (doc_status enum)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    import os
    from supabase import create_client
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (service_role key) first.")
    return create_client(url, key)


# ------------------------------------------------------------------ READ
def fetch_cases(sb, case_numbers=None):
    rows = (sb.table("claims_cases")
            .select("case_id, case_number, pipeline_stage, admission_date, discharge_date")
            .eq("pipeline_stage", STAGE_IN).execute().data or [])
    if case_numbers:
        rows = [r for r in rows if r["case_number"] in case_numbers]
    return rows


def fetch_docs(sb, case_uuid):
    return (sb.table("claim_documents")
            .select("doc_id, file_name, processing_status, redacted_ocr_json, "
                    "extracted_clinical_facts, audit_summary")
            .eq("case_id", case_uuid).execute().data or [])


def masked_text_of(redacted):
    """Document-level masked_text written by Squad 2; fall back to joining the pages."""
    text = (redacted.get("masked_text") or "").strip()
    if text:
        return text
    pages = redacted.get("pages") or []
    return "\n\n".join(p["masked_text"] for p in pages if p.get("masked_text")).strip()


# ------------------------------------------------------------------ RUN SQUAD 3 CODE
def run_squad3(extract_fn, text):
    """Same conversion main.py uses: validated ClinicalExtraction -> JSON-compatible dict."""
    return extract_fn(text).model_dump(mode="json")


# ------------------------------------------------------------------ WRITE
def save_document(sb, row, facts, dry):
    """facts=None -> already extracted on an earlier run: only finalise the status."""
    upd = {}
    if facts is not None:
        audit = dict(row.get("audit_summary") or {})        # keep what other squads wrote
        audit["squad3_extraction"] = {
            "diagnoses": len(facts.get("diagnoses", [])),
            "procedures": len(facts.get("procedures", [])),
            "completed_at_utc": _now(),
        }
        upd.update({"extracted_clinical_facts": facts, "audit_summary": audit})
    if row["processing_status"] == DOC_IN:
        upd.update({"processing_status": DOC_OUT, "fhir_completed_at": _now()})
    if upd and not dry:
        sb.table("claim_documents").update(upd).eq("doc_id", row["doc_id"]).execute()


def case_dates(case, all_facts):
    """Earliest admission / latest discharge across the case's documents (ISO dates sort
    correctly as strings). Returns (value_for_bundle, columns_to_fill). Never overwrites a date
    already set on the case - a differing value is only reported."""
    adm = [f["admission_date"] for f in all_facts if f.get("admission_date")]
    dis = [f["discharge_date"] for f in all_facts if f.get("discharge_date")]
    found = {"admission_date": min(adm) if adm else None,
             "discharge_date": max(dis) if dis else None}
    fill = {}
    for col, val in found.items():
        if not val:
            continue
        current = case.get(col)
        if not current:
            fill[col] = val
        elif str(current) != val:
            print(f"  [warn] {col} already {current}; extractor found {val} - left unchanged")
    return found, fill


def build_bundle(case_number, found_dates, items):
    """items = [(row, facts)]. One case-level JSON: per-document facts plus a de-duplicated
    combined list of diagnoses / procedures with the documents that mention each."""
    def combine(key):
        merged = {}
        for row, facts in items:
            for f in facts.get(key, []):
                k = (f["name"].strip().lower(), f.get("event_date"))
                e = merged.setdefault(k, {**f, "source_documents": []})
                if row["file_name"] not in e["source_documents"]:
                    e["source_documents"].append(row["file_name"])
        return list(merged.values())
    return {"case_number": case_number, "generated_at_utc": _now(),
            "admission_date": found_dates["admission_date"],
            "discharge_date": found_dates["discharge_date"],
            "diagnoses": combine("diagnoses"), "procedures": combine("procedures"),
            "documents": [{"doc_id": r["doc_id"], "file_name": r["file_name"], "extraction": f}
                          for r, f in items]}


def save_case(sb, case, bundle, fill, dry):
    upd = {**fill, "master_fhir_bundle_json": bundle, "pipeline_stage": STAGE_OUT}
    if not dry:                    # one update, so the PENDING_FHIR purge trigger is not re-fired
        sb.table("claims_cases").update(upd).eq("case_id", case["case_id"]).execute()


# ------------------------------------------------------------------ MAIN
def run(sb, extract_fn, case_numbers=None, dry=False, force=False):
    cases = fetch_cases(sb, case_numbers)
    if not cases:
        print(f"No cases at {STAGE_IN} matching {case_numbers or 'any'}.")
        return
    for case in cases:
        cn, cu = case["case_number"], case["case_id"]
        print(f"\nCASE {cn}")
        items, all_ok = [], True                             # items = [(row, facts)]
        for row in fetch_docs(sb, cu):
            have = row.get("extracted_clinical_facts")
            if isinstance(have, dict) and not force:         # done earlier: finalise, no Gemini call
                save_document(sb, row, None, dry)
                items.append((row, have))
                print(f"  skip {row['file_name']} (already extracted)")
                continue
            red = row.get("redacted_ocr_json")
            if row["processing_status"] not in (DOC_IN, DOC_OUT) or not isinstance(red, dict):
                all_ok = False
                print(f"  skip {row['file_name']} (status {row['processing_status']}, "
                      f"no redacted text)")
                continue
            text = masked_text_of(red)
            if not text:
                all_ok = False
                print(f"  skip {row['file_name']} (masked text is empty)")
                continue
            try:                                             # one bad document must not stop the rest
                facts = run_squad3(extract_fn, text)
            except Exception as e:
                all_ok = False
                print(f"  FAIL {row['file_name']}: {e}", file=sys.stderr)
                continue
            save_document(sb, row, facts, dry)
            items.append((row, facts))
            print(f"  OK   {row['file_name']}: {len(facts['diagnoses'])} diagnoses, "
                  f"{len(facts['procedures'])} procedures")
        found, fill = case_dates(case, [f for _, f in items])
        if items and all_ok:
            save_case(sb, case, build_bundle(cn, found, items), fill, dry)
            print(f"  -> case {cn} advanced to {STAGE_OUT}, master_fhir_bundle_json written"
                  + (f", dates set {fill}" if fill else "") + ("  (dry run)" if dry else ""))
        else:
            print(f"  -> case {cn} left at {STAGE_IN} (some documents not extracted; re-run to retry)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # Imported here because extractor.py needs GEMINI_API_KEY the moment it is imported.
    from extractor import extract_clinical_facts            # Squad 3's file, untouched
    run(connect(), extract_clinical_facts, args or None,
        dry="--dry-run" in sys.argv, force="--force" in sys.argv)
