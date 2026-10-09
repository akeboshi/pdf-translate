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
    span_rects: list[pymupdf.Rect] = field(default_factory=list)
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
    return text


def _merge_tags(s: str) -> str:
    return re.sub(r"</(i|b|code|sup)>(\s*)<\1>", r"\2", s)


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
    return {**line, "spans": spans, "bbox": tuple(title_rect)}, tail["bbox"][0]


def _line_size(line) -> int:
    spans = [s for s in line["spans"] if s["text"].strip()] or line["spans"]
    return round(_dominant(spans, lambda s: s["size"]))


def _paragraphs(block):
    """Split a block into paragraphs: a line that stops short of the right edge ends a paragraph."""
    right = block["bbox"][2]
    tol = max(12.0, (block["bbox"][2] - block["bbox"][0]) * 0.05)
    group = []
    for line in block["lines"]:
        toc = _split_toc_line(line)
        if toc:
            if group:
                yield group, None
                group = []
            yield [toc[0]], toc[1]
            continue
        if group and _line_size(line) != _line_size(group[-1]):
            yield group, None
            group = []
        group.append(line)
        if line["bbox"][2] < right - tol:
            yield group, None
            group = []
    if group:
        yield group, None


def _make_unit(page_no, page_rect, lines, rect, line_pitch=None, limit_x1=None, keep_breaks=False) -> Unit | None:
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
        if max(x1s) - min(x1s) <= 2 and max(x0s) - min(x0s) > 5:
            align = "right"
    cx = (rect.x0 + rect.x1) / 2
    if len(lines) <= 2 and abs(cx - page_rect.width / 2) < 3 and rect.width < page_rect.width * 0.6:
        align = "center"

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
    for block in data["blocks"]:
        if block["type"] != 0 or not block["lines"]:
            continue
        for line in block["lines"]:
            for s in line["spans"]:
                s["text"] = "".join(c["c"] for c in s["chars"])

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
        keep_breaks = False
        if not _stacked(block["lines"]):
            groups = [([line], None) for line in block["lines"]]
        elif _is_display(block):
            groups, keep_breaks = [(block["lines"], None)], True
        else:
            groups = list(_paragraphs(block))
        for lines, limit_x1 in groups:
            pitch = None
            if len(lines) > 1:
                pitch = (lines[-1]["bbox"][1] - lines[0]["bbox"][1]) / (len(lines) - 1)
            u = _make_unit(page.number, page.rect, lines, _bbox(lines), pitch, limit_x1, keep_breaks)
            if u:
                units.append(u)
    return units
