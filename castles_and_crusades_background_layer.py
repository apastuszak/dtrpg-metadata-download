#!/usr/bin/env python3
"""
Tags (or removes) the full-page parchment/texture background on each page
of a PDF as its own Optional Content Group (layer), so it can be toggled
off in any PDF viewer that supports layers (Acrobat, PDF Expert, Xodo,
PDF-XChange -- NOT Preview.app, which doesn't support OCG visibility
toggling at all).

DETECTION METHOD
-----------------
A background candidate is identified by actually tracking the page's
graphics state (q/Q/cm) through its content stream and computing the true
on-page extent of each drawn XObject -- not by assuming "whatever is
drawn first is the background." Only an XObject whose drawn area covers
essentially the full page qualifies as a candidate.

Once a full-page candidate is found on a page, it's tagged as background
if EITHER:
  - something else is visibly drawn on top of it afterward (real text, a
    fill/stroke, or another image) -- i.e. it's clearly sitting *behind*
    real page content, or
  - the exact same background object is reused on 2+ pages elsewhere in
    the document -- i.e. it's clearly a recurring design asset, even on
    pages that are otherwise blank.

Pages where the only full-page-covering object is unique to that page AND
has nothing drawn on top of it (book covers, standalone chapter-divider
illustrations, back-cover art) are correctly left untouched.
"""
import sys
import argparse
import pikepdf
from pikepdf import Name, Dictionary, String


# ---------------------------------------------------------------------------
# 2D affine matrix helpers (PDF matrices are (a,b,c,d,e,f), row-vector convention)
# ---------------------------------------------------------------------------

IDENTITY = (1, 0, 0, 1, 0, 0)


