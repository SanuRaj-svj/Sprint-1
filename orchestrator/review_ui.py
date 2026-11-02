import human_review as hr


def show(sb, pipeline=None):
    import ipywidgets as w
    from IPython.display import display, clear_output

    items = hr.get_review_queue(sb)
    out = w.Output()
    if not items:
        display(w.HTML("<b>Nothing is waiting for human review.</b>"))
        return

    reviewer = w.Text(description="Your name:", placeholder="required - goes into the audit trail",
                      style={"description_width": "80px"}, layout=w.Layout(width="420px"))
    rows, boxes = [], []                                     # rows: (item, dropdown, textbox)
    cases = {}
    for it in items:
        cases.setdefault(it["case_number"], {}).setdefault((it["doc_id"], it["file_name"]), []).append(it)

    for case_number, docs in cases.items():
        boxes.append(w.HTML(f"<h3>Case {case_number}</h3>"))
        for (doc_id, file_name), its in docs.items():
            reason = w.Text(placeholder="reason for rescan (optional)", layout=w.Layout(width="260px"))
            rescan = w.Button(description="Send whole scan back (rescan)", button_style="warning",
                              layout=w.Layout(width="230px"))

            def do_rescan(_b, cn=case_number, did=doc_id, r=reason):
                if not reviewer.value.strip():
                    with out:
                        print("Enter your name first.")
                    return
                hr.request_rescan(sb, cn, did, r.value, reviewer.value.strip())
                with out:
                    clear_output()
                    print(f"{cn}: scan sent back to the hospital (RFI). Upload a clearer file with intake.py --replace.")

            rescan.on_click(do_rescan)
            boxes.append(w.HBox([w.HTML(f"<b>{file_name}</b>&nbsp;&nbsp;"), reason, rescan]))
            for it in its:
                png = hr.download_snippet(sb, it["snippet_key"])
                img = (w.Image(value=png, format="png", layout=w.Layout(max_height="60px", object_fit="contain"))
                       if png else w.HTML("<i>(no image)</i>"))
                action = w.Dropdown(options=[("Correct as shown", "CONFIRM"), ("I fixed the text", "EDIT"),
                                             ("Unreadable - leave out", "UNREADABLE")], value="CONFIRM",
                                    layout=w.Layout(width="200px"))
                text = w.Text(value=it["text"], layout=w.Layout(width="360px"))
                conf = it["confidence"]
                tag = f"p{it['page']} #{it['block_id']}  conf {conf:.2f}" + ("  CRITICAL" if it["is_critical"] else "")
                boxes.append(w.HBox([w.HTML(f"<small>{tag}</small>", layout=w.Layout(width="190px")), img, text, action]))
                rows.append((it, action, text))

    save = w.Button(description="Save decisions", button_style="success", icon="check")

    def do_save(_b):
        with out:
            clear_output()
            if not reviewer.value.strip():
                print("Enter your name first.")
                return
            by_case = {}
            for it, action, text in rows:
                d = {"doc_id": it["doc_id"], "page": it["page"], "block_id": it["block_id"], "action": action.value}
                if action.value == "EDIT":
                    d["text"] = text.value
                by_case.setdefault(it["case_number"], []).append(d)
            released = False
            for cn, decisions in by_case.items():
                try:
                    r = hr.apply_decisions(sb, cn, decisions, reviewer.value.strip())
                except ValueError as e:
                    print(f"{cn}: NOT saved - {e}")
                    continue
                released |= r["released"]
                print(f"{cn}: saved {r['applied']} decision(s); "
                      + ("released to Squad 2." if r["released"] else f"{r['still_pending']} still pending."))
            if released and pipeline is not None:
                pipeline.run_until_settled()

    save.on_click(do_save)
    display(w.VBox([reviewer, *boxes, save, out]))
