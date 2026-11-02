"""
Intake: put documents into Supabase so Squad 1 can find them. Replaces the one-off cell that was
used to copy files from Colab storage into the database.

What it does for one case
  1. checks every file up front (type, size, duplicate names) - nothing is written if any file is bad,
  2. creates the claims_cases row at stage INTAKE_IN_PROGRESS (Squad 1 ignores that stage),
  3. uploads each file to raw-medical-docs/{case_number}/{file_name},
  4. creates one claim_documents row per file (processing_status = PENDING_OCR),
  5. only then flips the case to PENDING_OCR - Squad 1 can never see a half-uploaded case.

Re-sending a clearer scan for an RFI case:  intake_case(..., replace=True)  (CLI: --replace)
Safe to re-run: files already in the case are skipped unless replace=True.

CLI (from the orchestrator/ folder, with SUPABASE_URL / SUPABASE_SERVICE_KEY exported):
    python intake.py PA-2026-0001 ./scans/discharge.pdf ./scans/lab.png
    python intake.py PA-2026-0001 ./scans_for_case_1/            # a folder = all supported files in it
    python intake.py --inbox ./inbox          # one case per patient folder: Patient_01_Name/(docs/)files -> case Patient_01
    python intake.py PA-2026-0001 ./clear_scan.png --replace      # answer an RFI
    python intake.py ... --dry-run                                # check only, write nothing
"""
import mimetypes
import re
import sys
import time
from pathlib import Path

from common import (RAW_BUCKET, SUPPORTED_EXTS, MAX_UPLOAD_BYTES, STAGE_INTAKE, STAGE_PENDING_OCR,
                    STAGE_RFI, STAGE_OCR_FAILED, safe_name, safe_filename, doc_id_from_name, connect)

# A case in any other stage is already moving through (or finished) the pipeline: refuse to add files.
REOPENABLE_STAGES = {STAGE_INTAKE, STAGE_PENDING_OCR, STAGE_RFI, STAGE_OCR_FAILED}


class IntakeError(Exception):
    pass


def default_case_number(folder_name):
    """'Patient_01_Sunita_Sharma' -> 'Patient_01'. The patient's name must never become a case number
    (it would end up in the database, the storage path and every log). Other folder names are used as they are."""
    m = re.search(r"Patient[_ ]?(\d+)", str(folder_name), re.IGNORECASE)
    return f"Patient_{int(m.group(1)):02d}" if m else str(folder_name)


def find_inbox_root(path):
    """A zip often wraps everything in one extra folder. Step into it while the folder holds exactly one
    sub-folder, no files, and that sub-folder is itself a container (it has neither a 'docs' folder nor
    documents of its own - i.e. it is not a patient folder)."""
    p = Path(path)
    while True:
        subs = [x for x in p.iterdir() if x.is_dir() and not x.name.startswith((".", "__MACOSX"))]
        files = [x for x in p.iterdir() if x.is_file() and not x.name.startswith(".")]
        if len(subs) != 1 or files:
            return p
        s0 = subs[0]
        is_patient = (s0 / "docs").is_dir() or any(
            f.is_file() and f.suffix.lower() in SUPPORTED_EXTS for f in s0.iterdir())
        if is_patient:
            return p
        p = s0


def collect_files(paths, ignored=None):
    """Expand folders into the document files inside them (non-recursive).
    If a folder has a 'docs' sub-folder, that is used (so a sibling 'preprocessed/' folder of derived images is
    never uploaded by mistake). Files of other types found inside a folder (json, txt, ...) are skipped and
    listed in `ignored`; a file named explicitly is never skipped - it is validated instead."""
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            if (p / "docs").is_dir():
                p = p / "docs"
            for f in sorted(p.iterdir()):
                if not f.is_file() or f.name.startswith("."):
                    continue
                if f.suffix.lower() in SUPPORTED_EXTS:
                    out.append(f)
                elif ignored is not None:
                    ignored.append(f.name)
        else:
            out.append(p)
    return out


def validate_files(files):
    """Return a list of human-readable problems (empty list = all good)."""
    problems, seen = [], {}
    if not files:
        problems.append("no files given")
    for f in files:
        f = Path(f)
        if not f.is_file():
            problems.append(f"{f}: not found")
            continue
        if f.suffix.lower() not in SUPPORTED_EXTS:
            problems.append(f"{f.name}: unsupported type '{f.suffix}' (use {', '.join(sorted(SUPPORTED_EXTS))})")
            continue
        size = f.stat().st_size
        if size == 0:
            problems.append(f"{f.name}: file is empty")
        elif size > MAX_UPLOAD_BYTES:
            problems.append(f"{f.name}: {size / 1e6:.1f} MB exceeds the {MAX_UPLOAD_BYTES // 2**20} MB upload limit")
        did = doc_id_from_name(f.name)
        if did in seen:
            problems.append(f"{f.name}: same document id '{did}' as {seen[did]} - rename one "
                            f"(Squad 1 would skip it)")
        seen[did] = f.name
    return problems


def _upload(sb, key, data, content_type, retries=3):
    for attempt in range(1, retries + 1):
        try:
            sb.storage.from_(RAW_BUCKET).upload(
                path=key, file=data, file_options={"content-type": content_type, "upsert": "true"})
            return
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.5 * attempt)


