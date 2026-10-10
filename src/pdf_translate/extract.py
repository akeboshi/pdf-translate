import html
import math
import re
from dataclasses import dataclass, field

import pymupdf

SANS_HINTS = ("sans", "myriad", "helvetica", "arial", "gilroy", "guardian", "gothic", "frutiger", "futura", "verdana")
MONO_HINTS = ("mono", "courier", "consol", "menlo", "code")


@dataclass
class Unit:
    page: int
    rect: pymupdf.Rect
    source: str
    font_size: float
    line_height: float
    sans: bool
    bold: bool
    color: str
    align: str
    single_line: bool
    limit_x1: float | None = None
    angle: float = 0.0
    frame: tuple[float, float, float, float] | None = None  # rotated text: center x, center y, width, height
    role: str = "text"  # text | table | line: units of the same role and size share one font scale per page
    span_rects: list[pymupdf.Rect] = field(default_factory=list)
    # original link annotations inside this unit, keyed by the index used in <a href="#L<index>">
    links: dict[int, dict] = field(default_factory=dict)
    underlined: set[int] = field(default_factory=set)
    whole_link: int | None = None  # every span belongs to this one link (TOC entry, bare URL line)
    erase_rects: list[pymupdf.Rect] = field(default_factory=list)
    nowrap: bool = False  # lines are broken by design (<br>); shrink instead of wrapping further
    # dot leader of a TOC entry: (right end x, baseline y, font size, color, pattern); redrawn after the new title
    leader: tuple[float, float, float, str, str] | None = None
    translation: str | None = None


def _is_mono(span) -> bool:
    return bool(span["flags"] & pymupdf.TEXT_FONT_MONOSPACED) or any(h in span["font"].lower() for h in MONO_HINTS)


def _is_bold(span) -> bool:
    name = span["font"].lower()
    return bool(span["flags"] & pymupdf.TEXT_FONT_BOLD) or "bold" in name or "semibold" in name or "black" in name


def _is_italic(span) -> bool:
    name = span["font"].lower()
    return bool(span["flags"] & pymupdf.TEXT_FONT_ITALIC) or "italic" in name or name.endswith("-it")


def _is_sans(span) -> bool:
    return any(h in span["font"].lower() for h in SANS_HINTS)


def _hex(color: int) -> str:
    return f"#{color:06x}"


def _dominant(spans, key):
    weights: dict = {}
    for s in spans:
        k = key(s)
        weights[k] = weights.get(k, 0) + len(s["text"])
    return max(weights, key=weights.get)


def _span_html(span, block_bold: bool, block_color: int, base_size: float) -> str:
    text = html.escape(span["text"])
    if not text:
        return ""
    if _is_mono(span):
        text = f"<code>{text}</code>"
    if _is_italic(span):
        text = f"<i>{text}</i>"
    if _is_bold(span) and not block_bold:
        text = f"<b>{text}</b>"
    if span["flags"] & pymupdf.TEXT_FONT_SUPERSCRIPT or (span["size"] < base_size * 0.75 and span["text"].strip().isdigit()):
        text = f"<sup>{text}</sup>"
    if span["color"] != block_color:
        text = f'<span style="color:{_hex(span["color"])}">{text}</span>'
    if span.get("link") is not None:
        text = f'<a href="#L{span["link"]}">{text}</a>'
    return text


def _merge_tags(s: str) -> str:
    s = re.sub(r"</(i|b|code|sup)>(\s*)<\1>", r"\2", s)
    while True:
        merged = re.sub(r'(<a href="(#L\d+)">)((?:(?!</a>).)*)</a>(\s*)<a href="\2">', r"\1\3\4", s)
        if merged == s:
            return s
        s = merged


def _line_html(line, block_bold, block_color, base_size) -> str:
    return "".join(_span_html(s, block_bold, block_color, base_size) for s in line["spans"])


