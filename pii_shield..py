
import glob
import json
import os
import re
import sys
from dataclasses import dataclass

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

LOW_CONF = 0.60          # same limit as the Socratic defence (Q1)
VERSION = "1.2"          # 1.2 = image masking support

# ---------------------------------------------------------------- Verhoeff (Aadhaar check digit)
_D = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],[3,4,0,1,2,8,9,5,6,7],
      [4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],[6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],
      [8,7,6,5,9,3,2,1,0,4],[9,8,7,6,5,4,3,2,1,0]]
_P = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],[8,9,1,6,0,4,3,5,2,7],
      [9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],[2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8]]


def verhoeff_ok(num: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


# ---------------------------------------------------------------- free-text patterns
SEP = r"[ \-\.]?"
AADHAAR_12 = re.compile(rf"(?<!\d)\d{{4}}{SEP}\d{{4}}{SEP}\d{{4}}(?!\d)")   # any 12 digits (checksum noted only)
ABHA = re.compile(r"(?<!\d)\d{2}-\d{4}-\d{4}-\d{4}(?!\d)")
PHONE = re.compile(r"(?<!\d)(?:\+?91[\s\-]?|0)?([6-9]\d{4}[\s\-]?\d{5})(?!\d)")
LANDLINE = re.compile(r"(?<!\d)0\d{2,4}[\s\-]\d{6,8}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
MRN = re.compile(r"(?i)\b(?:MRN|UHID)[\s\-:#]*\d+\b")
PIN = re.compile(r"(?<!\d)[1-9]\d{5}(?!\d)")
CONFUSABLE = str.maketrans("OoIlSB", "001158")
NOISY_DIGITS = re.compile(r"(?<![A-Za-z0-9])[0-9OoIlSB][0-9OoIlSB \-]{8,18}[0-9OoIlSB](?![A-Za-z0-9])")
LONG_DIGITS = re.compile(r"(?<!\d)\d[\d \-]{8,}\d(?!\d)")                    # strict mode only
TITLE_NAME = re.compile(r"\b(?:Mr|Mrs|Ms|Smt|Shri|Master|Baby)\.?[ \t]+([A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z]+){0,2})")
NAME_STOP = {"age", "sex", "gender", "mrn", "uhid", "dob", "mobile", "aadhaar", "address", "ref", "date", "admission",
             "discharge", "diagnosis", "procedure", "dr", "doctor", "consultant", "surgeon", "ward", "bed", "patient"}
KEEP_PREFIX = re.compile(r"(?i)^\s*(dr|doctor|prof)\b")                      # doctors are not masked

# ---------------------------------------------------------------- label -> value rules
# A label line is either "Label: value" (value on same line) or "Label" alone with the value on
# the NEXT line (how the Reader emits form fields, one block per line).
LABELS = [
    ("NAME",    r"patient(?:'s|s)?\s*name|patient|name\s*of\s*(?:the\s*)?patient|pt\.?\s*name|name"
                r"|(?:father|mother|husband|wife|spouse|guardian|relative|attendant|nominee)(?:'s)?(?:\s*name)?"
                r"|s/o|d/o|w/o|c/o"),
    ("ADDRESS", r"(?:permanent|present|current|resi(?:dential)?|correspondence)?\s*address|addr\.?"),
    ("DOB",     r"d\.?o\.?b\.?|date\s*of\s*birth|birth\s*date"),
    ("AADHAAR", r"aadhaar|aadhar|adhar|uid(?:ai)?"),            # optional "number/no/card" is consumed below
    ("PHONE",   r"mobile|mob|phone|contact|tel(?:ephone)?|cell"),
    ("MRN",     r"(?:medication|medical)\s*(?:/\s*(?:lab|mrn)\s*)?(?:no|number)|mrn|uhid|patient\s*id|ip\s*no|op\s*no"),
]
_TAIL = r"(?:\s*(?:number|no|card|#))?\.?"
LABEL_RES = [(k, re.compile(rf"(?i)^(?P<lead>[ \t>*\-]*)(?P<lab>(?:{p}){_TAIL})(?P<sep>\s*[:：]\s*|\s+-\s+|\s+|)(?P<val>.*)$"))
             for k, p in LABELS]
# lines that are field names but not PII; the next-line rule must never swallow them
OTHER_LABELS = re.compile(
    r"(?i)^\s*(age|sex|gender|ref\b|referred|admission|discharge|date|test|value|unit|reference|sample|doctor|"
    r"consultant|diagnosis|procedure|package|claim|bill|ward|bed|hospital|pathologist|checked|verified|"
    r"authori[sz]ed|result|remarks?|note|method|collection|report)")
MAX_ADDR_LINES = 3


@dataclass
class Hit:
    start: int
    end: int
    kind: str
    rule: str


_NEXT_FIELD = re.compile(r"\s{2,}|\s+(?=[A-Za-z/]{2,15}:)|\s+\|")


def _trim_value(val: str) -> str:
    m = _NEXT_FIELD.search(val)
    return val[:m.start()] if m else val


def _is_label_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if OTHER_LABELS.match(s):
        return True
    for _, rx in LABEL_RES:
        m = rx.match(s)
        if m and not m.group("val").strip():      # bare label with nothing after it
            return True
    return False


def _label_hits(text: str):
    hits, lines, pos = [], text.split("\n"), 0
    starts = []
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    for i, ln in enumerate(lines):
        for kind, rx in LABEL_RES:
            m = rx.match(ln)
            if not m:
                continue
            val, sep = m.group("val"), m.group("sep")
            has_colon = ":" in sep or "：" in sep or "-" in sep
            if val.strip():
                # same-line value: needs ':' / '-' unless it is a numeric id field
                numeric_field = kind in ("AADHAAR", "PHONE", "MRN", "DOB")
                if not (has_colon or (numeric_field and re.match(r"[\dA-Za-z+]", val) and re.search(r"\d", val))):
                    continue
                if KEEP_PREFIX.match(val):
                    break
                vs = starts[i] + m.start("val")
                val = _trim_value(val)
                ve = vs + len(val.rstrip())
                if kind in ("AADHAAR", "PHONE", "MRN", "DOB"):      # stop at the end of the id-like run
                    run = re.match(r"[A-Za-z]{0,5}[\-#]?[+\d][\d \t\-,/+.]*\d|[A-Za-z]{0,5}[\-#]?\d", val)
                    if run:
                        ve = vs + run.end()
                hits.append(Hit(vs, ve, kind, f"label_{kind.lower()}"))
                break
            # bare label -> value on the following line(s)
            n_lines = MAX_ADDR_LINES if kind == "ADDRESS" else 1
            j, taken = i + 1, 0
            while j < len(lines) and taken < n_lines:
                nxt = lines[j]
                if not nxt.strip():
                    j += 1
                    continue
                if _is_label_line(nxt) or KEEP_PREFIX.match(nxt):
                    break
                s0 = starts[j] + (len(nxt) - len(nxt.lstrip()))
                hits.append(Hit(s0, starts[j] + len(nxt.rstrip()), kind, f"label_{kind.lower()}_nextline"))
                taken += 1
                j += 1
            break
    return hits


def _find(text: str, strict: bool):
    hits = _label_hits(text)

    for m in AADHAAR_12.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        hits.append(Hit(m.start(), m.end(), "AADHAAR",
                        "aadhaar_12digit_verhoeff" if verhoeff_ok(digits) else "aadhaar_12digit"))
    for m in ABHA.finditer(text):
        hits.append(Hit(m.start(), m.end(), "ABHA_ID", "abha_14digit"))
    for m in PHONE.finditer(text):
        hits.append(Hit(m.start(), m.end(), "PHONE", "indian_mobile"))
    for m in LANDLINE.finditer(text):
        hits.append(Hit(m.start(), m.end(), "PHONE", "landline_std"))
    for m in NOISY_DIGITS.finditer(text):                       # OCR letter/digit confusions (O->0, l->1 ...)
        raw = m.group()
        norm = raw.translate(CONFUSABLE).replace(" ", "").replace("-", "")
        if sum(c.isdigit() for c in raw) >= 6 and norm.isdigit() and len(norm) in (10, 12, 14) and norm[0] in "23456789":
            hits.append(Hit(m.start(), m.end(), "NUMERIC_ID", "ocr_confusable_digits"))
    for m in EMAIL.finditer(text):
        hits.append(Hit(m.start(), m.end(), "EMAIL", "email"))
    for m in PAN.finditer(text):
        hits.append(Hit(m.start(), m.end(), "PAN", "pan"))
    for m in MRN.finditer(text):
        hits.append(Hit(m.start(), m.end(), "MRN", "mrn_pattern"))
    for m in TITLE_NAME.finditer(text):
        end = m.start(1)
        for w in re.finditer(r"[A-Za-z]+", m.group(1)):
            if w.group().lower() in NAME_STOP:
                break
            end = m.start(1) + w.end()
        if end > m.start(1):
            hits.append(Hit(m.start(1), end, "NAME", "title_name"))
    for m in PIN.finditer(text):                                # PIN only near an address cue
        ctx = text[max(0, m.start() - 80):m.start()].lower()
        if any(w in ctx for w in ("address", "pin", "dist", "road", "nagar", "colony", "village", "marg", "vihar", "mp", "bhopal")):
            hits.append(Hit(m.start(), m.end(), "PIN", "pin_with_address_context"))
    if strict:                                                  # low-OCR-confidence safety net
        for m in LONG_DIGITS.finditer(text):
            if len(re.sub(r"\D", "", m.group())) >= 10:
                hits.append(Hit(m.start(), m.end(), "NUMERIC_ID", "strict_long_digits"))
    return _drop_facility_contacts(text, hits)


def _drop_facility_contacts(text, hits):
    """Hospital / lab letterhead contact details (phone, PIN, email in the first lines, above the first
    patient identifier) are not personal data: leave them visible. If no patient identifier is found,
    nothing is treated as letterhead and everything is masked (safe default)."""
    ident = [h.start for h in hits if h.kind in ("NAME", "AADHAAR", "MRN", "DOB")]
    if not ident:
        return hits
    head_end = text.rfind("\n", 0, min(ident)) + 1               # start of the line holding the first identifier
    eighth_line = 0
    for _ in range(8):
        nl = text.find("\n", eighth_line)
        if nl == -1:
            eighth_line = len(text)
            break
        eighth_line = nl + 1
    head_end = min(head_end, eighth_line)
    return [h for h in hits
            if not (h.end <= head_end and not h.rule.endswith("_nextline")
                    and (h.kind in ("PHONE", "PIN", "EMAIL") or h.rule == "ocr_confusable_digits"))]


def _merge(hits):
    """Resolve overlaps: earliest, longest span wins; overlapping spans are fused."""
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    out = []
    for h in hits:
        if out and h.start < out[-1].end:
            if h.end > out[-1].end:
                out[-1].end = h.end
            continue
        out.append(h)
    return out


def mask_text(text: str, strict: bool = False):
    """Return (masked_text, log_entries). Offsets in the log refer to the ORIGINAL text."""
    hits = _merge(_find(text, strict))
    parts, last, log = [], 0, []
    for h in hits:
        parts.append(text[last:h.start])
        parts.append(f"[{h.kind}]")
        log.append({"type": h.kind, "rule": h.rule, "start": h.start, "end": h.end,
                    "length": h.end - h.start, "placeholder": f"[{h.kind}]"})
        last = h.end
    parts.append(text[last:])
    return "".join(parts), log


# ---------------------------------------------------------------- Reader JSON adapter
def _first(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _is_hold(status) -> bool:
    s = str(status or "").upper()
    return "RFI" in s or "RESCAN" in s


def _page_input(pg, doc_conf, include_held, doc_status=None):
    """Return (text, block_spans, confidence_min, held, hold_reasons, block_meta) for one page.
    block_meta maps block_id -> bounding_box (list of [x,y] corners) when available.
    For a held page the text is still returned (so the redaction log / counts exist) but the
    caller withholds it from the output unless include_held is set."""
    if isinstance(pg, str):
        return pg, [], doc_conf, False, [], {}
    blocks = pg.get("text_blocks")
    if blocks is not None:                                       # real Reader format
        held = _is_hold(pg.get("page_action")) or _is_hold(doc_status)
        reasons = list(pg.get("rfi_reasons") or [])
        pieces, spans, confs, pos = [], [], [], 0
        block_meta = {}
        for b in blocks:
            bid = b.get("block_id")
            if bid is not None and b.get("bounding_box"):
                block_meta[bid] = b["bounding_box"]
            if held:
                val = b.get("raw_text")                          # masked for the log; text withheld below
            elif b.get("blocked_downstream"):
                continue
            else:
                val = b.get("downstream_value")
            if val is None:
                continue
            spans.append((pos, pos + len(val), bid))
            pieces.append(val)
            pos += len(val) + 1
            if b.get("calibrated_confidence") is not None:
                confs.append(float(b["calibrated_confidence"]))
        return "\n".join(pieces), spans, (min(confs) if confs else None), held, reasons, block_meta
    t = _first(pg, ["text", "ocr_text", "content", "raw_text"])
    if t is None and "words" in pg:
        t = " ".join(str(_first(w, ["text", "word"], "")) for w in pg["words"])
    conf = _first(pg, ["confidence", "avg_confidence", "ocr_confidence"], doc_conf)
    return t or "", [], conf, False, [], {}


def _case_id(doc):
    """'Patient_01' from a path like .../Patient_01_Sunita_Sharma/docs/x.json. Never includes the name."""
    m = re.search(r"Patient[_ ]?(\d+)", str(doc.get("_case_hint") or doc.get("case_id") or ""))
    return f"Patient_{m.group(1)}" if m else None


# ---------------------------------------------------------------- image location + redaction
def _resolve_image_path(doc: dict, page_num: int, page_dict=None):
    """Find the preprocessed page image for this document/page.

    Search order:
      1. page_dict['preprocessed_image_path'] if it exists on disk
      2. Sibling of the OCR JSON:  <parent>/preprocessed/<doc_id>_p{N}_clean.png
         (and a few filename variants)
      3. Same directory as the OCR JSON
    """
    candidates = []
    if isinstance(page_dict, dict):
        p = page_dict.get("preprocessed_image_path")
        if p:
            candidates.append(p)
            # also try basename under common local layouts
            candidates.append(os.path.basename(p))

    hint = doc.get("_case_hint") or ""
    doc_id = _first(doc, ["document_id", "doc_id", "id"], "")
    base_names = [
        f"{doc_id}_p{page_num}_clean.png",
        f"{doc_id}_p{page_num}.png",
        f"{doc_id}.png",
        f"{doc_id}_page{page_num}.png",
    ]
    # source_file may be a PDF/PNG name
    src = _first(doc, ["source_file", "file", "filename", "source", "image"])
    if src:
        stem = os.path.splitext(os.path.basename(src))[0]
        base_names.extend([
            f"{stem}_p{page_num}_clean.png",
            f"{stem}_p{page_num}.png",
            f"{stem}.png",
        ])

    search_dirs = []
    if hint:
        ocr_dir = os.path.dirname(os.path.abspath(hint))
        search_dirs.append(ocr_dir)
        parent = os.path.dirname(ocr_dir)
        search_dirs.append(os.path.join(parent, "preprocessed"))
        search_dirs.append(os.path.join(parent, "images"))
        search_dirs.append(parent)
        # Patient_xx/.../docs/ -> Patient_xx/preprocessed
        grand = os.path.dirname(parent)
        if grand:
            search_dirs.append(os.path.join(grand, "preprocessed"))

    for d in search_dirs:
        for name in base_names:
            candidates.append(os.path.join(d, name))

    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c):
            return c
    return None


def _bbox_to_xyxy(bbox, pad=4):
    """Convert [[x1,y1],[x2,y2],[x3,y3],[x4,y4]] (or similar) to (x0,y0,x1,y1) with padding."""
    if not bbox:
        return None
    xs, ys = [], []
    for pt in bbox:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            xs.append(float(pt[0]))
            ys.append(float(pt[1]))
        elif isinstance(pt, dict):
            xs.append(float(pt.get("x", pt.get("left", 0))))
            ys.append(float(pt.get("y", pt.get("top", 0))))
    if not xs:
        return None
    return (max(0, int(min(xs) - pad)), max(0, int(min(ys) - pad)),
            int(max(xs) + pad), int(max(ys) + pad))


def redact_image(image_path: str, block_meta: dict, log_entries: list, out_path: str,
                 fill=(0, 0, 0), pad: int = 6) -> bool:
    """Black-out the bounding boxes of every block_id referenced in log_entries.
    Returns True on success."""
    if not HAS_PIL or not image_path or not os.path.isfile(image_path):
        return False
    try:
        img = Image.open(image_path)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        else:
            img = img.copy()
        draw = ImageDraw.Draw(img)
        seen = set()
        for e in log_entries:
            for bid in (e.get("block_ids") or []):
                if bid in seen:
                    continue
                seen.add(bid)
                box = _bbox_to_xyxy(block_meta.get(bid), pad=pad)
                if box:
                    draw.rectangle(box, fill=fill)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        img.save(out_path)
        return True
    except Exception as exc:
        print(f"  [warn] image redaction failed for {image_path}: {exc}", file=sys.stderr)
        return False


def shield_document(doc: dict, include_held: bool = False, image_out_dir: str = None) -> dict:
    pages_in = _first(doc, ["pages", "page_results", "ocr_pages"])
    if pages_in is None:                                         # flat doc
        pages_in = [{"text": _first(doc, ["text", "ocr_text", "content"], "")}]
    doc_conf = _first(doc, ["confidence", "avg_confidence", "ocr_confidence"])

    pages_out, total, ok_texts = [], 0, []
    for i, pg in enumerate(pages_in, 1):
        pno = _first(pg, ["page_number", "page"], i) if isinstance(pg, dict) else i
        text, spans, conf, held, reasons, block_meta = _page_input(
            pg, doc_conf, include_held, doc.get("document_status"))
        strict = conf is not None and float(conf) < LOW_CONF
        masked, log = mask_text(text, strict)
        for e in log:
            e["page"] = pno
            if spans:
                e["block_ids"] = [bid for (s, t, bid) in spans if s < e["end"] and t > e["start"]]
        total += len(log)
        status = "OK" if not held else ("HELD_BY_READER_OVERRIDDEN" if include_held else "HELD_BY_READER")
        if not held:
            ok_texts.append(masked)
        elif not include_held:
            masked = ""                                          # held by Reader: nothing flows downstream

        page_entry = {"page": pno, "status": status, "hold_reasons": reasons,
                      "masked_text": masked,
                      "ocr_confidence_min": conf, "strict_mode": strict,
                      "needs_human_review": strict,
                      "redaction_log": log,
                      "masked_image_path": None}

        # ---- optional image redaction ----
        if image_out_dir is not None and (not held or include_held):
            src_img = _resolve_image_path(doc, pno, pg if isinstance(pg, dict) else None)
            if src_img:
                doc_id = _first(doc, ["document_id", "doc_id", "id"], "unknown")
                out_name = f"{doc_id}_p{pno}_masked.png"
                out_img = os.path.join(image_out_dir, out_name)
                if redact_image(src_img, block_meta, log, out_img):
                    # store basename only so patient-name folders never leak into JSON
                    page_entry["masked_image_path"] = os.path.basename(out_img)
                    page_entry["masked_image_dir"] = os.path.abspath(image_out_dir)
            elif image_out_dir is not None:
                # only warn when user asked for images
                print(f"  [warn] no image found for {doc.get('document_id')} page {pno}", file=sys.stderr)

        pages_out.append(page_entry)

    src = _first(doc, ["source_file", "file", "filename", "source", "image"])
    return {"doc_id": _first(doc, ["document_id", "doc_id", "id"], os.path.basename(src) if src else "unknown"),
            "source_file": os.path.basename(src) if src else None,
            "case_id": _case_id(doc),
            "doc_category": doc.get("detected_doc_category"),
            "pii_shield_version": VERSION,
            "reader_status": doc.get("document_status"),
            "ready_for_extraction": any(p["masked_text"].strip() for p in pages_out if p["status"] == "OK"),
            "masked_text": "\n".join(ok_texts),
            "total_redactions": total,
            "pages": pages_out}


def shield(reader_json, include_held: bool = False, image_out_dir: str = None):
    docs = reader_json
    if isinstance(reader_json, dict):
        docs = _first(reader_json, ["documents", "docs", "results"], [reader_json])
    out = {"pii_shield_version": VERSION,
           "documents": [shield_document(d, include_held, image_out_dir) for d in docs]}
    if image_out_dir:
        out["masked_images_dir"] = os.path.abspath(image_out_dir)
    return out


def load_inputs(path):
    if os.path.isdir(path):                                  # flat folder or Patient_xx/docs/ layout
        files = sorted(glob.glob(os.path.join(path, "**", "*_ocr.json"), recursive=True))
    else:
        files = [path]
    docs = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and "documents" in d and "run_id" in d:   # batch_manifest.json: skip
            continue
        for x in (d if isinstance(d, list) else [d]):
            if isinstance(x, dict):
                x.setdefault("_case_hint", f)                # path only used to derive a name-free case id
            docs.append(x)
    return docs


if __name__ == "__main__":
    raw = sys.argv[1:]
    include_held = "--include-held" in raw
    # --images  or  --images /path/to/dir
    image_out_dir = None
    if "--images" in raw:
        idx = raw.index("--images")
        if idx + 1 < len(raw) and not raw[idx + 1].startswith("-") and not raw[idx + 1].endswith(".json"):
            image_out_dir = raw[idx + 1]
            raw = raw[:idx] + raw[idx + 2:]
        else:
            image_out_dir = ""          # placeholder; set after we know the json path
            raw = [a for a in raw if a != "--images"]
    args = [a for a in raw if not a.startswith("--")]
    if len(args) < 2:
        sys.exit(
            "usage: python pii_shield.py <reader_json_or_dir> [<more>...] <masked_output.json>\n"
            "       [--include-held] [--images [output_dir]]"
        )
    out_json = args[-1]
    if image_out_dir == "":
        stem = os.path.splitext(os.path.abspath(out_json))[0]
        image_out_dir = stem + "_images"
    if image_out_dir is not None:
        os.makedirs(image_out_dir, exist_ok=True)
        if not HAS_PIL:
            print("[warn] Pillow not installed – image masking disabled.  pip install Pillow", file=sys.stderr)
            image_out_dir = None

    docs_in = [d for a in args[:-1] for d in load_inputs(a)]
    result = shield(docs_in, include_held=include_held, image_out_dir=image_out_dir)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    docs = result["documents"]
    n_img = sum(1 for d in docs for p in d["pages"] if p.get("masked_image_path"))
    print(f"{len(docs)} documents | usable: {sum(d['ready_for_extraction'] for d in docs)} | "
          f"held by Reader: {sum(p['status'].startswith('HELD') for d in docs for p in d['pages'])} | "
          f"redactions: {sum(d['total_redactions'] for d in docs)} | "
          f"strict/human-review pages: {sum(p['needs_human_review'] for d in docs for p in d['pages'])} | "
          f"masked images: {n_img}")
    if image_out_dir and n_img:
        print(f"  images written to: {os.path.abspath(image_out_dir)}")