def _get_or_create_case(sb, case_number, dry):
    rows = sb.table("claims_cases").select("case_id, case_number, pipeline_stage") \
             .eq("case_number", case_number).execute().data or []
    if rows:
        case = rows[0]
        if case["pipeline_stage"] not in REOPENABLE_STAGES:
            raise IntakeError(
                f"case {case_number} is at stage {case['pipeline_stage']}; files can only be added while it is at "
                f"{', '.join(sorted(REOPENABLE_STAGES))}. Use a new case number.")
        return case, False
    if dry:
        return {"case_id": None, "case_number": case_number, "pipeline_stage": None}, True
    res = sb.table("claims_cases").insert(
        {"case_number": case_number, "pipeline_stage": STAGE_INTAKE}).execute().data or []
    if not res:                                              # some client versions return no body
        res = sb.table("claims_cases").select("case_id, case_number, pipeline_stage") \
                .eq("case_number", case_number).execute().data
    return res[0], True


def intake_case(sb, case_number, paths, replace=False, dry=False):
    """Upload `paths` (files and/or folders) as one case. Returns a summary dict."""
    case_number = str(case_number).strip()
    if not case_number:
        raise IntakeError("case number is empty")
    ignored = []
    files = collect_files(paths, ignored)
    problems = validate_files(files)
    if problems:
        raise IntakeError("nothing uploaded - fix these first:\n  - " + "\n  - ".join(problems))

    case, is_new = _get_or_create_case(sb, case_number, dry)
    existing = {}
    if case["case_id"]:
        for r in sb.table("claim_documents").select("doc_id, file_name, processing_status") \
                   .eq("case_id", case["case_id"]).execute().data or []:
            existing[r["file_name"]] = r
    taken_ids = {doc_id_from_name(n): n for n in existing}

    plan = []                                                 # (file, stored_name, action)
    for f in files:
        stored = safe_filename(f.name)
        if stored in existing:
            plan.append((f, stored, "REPLACE" if replace else "SKIP_EXISTS"))
            continue
        clash = taken_ids.get(doc_id_from_name(stored))
        if clash:
            raise IntakeError(f"{f.name}: same document id as '{clash}' already in this case - rename the file")
        plan.append((f, stored, "NEW"))

    summary = {"case_number": case_number, "new_case": is_new, "dry_run": dry,
               "uploaded": [], "replaced": [], "skipped": [], "ignored": ignored,
               "folder": f"{safe_name(case_number)}/"}
    if dry:
        for f, stored, action in plan:
            {"NEW": summary["uploaded"], "REPLACE": summary["replaced"],
             "SKIP_EXISTS": summary["skipped"]}[action].append(stored)
        return summary

    for f, stored, action in plan:
        if action == "SKIP_EXISTS":
            summary["skipped"].append(stored)
            continue
        key = f"{safe_name(case_number)}/{stored}"
        ctype = mimetypes.guess_type(stored)[0] or "application/octet-stream"
        _upload(sb, key, f.read_bytes(), ctype)
        if action == "NEW":
            sb.table("claim_documents").insert({
                "case_id": case["case_id"], "file_name": stored, "raw_storage_path": key,
                "processing_status": "PENDING_OCR"}).execute()
            summary["uploaded"].append(stored)
        else:                                                 # replace: force Squad 1 to OCR it again
            sb.table("claim_documents").update({
                "raw_storage_path": key, "processing_status": "PENDING_OCR", "rfi_reason": None,
                "raw_ocr_json": None, "source_size_bytes": None}).eq("doc_id", existing[stored]["doc_id"]).execute()
            summary["replaced"].append(stored)

    total = len(sb.table("claim_documents").select("doc_id").eq("case_id", case["case_id"]).execute().data or [])
    sb.table("claims_cases").update(
        {"total_documents": total, "pipeline_stage": STAGE_PENDING_OCR}).eq("case_id", case["case_id"]).execute()
    return summary


def intake_inbox(sb, inbox, replace=False, dry=False, case_namer=default_case_number):
    """inbox/<patient folder>/(docs/)<files>  ->  one case per patient folder.
    The case number is case_namer(folder name); by default 'Patient_01_Sunita_Sharma' -> 'Patient_01'."""
    folders = sorted(p for p in Path(inbox).iterdir()
                     if p.is_dir() and not p.name.startswith((".", "__MACOSX")))
    numbers = [case_namer(p.name) for p in folders]
    dupes = {n for n in numbers if numbers.count(n) > 1}
    if dupes:
        raise IntakeError(f"two folders map to the same case number {sorted(dupes)}; pass your own case_namer")
    results = []
    for d, cn in zip(folders, numbers):
        try:
            results.append(intake_case(sb, cn, [d], replace=replace, dry=dry))
        except IntakeError as e:
            print(f"[{cn}] {e}", file=sys.stderr)
    return results


def _print(s):
    tag = " (dry run)" if s["dry_run"] else ""
    print(f"case {s['case_number']}{tag}: {'created' if s['new_case'] else 'existing'}; "
          f"uploaded {len(s['uploaded'])}, replaced {len(s['replaced'])}, skipped {len(s['skipped'])}")
    for k in ("uploaded", "replaced", "skipped", "ignored"):
        for n in s.get(k, []):
            print(f"   {k:9s} {n}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    flags = {a for a in argv if a.startswith("--") and a != "--inbox"}
    args = [a for a in argv if not a.startswith("--")]
    dry, rep = "--dry-run" in flags, "--replace" in flags
    try:
        if "--inbox" in argv:
            for s in intake_inbox(connect(), args[0], replace=rep, dry=dry):
                _print(s)
        elif len(args) >= 2:
            _print(intake_case(connect(), args[0], args[1:], replace=rep, dry=dry))
        else:
            sys.exit(__doc__)
    except IntakeError as e:
        sys.exit(f"INTAKE REFUSED: {e}")