def _join_lines(parts: list[tuple[str, str]]) -> str:
    """parts: (html, plain) per line. Undo end-of-line hyphenation."""
    out = ""
    prev_plain = ""
    for h, plain in parts:
        if not out:
            out = h
        elif re.search(r"[A-Za-z]-$", prev_plain) and plain[:1].islower():
            out = out.rstrip()
            out = out[:-1] if out.endswith("-") else out
            out += h.lstrip()
        else:
            out = out.rstrip() + " " + h.lstrip()
        prev_plain = plain.rstrip()
    return _merge_tags(out)


def _stacked(lines) -> bool:
    """False when lines share a row (table rows, multi-column labels)."""
    for a, b in zip(lines, lines[1:]):
        ha = a["bbox"][3] - a["bbox"][1]
        if b["bbox"][1] < a["bbox"][1] + ha * 0.5:
            return False
    return True


# "Title . . . . 41" / "Title      41": dot leaders or wide space followed by a page number
TOC_RE = re.compile(r"^(.*?[^\s.])(?:\s*(?:\.\s*){3,}|\s{2,})([0-9]+|[ivxlcdm]+)\s*$", re.I)


def _split_toc_line(line):
    """Return (title-only line, x where the leader/page number starts) or None."""
    chars = [c for s in line["spans"] for c in s["chars"]]
    m = TOC_RE.match("".join(c["c"] for c in chars))
    if not m:
        return None
    keep = m.end(1)
    spans, n = [], 0
    for s in line["spans"]:
        cs = s["chars"][: max(0, keep - n)]
        n += len(s["chars"])
        if cs:
            bbox = pymupdf.Rect(cs[0]["bbox"])
            for c in cs[1:]:
                bbox |= pymupdf.Rect(c["bbox"])
            spans.append({**s, "chars": cs, "text": "".join(c["c"] for c in cs), "bbox": tuple(bbox)})
    title_rect = pymupdf.Rect(spans[0]["bbox"])
    for s in spans[1:]:
        title_rect |= pymupdf.Rect(s["bbox"])
    tail = next(c for c in chars[keep:] if not c["c"].isspace())
    # with a dot leader the number starts after the last dot
    digits = [c for c in chars[keep:] if c["c"].strip() and c["c"] != "."]
    number_x = digits[0]["bbox"][0] if digits else tail["bbox"][0]
    return {**line, "spans": spans, "bbox": tuple(title_rect)}, number_x, [c for c in chars[keep:] if c["c"] == "."]


BULLET_RE = re.compile(r"^\s*([\u2022\u25e6\u25aa\u25cf\u25cb\u25a0\u25a1\u2023\u2043\u2219\u00b7\uf0b7\uf0a7]|[\u2013\u2014]\s|\(?\d{1,2}[.)]\s)")


def _starts_item(line) -> bool:
    """A bullet or numbered list item always begins a new paragraph."""
    return bool(BULLET_RE.match("".join(s["text"] for s in line["spans"])))


def _line_size(line) -> int:
    spans = [s for s in line["spans"] if s["text"].strip()] or line["spans"]
    return round(_dominant(spans, lambda s: s["size"]))


def _first_word_width(line) -> float:
    chars = [c for s in line["spans"] for c in s["chars"]]
    while chars and chars[0]["c"].isspace():
        chars.pop(0)
    word = []
    for c in chars:
        if c["c"].isspace():
            break
        word.append(c)
    return word[-1]["bbox"][2] - word[0]["bbox"][0] if word else 0.0


def _rule_between(a, b, rules) -> bool:
    """A horizontal rule (table row border, shaded cell edge) separating two consecutive lines."""
    top = (a["bbox"][1] + a["bbox"][3]) / 2
    bottom = (b["bbox"][1] + b["bbox"][3]) / 2
    x0, x1 = min(a["bbox"][0], b["bbox"][0]), max(a["bbox"][2], b["bbox"][2])
    return any(top < y < bottom and rx0 < x1 and rx1 > x0 for rx0, rx1, y in rules)


