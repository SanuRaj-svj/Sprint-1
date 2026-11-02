"""
Pipeline runner: Squad 1 -> (human-review gate) -> Squad 2 -> Squad 3, hands-free.

Nothing in any squad is modified or copied:
  * Squad 1 lives in a notebook, so its cells are loaded from OCR_reader/OCR.ipynb at run time and executed
    unchanged in one namespace (the Colab-only connection cell and the demo cells are not run).
  * Squad 2 and Squad 3 are run exactly as their READMEs say (`python db_runner.py [case numbers]`) in a
    subprocess. A subprocess is required: both folders contain a file called db_runner.py, and importing
    both into one process silently gives you the same module twice.

The squads already hand over through claims_cases.pipeline_stage; this file only decides *when* to run each one.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from common import (STAGE_INTAKE, STAGE_PENDING_OCR, STAGE_RFI, STAGE_REVIEW, STAGE_PENDING_PII,
                    STAGE_PENDING_FHIR, STAGE_COMPLETED, STAGE_OCR_FAILED)
import human_review

MAX_ATTEMPTS = 2          # a case that a squad leaves unchanged this many cycles in a row is reported, not retried


# ---------------------------------------------------------------- Squad 1 (loaded from the notebook, unchanged)
_SQUAD1_MARKERS = ("def load_document_pages",        # loader + preprocessing cell
                   "def process_document_to_json",   # OCR core cell
                   "def run_supabase_batch_pipeline")  # Supabase batch runner cell


def load_squad1(repo_root, sb):
    """Execute Squad 1's definition cells from OCR_reader/OCR.ipynb, untouched, in a private namespace.
    Returns the namespace; run_supabase_batch_pipeline / print_batch_summary live in it.
    Needs paddleocr + poppler installed (the Colab install cells do that)."""
    nb = json.loads((Path(repo_root) / "OCR_reader" / "OCR.ipynb").read_text(encoding="utf-8"))
    code_cells = ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]
    ns = {"__name__": "squad1_ocr",            # != "__main__", so the notebook's demo / auto-run blocks stay off
          "supabase": sb}                      # the global Squad 1's code expects
    done = set()
    for marker in _SQUAD1_MARKERS:
        src = next((s for s in code_cells if marker in s), None)
        if src is None:
            raise RuntimeError(f"Squad 1 notebook changed: no code cell contains '{marker}'. "
                               f"Update _SQUAD1_MARKERS in pipeline.py.")
        if src in done:                                 # one cell can hold more than one marker
            continue
        done.add(src)
        src = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("!", "%")))   # drop shell magics
        exec(compile(src, f"<OCR.ipynb: {marker}>", "exec"), ns)
    return ns


# ---------------------------------------------------------------- Squad 2 / 3 (subprocess, as in their READMEs)
def subprocess_runner(folder, name):
    def run(case_numbers):
        print(f"\n>>> {name}: python db_runner.py {' '.join(case_numbers)}")
        p = subprocess.Popen([sys.executable, "db_runner.py", *case_numbers], cwd=str(folder),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=os.environ.copy())
        for line in p.stdout:
            print("   " + line.rstrip())
        return p.wait()
    return run


# ---------------------------------------------------------------- state helpers
def _cases(sb, *stages):
    rows = []
    for s in stages:
        rows += sb.table("claims_cases").select("case_id, case_number, pipeline_stage") \
                  .eq("pipeline_stage", s).execute().data or []
    return sorted(rows, key=lambda r: r["case_number"])


def snapshot(sb):
    """{case_number: (stage, tuple of doc statuses)} - used to tell whether a cycle changed anything."""
    cases = sb.table("claims_cases").select("case_id, case_number, pipeline_stage").execute().data or []
    docs = sb.table("claim_documents").select("case_id, processing_status").execute().data or []
    by = {}
    for d in docs:
        by.setdefault(d["case_id"], []).append(d["processing_status"])
    return {c["case_number"]: (c["pipeline_stage"], tuple(sorted(by.get(c["case_id"], [])))) for c in cases}


def has_ocr_work(sb):
    """Mirrors Squad 1's own discovery rule: a case at PENDING_OCR, or any document still at PENDING_OCR."""
    if _cases(sb, STAGE_PENDING_OCR):
        return True
    return bool(sb.table("claim_documents").select("doc_id").eq("processing_status", "PENDING_OCR").execute().data)