def mat_mult(m1, m2):
    """m1 followed by m2 (PDF 'cm' semantics: new = m1 * current)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    a = a1 * a2 + b1 * c2
    b = a1 * b2 + b1 * d2
    c = c1 * a2 + d1 * c2
    d = c1 * b2 + d1 * d2
    e = e1 * a2 + f1 * c2 + e2
    f = e1 * b2 + f1 * d2 + f2
    return (a, b, c, d, e, f)


def apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def bbox_of_transformed_unit_square(m):
    pts = [apply(m, x, y) for x, y in [(0, 0), (1, 0), (1, 1), (0, 1)]]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_of_transformed_rect(m, rect):
    x0, y0, x1, y1 = [float(v) for v in rect]
    pts = [apply(m, x, y) for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def covers_page(bbox, page_w, page_h, tol_low=0.85, tol_high=1.35):
    x0, y0, x1, y1 = bbox
    w = x1 - x0
    h = y1 - y0
    return (page_w * tol_low <= w <= page_w * tol_high) and (
        page_h * tol_low <= h <= page_h * tol_high
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def find_full_page_candidate(page, instrs):
    """
    Walk the content stream tracking the CTM via q/Q/cm, and return the
    instruction index and Resources name of the first Do call whose drawn
    extent covers (approximately) the full page -- or (None, None) if no
    such call exists on this page.
    """
    mb = page.MediaBox
    page_w = float(mb[2]) - float(mb[0])
    page_h = float(mb[3]) - float(mb[1])

    stack = [IDENTITY]
    res = page.Resources

    for i, ins in enumerate(instrs):
        op = str(ins.operator)
        if op == "q":
            stack.append(stack[-1])
        elif op == "Q":
            if len(stack) > 1:
                stack.pop()
        elif op == "cm":
            vals = [float(v) for v in ins.operands]
            stack[-1] = mat_mult(tuple(vals), stack[-1])
        elif op == "Do":
            name = str(ins.operands[0])
            if "/XObject" not in res or name not in res.XObject:
                continue
            xobj = res.XObject[name]
            subtype = xobj.get("/Subtype")
            ctm = stack[-1]
            if subtype == Name("/Image"):
                bbox = bbox_of_transformed_unit_square(ctm)
            elif subtype == Name("/Form"):
                form_matrix = xobj.get("/Matrix", pikepdf.Array([1, 0, 0, 1, 0, 0]))
                fm = tuple(float(v) for v in form_matrix)
                combined = mat_mult(fm, ctm)
                bbox_raw = xobj.get("/BBox")
                if bbox_raw is None:
                    continue
                bbox = bbox_of_transformed_rect(combined, bbox_raw)
            else:
                continue
            if covers_page(bbox, page_w, page_h):
                return i, name
    return None, None


def has_meaningful_content(instrs, start_i):
    """True if anything *visible* is drawn after index start_i."""
    text_render_mode = 0  # 0 = fill (visible); 3 = invisible
    for ins in instrs[start_i:]:
        op = str(ins.operator)
        if op == "Do":
            return True
        if op == "Tr":
            text_render_mode = int(ins.operands[0])
        elif op == "Tj":
            s = str(ins.operands[0])
            if text_render_mode != 3 and s.strip(" \t\r\n"):
                return True
        elif op == "TJ":
            text = "".join(
                str(x) for x in ins.operands[0] if isinstance(x, pikepdf.String)
            )
            if text_render_mode != 3 and text.strip(" \t\r\n"):
                return True
        elif op in ("f", "f*", "S", "s", "B", "B*", "sh"):
            return True
    return False


def resolve_identity(xobj):
    """
    An identity used to detect reuse of the same background asset across
    pages. For a Form wrapping exactly one nested image, use that image's
    object id (so re-wrapped duplicates of the same image still match);
    otherwise use the XObject's own object id.
    """
    subtype = xobj.get("/Subtype")
    if subtype == Name("/Form"):
        sub_res = xobj.get("/Resources")
        imgs = []
        if sub_res and "/XObject" in sub_res:
            for n, x in sub_res.XObject.items():
                if x.get("/Subtype") == Name("/Image"):
                    imgs.append(x.objgen)
        if len(imgs) == 1:
            return imgs[0]
        return xobj.objgen
    return xobj.objgen


def find_removal_span(instrs, do_index):
    """Innermost q...Q bracket enclosing do_index, for --remove mode."""
    depth = 0
    start = None
    for i in range(do_index, -1, -1):
        op = str(instrs[i].operator)
        if op == "Q":
            depth += 1
        elif op == "q":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return None
    depth = 1
    for j in range(start + 1, len(instrs)):
        op = str(instrs[j].operator)
        if op == "q":
            depth += 1
        elif op == "Q":
            depth -= 1
            if depth == 0:
                return (start, j)
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Tag or remove the full-page background on each page of a PDF."
    )
    parser.add_argument("src", help="Input PDF")
    parser.add_argument("dst", help="Output PDF")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Delete the background entirely instead of tagging it as a "
        "toggleable layer. Cannot be undone.",
    )
    args = parser.parse_args()

    pdf = pikepdf.open(args.src)

    # Pass 1: find the full-page candidate (if any) on every page.
    page_data = []  # (page, instrs, do_index, name) or (page, instrs, None, None)
    for page in pdf.pages:
        instrs = pikepdf.parse_content_stream(page)
        do_i, name = find_full_page_candidate(page, instrs)
        page_data.append((page, instrs, do_i, name))

    # Pass 2: count how often each candidate's underlying asset recurs.
    from collections import Counter

    identities = []
    for page, instrs, do_i, name in page_data:
        if name is None:
            identities.append(None)
            continue
        xobj = page.Resources.XObject[name]
        identities.append(resolve_identity(xobj))
    identity_counts = Counter(i for i in identities if i is not None)

    # Pass 3: classify and act.
    ocg = None
    if not args.remove:
        ocg = pdf.make_indirect(
            Dictionary(
                Type=Name.OCG,
                Name=String("Background"),
                Usage=Dictionary(
                    View=Dictionary(ViewState=Name.ON),
                    Print=Dictionary(PrintState=Name.ON),
                ),
            )
        )
        pdf.Root.OCProperties = Dictionary(
            OCGs=[ocg],
            D=Dictionary(
                Name=String("Default"),
                ON=[ocg],
                OFF=[],
                Order=[ocg],
                BaseState=Name.ON,
            ),
        )

    changed = 0
    skipped = 0
    for (page, instrs, do_i, name), identity in zip(page_data, identities):
        if name is None:
            skipped += 1
            continue
        has_more = has_meaningful_content(instrs, do_i + 1)
        recurring = identity_counts[identity] >= 2
        if not (has_more or recurring):
            skipped += 1
            continue

        if args.remove:
            span = find_removal_span(instrs, do_i)
            if span is None:
                skipped += 1
                continue
            start, end = span
            new_instrs = instrs[:start] + instrs[end + 1 :]
            new_stream = pikepdf.unparse_content_stream(new_instrs)
            page.Contents = pdf.make_stream(new_stream)
        else:
            xobj = page.Resources.XObject[name]
            xobj.OC = ocg
        changed += 1

    if args.remove:
        pdf.remove_unreferenced_resources()

    pdf.save(args.dst)
    action = "Removed" if args.remove else "Tagged"
    print(f"{action} background on {changed} pages, skipped {skipped} pages")


if __name__ == "__main__":
    main()