def _is_justified(lines, right: float) -> bool:
    body = lines[:-1]
    return not body or sum(l["bbox"][2] >= right - 2 for l in body) >= len(body) * 0.6


def _line_style(line):
    spans = [s for s in line["spans"] if s["text"].strip()] or line["spans"]
    return _dominant(spans, lambda s: (s["color"], _is_bold(s)))


def _all_bold(line) -> bool:
    spans = [s for s in line["spans"] if s["text"].strip()]
    return bool(spans) and all(_is_bold(s) for s in spans)


def _starts_with_label(line) -> bool:
    """A line opening with a bold run such as "N2:" starts a new note inside a table cell."""
    spans = [s for s in line["spans"] if s["text"].strip()]
    return len(spans) > 1 and _is_bold(spans[0]) and not _is_bold(spans[-1])


def _paragraphs(block, rules=(), in_table=False, row_starts=(), measure_right=None):
    """Split a column of lines into paragraphs.

    Justified text ends a paragraph on a line that stops short of the right edge. Ragged-right text
    (Word documents, table cells) ends one where the next line's first word would still have fit.
    """
    lines = block["lines"]
    right = max(l["bbox"][2] for l in lines)
    # for running text the real limit is the page's text column, which a short paragraph may never reach
    fit_right = max(right, measure_right) if measure_right and not in_table else right
    tol = max(12.0, (right - block["bbox"][0]) * 0.05)
    # narrow ragged blocks outside tables (addresses, name lists, index columns) keep one line per item
    width = right - block["bbox"][0]
    prose = in_table or width >= 30 * _line_size(lines[0])
    justified = _is_justified(lines, right) or not prose
    group = []
    for idx, line in enumerate(lines):
        toc = _split_toc_line(line)
        if toc:
            if group:
                yield group, None
                group = []
            yield [toc[0]], (toc[1], toc[2])
            continue
        if group:
            prev = group[-1]
            height = prev["bbox"][3] - prev["bbox"][1]
            new_row = any(abs(line["bbox"][1] - y) < 2 for y in row_starts) or _starts_item(line)
            if (_line_size(line) != _line_size(prev) or line["bbox"][1] - prev["bbox"][3] > height * 0.5
                    or _rule_between(prev, line, rules) or new_row
                    or (in_table and (_line_style(line) != _line_style(prev) or _starts_with_label(line)))
                    or (_all_bold(prev) and not _all_bold(line))):
                yield group, None
                group = []
        group.append(line)
        nxt = lines[idx + 1] if idx + 1 < len(lines) else None
        if justified:
            ends = line["bbox"][2] < right - tol
        elif nxt is None:
            ends = False
        else:
            space = line["spans"][-1]["size"] * 0.3
            ends = line["bbox"][2] + space + _first_word_width(nxt) < fit_right - 1
        if ends:
            yield group, None
            group = []
    if group:
        yield group, None


def _boxed(lines, rules) -> bool:
    """Lines framed by hairlines above and below, i.e. a table cell even when it is its own block."""
    r = _bbox(lines)

    def spans(rx0, rx1):
        return min(rx1, r.x1) - max(rx0, r.x0) >= r.width * 0.8

    above = any(spans(rx0, rx1) and r.y0 - 15 <= y <= r.y0 + 3 for rx0, rx1, y in rules)
    below = any(spans(rx0, rx1) and r.y1 - 3 <= y <= r.y1 + 15 for rx0, rx1, y in rules)
    return above and below


def _column_right(block, all_lines) -> float:
    """Right edge of the text column a block sits in: the widest line on the page that starts where it starts."""
    x0 = block["bbox"][0]
    return max((lb[2] for lb in all_lines if abs(lb[0] - x0) < 3), default=block["bbox"][2])