def print_status(sb):
    snap = snapshot(sb)
    if not snap:
        print("(no cases)")
        return
    print(f"\n{'CASE':22s} {'STAGE':22s} DOCUMENTS")
    for cn, (stage, docs) in sorted(snap.items()):
        extra = ""
        if stage == STAGE_REVIEW:
            extra = f"   <- {len(human_review.get_review_queue(sb, cn))} block(s) waiting for a reviewer"
        elif stage == STAGE_RFI:
            extra = "   <- hospital must upload a clearer scan (intake.py --replace)"
        elif stage == STAGE_OCR_FAILED:
            extra = "   <- OCR failed repeatedly; check the file, then re-upload with --replace"
        print(f"{cn:22s} {stage:22s} {', '.join(docs) or '-'}{extra}")


# ---------------------------------------------------------------- the runner
class Pipeline:
    def __init__(self, sb, repo_root, squad1_ns=None, run_squad2=None, run_squad3=None, max_attempts=MAX_ATTEMPTS):
        self.sb, self.root, self.ns = sb, Path(repo_root), squad1_ns
        self.run_squad2 = run_squad2 or subprocess_runner(self.root / "squad2_pii", "Squad 2 (PII masking)")
        self.run_squad3 = run_squad3 or subprocess_runner(self.root / "squad3_extractor", "Squad 3 (clinical extractor)")
        self.max_attempts = max_attempts
        self.attempts = {}                              # (squad, case_number, stage) -> cycles left unchanged

    # -- Squad 1
    def _squad1(self):
        if self.ns is None or not has_ocr_work(self.sb):
            return
        print("\n>>> Squad 1 (OCR): run_supabase_batch_pipeline()")
        manifest = self.ns["run_supabase_batch_pipeline"]()
        self.ns["print_batch_summary"](manifest)
        for c in manifest["cases"]:                     # a case Squad 1 could not finish keeps its stage: count it
            if str(c["next_pipeline_stage"]).endswith("(unchanged)"):
                k = ("ocr", c["case_number"])
                self.attempts[k] = self.attempts.get(k, 0) + 1
                if self.attempts[k] >= self.max_attempts:
                    self.sb.table("claims_cases").update({"pipeline_stage": STAGE_OCR_FAILED}) \
                        .eq("case_id", c["case_uuid"]).execute()
                    print(f"  !! {c['case_number']}: OCR could not complete after {self.attempts[k]} tries -> "
                          f"{STAGE_OCR_FAILED}")

    # -- Squad 2 / 3 with a retry budget per case
    def _stage_runner(self, squad, stage, runner):
        cases = [c["case_number"] for c in _cases(self.sb, stage)]
        todo = []
        for cn in cases:
            n = self.attempts.get((squad, cn, stage), 0)
            if n >= self.max_attempts:
                print(f"  !! {cn}: stuck at {stage} after {n} tries - needs attention, not retrying")
            else:
                todo.append(cn)
        if not todo:
            return
        runner(todo)
        for cn in todo:                                 # still at the same stage => it did not advance
            if any(c["case_number"] == cn for c in _cases(self.sb, stage)):
                self.attempts[(squad, cn, stage)] = self.attempts.get((squad, cn, stage), 0) + 1

    def run_cycle(self):
        """One pass over every stage. Returns True if anything changed."""
        before = snapshot(self.sb)
        self._squad1()
        held = human_review.hold_cases_for_review(self.sb)
        for cn, n in held:
            print(f"\n>>> Review gate: {cn} -> {STAGE_REVIEW} ({n} uncertain block(s) need a person)")
        self._stage_runner("squad2", STAGE_PENDING_PII, self.run_squad2)
        self._stage_runner("squad3", STAGE_PENDING_FHIR, self.run_squad3)
        return snapshot(self.sb) != before

    def run_until_settled(self, max_cycles=10):
        """Keep cycling until a pass changes nothing: every case is then COMPLETED or waiting for a person
        (human review / hospital rescan). This is the 'feed Squad 1 and walk away' call."""
        for i in range(1, max_cycles + 1):
            print(f"\n================ cycle {i} ================")
            if not self.run_cycle():
                break
        print_status(self.sb)

    def watch(self, poll_seconds=30, max_minutes=60):
        """Poll for new work (e.g. fresh intake) for a while. Stop the cell with the stop button."""
        end = time.time() + max_minutes * 60
        while time.time() < end:
            if self.run_cycle():
                print_status(self.sb)
            time.sleep(poll_seconds)
