"""
Human-review stage (the 'pending human review' step promised in the Socratic defence, Q1).

Why it exists
  Squad 1 routes every OCR block to AUTO / SUGGEST_CONFIRM / HUMAN_REVIEW. Only AUTO text flows on; the other
  blocks are blocked (blocked_downstream = true) and Squad 2/3 never see them. Nothing in the squads asks a
  person about those blocks, so they used to vanish silently. This module closes that gap WITHOUT changing a
  squad: it sits between Squad 1 and Squad 2 and uses only the fields Squad 1 already writes.

Flow
  Squad 1 -> case at PENDING_PII
          -> hold_cases_for_review()   case has unreviewed uncertain blocks  -> stage PENDING_HUMAN_REVIEW
          -> a person works through get_review_queue() (cropped image + suggested text)
          -> apply_decisions()         writes the verified text into the Squad 1 contract
                                       (downstream_value, blocked_downstream=false, verified_status=HUMAN_VERIFIED)
          -> when nothing is pending, the case goes back to PENDING_PII and Squad 2 picks it up as normal.

Reviewer actions per block
  CONFIRM     the OCR text shown is correct
  EDIT        use the text the reviewer typed
  UNREADABLE  leave this text out (a conscious human decision, recorded)
Per document
  request_rescan()   page is not usable at all -> RFI_BLURRY_SCAN, case goes to RFI_RESCAN_REQUIRED

Only the Squad 1 JSON contract is touched, and only on documents that are still waiting for Squad 2.
"""
from common import (STAGE_PENDING_PII, STAGE_REVIEW, STAGE_RFI, PREVIEW_BUCKET, now_iso)

REVIEW_ROUTINGS = {"SUGGEST_CONFIRM", "HUMAN_REVIEW"}
ACTIONS = {"CONFIRM", "EDIT", "UNREADABLE"}
MIN_ALNUM_CHARS = 2          # blocks with less text than this (stray marks) are not worth a reviewer's time


def _meaningful(text):
    return sum(ch.isalnum() for ch in str(text or "")) >= MIN_ALNUM_CHARS


def pending_blocks(raw_doc):
    """Blocks of one Squad-1 document that still need a person: uncertain routing, not yet reviewed, on a
    page that is not already being sent back for a rescan. Returns [(page_number, block_dict)]."""
    out = []
    for pg in (raw_doc or {}).get("pages", []):
        if pg.get("page_action") == "TRIGGER_PAGE_RFI_RESCAN":
            continue
        for b in pg.get("text_blocks", []):
            if (b.get("routing_decision") in REVIEW_ROUTINGS and not b.get("human_review")
                    and _meaningful(b.get("raw_text"))):
                out.append((pg.get("page_number"), b))
    return out


def _case_docs(sb, case_uuid):
    return (sb.table("claim_documents")
            .select("doc_id, file_name, processing_status, raw_ocr_json, audit_summary")
            .eq("case_id", case_uuid).execute().data or [])


def _pending_in_case(docs):
    return sum(len(pending_blocks(d["raw_ocr_json"])) for d in docs
               if d["processing_status"] == "PENDING_PII" and isinstance(d.get("raw_ocr_json"), dict))


# ---------------------------------------------------------------- 1. gate
def hold_cases_for_review(sb, dry=False):
    """Call AFTER Squad 1 and BEFORE Squad 2. Moves every PENDING_PII case that still has uncertain blocks to
    PENDING_HUMAN_REVIEW so that Squad 2 (which only looks at PENDING_PII) skips it. Returns [(case_number, n)]."""
    held = []
    cases = sb.table("claims_cases").select("case_id, case_number, pipeline_stage") \
              .eq("pipeline_stage", STAGE_PENDING_PII).execute().data or []
    for c in cases:
        n = _pending_in_case(_case_docs(sb, c["case_id"]))
        if n:
            held.append((c["case_number"], n))
            if not dry:
                sb.table("claims_cases").update({"pipeline_stage": STAGE_REVIEW}).eq("case_id", c["case_id"]).execute()
    return held


# ---------------------------------------------------------------- 2. queue
def get_review_queue(sb, case_number=None):
    """Everything a reviewer has to look at, one dict per block, ready for a UI."""
    q = sb.table("claims_cases").select("case_id, case_number").eq("pipeline_stage", STAGE_REVIEW)
    cases = q.execute().data or []
    if case_number:
        cases = [c for c in cases if c["case_number"] == case_number]
    items = []
    for c in sorted(cases, key=lambda r: r["case_number"]):
        for d in sorted(_case_docs(sb, c["case_id"]), key=lambda r: r["file_name"]):
            if d["processing_status"] != "PENDING_PII" or not isinstance(d.get("raw_ocr_json"), dict):
                continue
            for page, b in pending_blocks(d["raw_ocr_json"]):
                items.append({
                    "case_number": c["case_number"], "doc_id": d["doc_id"], "file_name": d["file_name"],
                    "page": page, "block_id": b["block_id"], "text": b["raw_text"],
                    "confidence": b.get("calibrated_confidence"), "routing": b["routing_decision"],
                    "is_critical": bool(b.get("is_critical_field")),
                    "snippet_key": b.get("cropped_snippet_path"),
                })
    return items