def _columns(lines) -> list[list]:
    """Group lines into columns by horizontal overlap; Word tables put several cells in one block."""
    cols: list[list] = []
    for line in sorted(lines, key=lambda l: l["bbox"][0]):
        x0, x1 = line["bbox"][0], line["bbox"][2]
        hit = [c for c in cols if x0 < c[1] - 1 and x1 > c[0] + 1]
        merged = [min([x0] + [c[0] for c in hit]), max([x1] + [c[1] for c in hit]),
                  [line] + [l for c in hit for l in c[2]]]
        cols = [c for c in cols if c not in hit] + [merged]
    return [sorted(c[2], key=lambda l: (l["bbox"][1], l["bbox"][0])) for c in sorted(cols, key=lambda c: c[0])]


def _horizontal_rules(page: pymupdf.Page) -> list[tuple[float, float, float]]:
    rules = []
    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "l":
                p, q = item[1], item[2]
                if abs(p.y - q.y) < 1 and abs(p.x - q.x) > 10:
                    rules.append((min(p.x, q.x), max(p.x, q.x), p.y))
            # only hairline rects count: Word shades table rows with one filled rect per text line
            elif item[0] == "re":
                r = item[1]
                if r.width > 10 and r.height < 2:
                    rules.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
    return rules


def _make_unit(page_no, page_rect, lines, rect, line_pitch=None, toc=None, keep_breaks=False) -> Unit | None:
    limit_x1, leader_chars = toc if toc else (None, [])
    spans = [s for l in lines for s in l["spans"] if s["text"].strip()]
    if not spans:
        return None
    plain = "".join(s["text"] for s in spans)
    if not re.search(r"[A-Za-z]{2,}", plain):
        return None
    prose = " ".join(s["text"] for s in spans if not _is_mono(s))
    if not re.search(r"[A-Za-z]{3,}", prose):
        return None

    size = _dominant(spans, lambda s: round(s["size"], 1))
    bold = _dominant(spans, _is_bold)
    color = _dominant(spans, lambda s: s["color"])
    sans = _dominant(spans, _is_sans)
    parts = [(_line_html(l, bold, color, size), "".join(s["text"] for s in l["spans"])) for l in lines]
    if keep_breaks:
        source = _merge_tags("<br>".join(h.strip() for h, _ in parts))
    else:
        source = _join_lines(parts).strip()

    if line_pitch is None:
        line_pitch = size * 1.2
    line_height = min(max(line_pitch / size, 1.1), 1.8)

    align = "left"
    if len(lines) > 1:
        x0s = [l["bbox"][0] for l in lines]
        x1s = [l["bbox"][2] for l in lines]
        # flush right with a ragged left edge; a justified paragraph's indented first line does not count
        if max(x1s) - min(x1s) <= 2 and max(x0s[1:]) - min(x0s) > 20:
            align = "right"
    cx = (rect.x0 + rect.x1) / 2
    if len(lines) <= 2 and abs(cx - page_rect.width / 2) < 3 and rect.width < page_rect.width * 0.6:
        align = "center"

    if leader_chars:
        spans = spans + [{"bbox": c["bbox"]} for c in leader_chars]
    return Unit(
        page=page_no,
        rect=pymupdf.Rect(rect),
        source=source,
        font_size=size,
        line_height=line_height,
        sans=sans,
        bold=bold,
        color=_hex(color),
        align=align,
        single_line=len(lines) == 1,
        limit_x1=limit_x1,
        span_rects=[pymupdf.Rect(s["bbox"]) for s in spans],
        links={s["link"]: s["link_info"] for s in spans if s.get("link") is not None},
        underlined={s["link"] for s in spans if s.get("underline")},
        whole_link=_whole_link([s for s in spans if "text" in s]),
        erase_rects=[r for s in spans for r in s.get("underline", [])],
        nowrap=keep_breaks,
        leader=_leader(leader_chars, limit_x1),
    )


def _bbox(lines) -> pymupdf.Rect:
    r = pymupdf.Rect(lines[0]["bbox"])
    for l in lines[1:]:
        r |= pymupdf.Rect(l["bbox"])
    return r


