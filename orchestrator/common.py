import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

RAW_BUCKET = "raw-medical-docs"
PREVIEW_BUCKET = "processed-previews"
SUPPORTED_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}        
MAX_UPLOAD_BYTES = 50 * 1024 * 1024                        

STAGE_INTAKE = "INTAKE_IN_PROGRESS"        
STAGE_PENDING_OCR = "PENDING_OCR"          # Squad 1 input
STAGE_RFI = "RFI_RESCAN_REQUIRED"          # Squad 1 output: hospital must send a clearer scan
STAGE_REVIEW = "PENDING_HUMAN_REVIEW"      # orchestrator: low-confidence text waits for a person
STAGE_PENDING_PII = "PENDING_PII"          # Squad 2 input
STAGE_PENDING_FHIR = "PENDING_FHIR"        # Squad 3 input
STAGE_COMPLETED = "COMPLETED"              # Squad 3 output
STAGE_OCR_FAILED = "OCR_FAILED"            # Squad 1 failed repeatedly; stop retrying


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_") or "UNNAMED"


def safe_filename(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)) or "UNNAMED"


def doc_id_from_name(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", Path(safe_filename(name)).stem)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    from supabase import create_client
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (service_role key) first.")
    return create_client(url, key)