def download_snippet(sb, key):
    """PNG bytes of the cropped line image (processed-previews bucket), or None."""
    if not key:
        return None
    try:
        return sb.storage.from_(PREVIEW_BUCKET).download(key)
    except Exception:
        return None


# ---------------------------------------------------------------- 3. decisions
def apply_decisions(sb, case_number, decisions, reviewer):
    """decisions: [{"doc_id", "page", "block_id", "action": CONFIRM|EDIT|UNREADABLE, "text": <for EDIT>}].
    Writes them into raw_ocr_json and, if nothing is pending any more, releases the case to PENDING_PII.
    Returns {"applied": n, "still_pending": n, "released": bool}."""
    if not reviewer or not str(reviewer).strip():
        raise ValueError("reviewer name is required (it goes into the audit trail)")
    case = sb.table("claims_cases").select("case_id, case_number, pipeline_stage") \
             .eq("case_number", case_number).execute().data
    if not case or case[0]["pipeline_stage"] != STAGE_REVIEW:
        raise ValueError(f"case {case_number} is not waiting for human review")
    case_uuid = case[0]["case_id"]
    docs = {d["doc_id"]: d for d in _case_docs(sb, case_uuid)}

    by_doc = {}
    for d in decisions:
        if d.get("action") not in ACTIONS:
            raise ValueError(f"unknown action {d.get('action')!r}")
        if d["action"] == "EDIT" and not str(d.get("text") or "").strip():
            raise ValueError(f"EDIT of block {d.get('block_id')} needs the corrected text")
        by_doc.setdefault(d["doc_id"], []).append(d)

    applied, ts = 0, now_iso()
    for doc_id, items in by_doc.items():
        row = docs.get(doc_id)
        if row is None or not isinstance(row.get("raw_ocr_json"), dict):
            raise ValueError(f"document {doc_id} has no OCR data to review")
        raw = row["raw_ocr_json"]
        index = {(pg.get("page_number"), b.get("block_id")): b
                 for pg in raw.get("pages", []) for b in pg.get("text_blocks", [])}
        counts = {"CONFIRM": 0, "EDIT": 0, "UNREADABLE": 0}
        for d in items:
            b = index.get((d["page"], d["block_id"]))
            if b is None or b.get("routing_decision") not in REVIEW_ROUTINGS:
                raise ValueError(f"block {d['block_id']} on page {d['page']} is not a reviewable block")
            if b.get("human_review"):
                continue                                              # already decided: never overwrite silently
            b["human_review"] = {"action": d["action"], "reviewer": str(reviewer).strip(), "reviewed_at_utc": ts,
                                 "original_routing": b["routing_decision"],
                                 "original_calibrated_confidence": b.get("calibrated_confidence")}
            if d["action"] == "UNREADABLE":
                b["verified_status"] = "REVIEWED_UNREADABLE"          # stays blocked: text is not used
            else:
                text = b["raw_text"] if d["action"] == "CONFIRM" else str(d["text"]).strip()
                b["downstream_value"] = text
                b["blocked_downstream"] = False
                b["verified_status"] = "HUMAN_VERIFIED"
                b["calibrated_confidence"] = 1.0                      # a person has verified it
            counts[d["action"]] += 1
            applied += 1
        audit = dict(row.get("audit_summary") or {})
        prev = audit.get("human_review") or {}
        audit["human_review"] = {                                     # counts only: no patient text in the audit
            "reviewer": str(reviewer).strip(), "last_reviewed_at_utc": ts,
            "confirmed": int(prev.get("confirmed", 0)) + counts["CONFIRM"],
            "edited": int(prev.get("edited", 0)) + counts["EDIT"],
            "unreadable": int(prev.get("unreadable", 0)) + counts["UNREADABLE"]}
        sb.table("claim_documents").update({"raw_ocr_json": raw, "audit_summary": audit}).eq("doc_id", doc_id).execute()

    return {"applied": applied, **release_if_complete(sb, case_uuid)}


def release_if_complete(sb, case_uuid):
    left = _pending_in_case(_case_docs(sb, case_uuid))
    if left == 0:
        sb.table("claims_cases").update({"pipeline_stage": STAGE_PENDING_PII}).eq("case_id", case_uuid).execute()
    return {"still_pending": left, "released": left == 0}


def request_rescan(sb, case_number, doc_id, reason, reviewer):
    """The scan is unusable: send it back to the hospital (same RFI state Squad 1 uses for blurry pages).
    The hospital's new file goes in via intake.py --replace and Squad 1 OCRs it again."""
    reason = (reason or "").strip() or "page not legible"
    sb.table("claim_documents").update({
        "processing_status": "RFI_BLURRY_SCAN", "is_confidence_acceptable": False,
        "rfi_reason": f"Human reviewer {reviewer}: please upload a clearer scan - {reason}"}).eq("doc_id", doc_id).execute()
    sb.table("claims_cases").update({"pipeline_stage": STAGE_RFI}).eq("case_number", case_number).execute()