def _is_display(block) -> bool:
    """Large-type blocks such as cover credits or titles, where each line break is part of the design."""
    spans = [s for l in block["lines"] for s in l["spans"] if s["text"].strip()]
    sizes = {round(s["size"]) for s in spans}
    # mixed sizes (e.g. "CHAPTER 1" over a chapter title) are separate elements, not one broken line
    return 1 < len(block["lines"]) <= 4 and len(sizes) == 1 and min(sizes) >= 14


def _line_text(line) -> str:
    return "".join(s["text"] for s in line["spans"]).strip()


def _continues(prev, cur) -> bool:
    """True when `cur` is the next part of the same paragraph that PyMuPDF split into its own block."""
    a, b = prev["lines"][-1], cur["lines"][0]
    if not (_horizontal(a) and _horizontal(b)) or _line_size(a) != _line_size(b):
        return False
    if re.search(r"[.!?:;\u201d\u2019\")]$", _line_text(a)) or not _line_text(b) or _starts_item(b):
        return False
    height = a["bbox"][3] - a["bbox"][1]
    if not (-height * 0.5 < b["bbox"][1] - a["bbox"][1] < height * 1.5):
        return False
    # the previous line must run to the shared right edge, otherwise it ended the paragraph (list items, labels)
    right = max(prev["bbox"][2], cur["bbox"][2])
    return a["bbox"][2] >= right - 3 and min(a["bbox"][2], b["bbox"][2]) > max(a["bbox"][0], b["bbox"][0])


def _merge_continued(blocks):
    merged = []
    for block in blocks:
        if merged and _continues(merged[-1], block):
            prev = merged[-1]
            bbox = pymupdf.Rect(prev["bbox"]) | pymupdf.Rect(block["bbox"])
            merged[-1] = {**prev, "lines": prev["lines"] + block["lines"], "bbox": tuple(bbox)}
        else:
            merged.append(block)
    return merged


def _link_of(char, links) -> int | None:
    box = pymupdf.Rect(char["bbox"])
    center = (box.tl + box.br) / 2
    return next((i for i, link in enumerate(links) if link["from"].contains(center)), None)


def _split_at_links(span, links) -> list[dict]:
    """Cut a span where a link starts or ends, so a link covering part of a span maps to exactly its words."""
    if not links or not span["chars"]:
        return [span]
    parts, current, owner = [], [], _link_of(span["chars"][0], links)
    for ch in span["chars"]:
        o = _link_of(ch, links)
        if o != owner and current:
            parts.append(current)
            current = []
        owner = o
        current.append(ch)
    parts.append(current)
    if len(parts) == 1:
        return [span]
    out = []
    for chars in parts:
        box = pymupdf.Rect(chars[0]["bbox"])
        for c in chars[1:]:
            box |= pymupdf.Rect(c["bbox"])
        out.append({**span, "chars": chars, "bbox": tuple(box)})
    return out


def _whole_link(spans) -> int | None:
    owners = {s.get("link") for s in spans if s["text"].strip()}
    return owners.pop() if len(owners) == 1 and None not in owners else None


