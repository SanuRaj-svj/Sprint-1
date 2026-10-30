"""
Squad 2 <-> Supabase connector  (reads PENDING_PII, writes redacted output).

This file does NOT change pii_shield.py. It only:
  1. READS   claim_documents.raw_ocr_json  (written by Squad 1) + page images from the
             'processed-previews' bucket,
  2. CALLS   pii_shield.shield_document()  exactly as Squad 2 wrote it,
  3. WRITES  claim_documents.redacted_ocr_json / pii_redaction_audit / processing_status,
             masked PNGs to 'processed-previews/masked/...',
             claims_cases.sanitized_master_bundle_json / pii_audit_summary / pipeline_stage.

Run (from the squad2_pii/ folder):
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_SERVICE_KEY="<service_role key>"
    python db_runner.py                       # every case at pipeline_stage = PENDING_PII
    python db_runner.py PA-2026-TEST01        # only these case numbers
    python db_runner.py --dry-run             # do everything except write to Supabase
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pii_shield                              # Squad 2's file, untouched

PREVIEW_BUCKET = "processed-previews"
STAGE_IN, STAGE_OUT = "PENDING_PII", "PENDING_FHIR"
DOC_IN, DOC_OUT = "PENDING_PII", "PENDING_FHIR"


def _safe(name):                               # same rule Squad 1 uses for storage keys
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_") or "UNNAMED"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    from supabase import create_client
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (service_role key) first.")
    return create_client(url, key)


# ------------------------------------------------------------------ READ
def fetch_cases(sb, case_numbers=None):
    q = sb.table("claims_cases").select("case_id, case_number, pipeline_stage")
    rows = q.eq("pipeline_stage", STAGE_IN).execute().data or []
    if case_numbers:
        rows = [r for r in rows if r["case_number"] in case_numbers]
    return rows


def fetch_docs(sb, case_uuid):
    return (sb.table("claim_documents")
            .select("doc_id, file_name, processing_status, raw_ocr_json, redacted_ocr_json")
            .eq("case_id", case_uuid).execute().data or [])


def localise_images(sb, raw_doc, tmp):
    """Squad 1 stores storage KEYS in pages[].preprocessed_image_path. pii_shield reads LOCAL
    files, so download each page image and point the path at the local copy (in memory only)."""
    for pg in raw_doc.get("pages", []):
        key = pg.get("preprocessed_image_path")
        if not key or os.path.isfile(key):
            continue
        try:
            data = sb.storage.from_(PREVIEW_BUCKET).download(key)
            local = Path(tmp) / "in" / Path(key).name
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            pg["preprocessed_image_path"] = str(local)
        except Exception as e:                       # text masking still works without the image
            print(f"    [warn] page image not downloaded ({key}): {e}", file=sys.stderr)
    return raw_doc


# ------------------------------------------------------------------ RUN SQUAD 2 CODE
def run_squad2(raw_doc, case_number, img_dir):
    out = pii_shield.shield_document(raw_doc, include_held=False, image_out_dir=img_dir)
    out["case_id"] = case_number                     # DB case number instead of 'Patient_NN' folder hint
    return out


def upload_masked_images(sb, out, case_number, img_dir, dry):
    """Push masked PNGs to Storage and replace the local temp path by the storage key."""
    for pg in out["pages"]:
        pg.pop("masked_image_dir", None)             # absolute temp path: must not be stored
        name = pg.get("masked_image_path")
        if not name:
            continue
        key = f"masked/{_safe(case_number)}/{name}"
        if not dry:
            sb.storage.from_(PREVIEW_BUCKET).upload(
                path=key, file=(Path(img_dir) / name).read_bytes(),
                file_options={"content-type": "image/png", "upsert": "true"})
        pg["masked_image_path"] = key
    return out


def audit_of(out):
    types = {}
    for pg in out["pages"]:
        for e in pg["redaction_log"]:
            types[e["type"]] = types.get(e["type"], 0) + 1
    return {"pii_shield_version": out["pii_shield_version"],
            "total_redactions": out["total_redactions"],
            "redactions_by_type": types,
            "needs_human_review": any(p["needs_human_review"] for p in out["pages"]),
            "completed_at_utc": _now()}


# ------------------------------------------------------------------ WRITE
def save_document(sb, doc_id, out, dry):
    ready = out["ready_for_extraction"]
    upd = {"redacted_ocr_json": out, "pii_redaction_audit": audit_of(out)}
    if ready:                                        # this single update fires trg_purge_raw_pii:
        upd["processing_status"] = DOC_OUT           # raw_ocr_json -> NULL, is_raw_pii_purged = true
    if not dry:
        sb.table("claim_documents").update(upd).eq("doc_id", doc_id).execute()
    return ready


def save_case(sb, case_uuid, case_number, outs, dry):
    bundle = {"case_number": case_number, "pii_shield_version": pii_shield.VERSION,
              "generated_at_utc": _now(), "documents": outs,
              "combined_masked_text": "\n\n".join(
                  f"--- DOCUMENT: {o['doc_id']} ---\n{o['masked_text']}" for o in outs if o["masked_text"])}
    summary = {"documents": len(outs),
               "total_redactions": sum(o["total_redactions"] for o in outs),
               "documents_needing_review": [o["doc_id"] for o in outs
                                            if any(p["needs_human_review"] for p in o["pages"])]}
    if not dry:                                      # fires trg_purge_master_case_pii (wipes raw OCR)
        sb.table("claims_cases").update({
            "sanitized_master_bundle_json": bundle, "pii_audit_summary": summary,
            "pipeline_stage": STAGE_OUT}).eq("case_id", case_uuid).execute()


# ------------------------------------------------------------------ MAIN
def run(sb, case_numbers=None, dry=False):
    cases = fetch_cases(sb, case_numbers)
    if not cases:
        stages = sb.table("claims_cases").select("case_number, pipeline_stage").execute().data
        print(f"No cases at {STAGE_IN} matching {case_numbers or 'any'}. Cases in DB:")
        for r in stages:
            print(f"  {r['case_number']}: {r['pipeline_stage']}")
        return
    for case in fetch_cases(sb, case_numbers):
        cn, cu = case["case_number"], case["case_id"]
        print(f"\nCASE {cn}")
        outs, all_ok = [], True
        for row in fetch_docs(sb, cu):
            if row["processing_status"] != DOC_IN or not isinstance(row["raw_ocr_json"], dict):
                if row["processing_status"] in (DOC_OUT, "COMPLETED"):
                    if isinstance(row.get("redacted_ocr_json"), dict):   # done on an earlier run:
                        outs.append(row["redacted_ocr_json"])            # still belongs in the case bundle
                else:
                    all_ok = False                   # RFI / failed / not OCR'd yet -> case must wait
                    print(f"  skip {row['file_name']} (status {row['processing_status']})")
                continue
            with tempfile.TemporaryDirectory() as tmp:
                img_dir = str(Path(tmp) / "masked")
                os.makedirs(img_dir)
                raw = localise_images(sb, row["raw_ocr_json"], tmp)
                out = run_squad2(raw, cn, img_dir)
                out = upload_masked_images(sb, out, cn, img_dir, dry)
            ready = save_document(sb, row["doc_id"], out, dry)
            outs.append(out)
            all_ok &= ready
            print(f"  {'OK  ' if ready else 'HOLD'} {row['file_name']}: "
                  f"{out['total_redactions']} redactions")
        if outs and all_ok:
            save_case(sb, cu, cn, outs, dry)
            print(f"  -> case {cn} advanced to {STAGE_OUT}" + ("  (dry run)" if dry else ""))
        else:
            print(f"  -> case {cn} left at {STAGE_IN} (some documents not ready)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    run(connect(), args or None, dry="--dry-run" in sys.argv)
