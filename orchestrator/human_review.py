from common import (STAGE_PENDING_PII, STAGE_REVIEW, STAGE_RFI, PREVIEW_BUCKET, now_iso)

REVIEW_ROUTINGS = {"SUGGEST_CONFIRM", "HUMAN_REVIEW"}
ACTIONS = {"CONFIRM", "EDIT", "UNREADABLE"}
MIN_ALNUM_CHARS = 2          

def _meaningful(text):
    return sum(ch.isalnum() for ch in str(text or "")) >= MIN_ALNUM_CHARS


def pending_blocks(raw_doc):
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
    if not key:
        return None
    try:
        return sb.storage.from_(PREVIEW_BUCKET).download(key)
    except Exception:
        return None


# ---------------------------------------------------------------- 3. decisions
def apply_decisions(sb, case_number, decisions, reviewer):
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
    reason = (reason or "").strip() or "page not legible"
    sb.table("claim_documents").update({
        "processing_status": "RFI_BLURRY_SCAN", "is_confidence_acceptable": False,
        "rfi_reason": f"Human reviewer {reviewer}: please upload a clearer scan - {reason}"}).eq("doc_id", doc_id).execute()
    sb.table("claims_cases").update({"pipeline_stage": STAGE_RFI}).eq("case_number", case_number).execute()