def _leader(chars, number_x) -> tuple | None:
    if len(chars) < 3:
        return None
    gaps = [b["bbox"][0] - a["bbox"][2] for a, b in zip(chars, chars[1:])]
    pattern = ". " if sorted(gaps)[len(gaps) // 2] > 1.5 else "."
    size = chars[0]["bbox"][3] - chars[0]["bbox"][1]
    return (number_x - 2, chars[0]["origin"][1], size * 0.8, "#000000", pattern)


def _attach_link(span, links, hairlines) -> None:
    """Tag a span with the link annotation covering it and any underline drawn beneath it."""
    if not span["text"].strip():
        return
    box = pymupdf.Rect(span["bbox"])
    center = (box.tl + box.br) / 2
    for idx, link in enumerate(links):
        if link["from"].contains(center):
            span["link"], span["link_info"] = idx, link
            under = [h for h in hairlines
                     if box.y1 - 4 <= h.y0 <= box.y1 + 3 and h.x0 < box.x1 and h.x1 > box.x0]
            if under:
                span["underline"] = under
            return


def _rotated_frame(lines) -> tuple[float, tuple[float, float, float, float]]:
    """Angle (degrees, clockwise on the page) and the text box measured along the writing direction."""
    c, sn = lines[0]["dir"]
    us, vs = [], []
    for line in lines:
        for span in line["spans"]:
            size = span["size"]
            for ch in span["chars"]:
                ox, oy = ch["origin"]
                u = ox * c + oy * sn
                v = -ox * sn + oy * c
                us += [u, u + size * 0.55]
                vs += [v - size * 0.85, v + size * 0.25]
    uc, vc = (min(us) + max(us)) / 2, (min(vs) + max(vs)) / 2
    center = (uc * c - vc * sn, uc * sn + vc * c)
    return math.degrees(math.atan2(sn, c)), (*center, max(us) - min(us), max(vs) - min(vs))


def _horizontal(line) -> bool:
    return abs(line["dir"][1]) < 0.05 and line["dir"][0] > 0


def extract_units(page: pymupdf.Page) -> list[Unit]:
    flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    data = page.get_text("rawdict", flags=flags)
    units: list[Unit] = []
    rules = _horizontal_rules(page)
    links = page.get_links()
    hairlines = [d["rect"] for d in page.get_drawings() if d["rect"].height < 2 and d["rect"].width > 2]
    blocks = [b for b in data["blocks"] if b["type"] == 0 and b["lines"]]
    # a hairline just under a line of text is an underline, not a table border
    all_lines = [l["bbox"] for b in blocks for l in b["lines"]]
    rules = [(x0, x1, y) for x0, x1, y in rules
             if not any(lb[3] - 4 <= y <= lb[3] + 3 and x0 >= lb[0] - 2 and x1 <= lb[2] + 2 for lb in all_lines)]
    for block in blocks:
        for line in block["lines"]:
            line["spans"] = [part for s in line["spans"] for part in _split_at_links(s, links)]
            for s in line["spans"]:
                s["text"] = "".join(c["c"] for c in s["chars"])
                _attach_link(s, links, hairlines)
    for block in _merge_continued(blocks):

        rotated: dict[tuple[float, float], list] = {}
        for line in block["lines"]:
            if not _horizontal(line):
                rotated.setdefault((round(line["dir"][0], 2), round(line["dir"][1], 2)), []).append(line)
        for lines in rotated.values():
            u = _make_unit(page.number, page.rect, lines, _bbox(lines), lines[0]["spans"][0]["size"] * 1.2)
            if u:
                u.angle, u.frame = _rotated_frame(lines)
                u.single_line = False
                units.append(u)

        block = {**block, "lines": [line for line in block["lines"] if _horizontal(line)]}
        if not block["lines"]:
            continue
        groups = []
        cols = _columns(block["lines"])
        # in a multi-column block, a line in the leftmost column marks the start of a table row
        row_starts = [l["bbox"][1] for l in cols[0]] if len(cols) > 1 else []
        for ci, col in enumerate(cols):
            sub = {**block, "lines": col, "bbox": tuple(_bbox(col))}
            in_table = len(cols) > 1 or _boxed(col, rules)
            if not _stacked(col):
                groups += [([line], None, False, "line") for line in col]
            elif _is_display(sub):
                groups.append((col, None, True, "line"))
            else:
                role = "table" if in_table else "text"
                groups += [(g, limit, False, role)
                           for g, limit in _paragraphs(sub, rules, in_table, row_starts if ci else (),
                                                       _column_right(sub, all_lines))]
        for lines, limit_x1, keep_breaks, role in groups:
            pitch = None
            if len(lines) > 1:
                pitch = (lines[-1]["bbox"][1] - lines[0]["bbox"][1]) / (len(lines) - 1)
            u = _make_unit(page.number, page.rect, lines, _bbox(lines), pitch, limit_x1, keep_breaks)
            if u:
                u.role = role if len(lines) > 1 else "line"
                units.append(u)
    return units
