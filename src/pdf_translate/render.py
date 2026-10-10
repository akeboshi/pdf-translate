import html
import io
import os
import math
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from pymupdf import mupdf

from .extract import Unit


@dataclass
class Fonts:
    archive: pymupdf.Archive | None
    css: str
    # font files by role (serif / sans / sansbold) for measuring text width outside of Story
    files: dict[str, Path] = field(default_factory=dict)

    def measure(self, text: str, size: float, sans: bool, bold: bool) -> float:
        role = "sansbold" if bold else ("sans" if sans else "serif")
        path = self.files.get(role)
        font = _font_cache.get(path) if path else None
        if path and font is None:
            font = _font_cache[path] = pymupdf.Font(fontfile=str(path))
        if font is None:
            font = _font_cache.setdefault(None, pymupdf.Font("japan"))
        return font.text_length(text, fontsize=size)


_font_cache: dict = {}


def _find_first(dirs: list[Path], names: list[str]) -> tuple[Path, str] | None:
    for d in dirs:
        for n in names:
            if (d / n).exists():
                return d, n
    return None


def load_fonts(serif: str | None, sans: str | None, sans_bold: str | None) -> Fonts:
    """Resolve Japanese fonts. Falls back to MuPDF's built-in CJK font when none is found."""
    candidates = {
        "serif": [serif] if serif else [],
        "sans": [sans] if sans else [],
        "sansbold": [sans_bold] if sans_bold else [],
    }
    if platform.system() == "Darwin":
        sysdir = "/System/Library/Fonts"
        candidates["serif"] += [f"{sysdir}/ヒラギノ明朝 ProN.ttc"]
        candidates["sans"] += [f"{sysdir}/ヒラギノ角ゴシック W3.ttc"]
        candidates["sansbold"] += [f"{sysdir}/ヒラギノ角ゴシック W6.ttc"]
    elif platform.system() == "Windows":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates["serif"] += [str(windir / n) for n in ("yumin.ttf", "msmincho.ttc")]
        candidates["sans"] += [str(windir / n) for n in ("YuGothR.ttc", "YuGothM.ttc", "meiryo.ttc", "msgothic.ttc")]
        candidates["sansbold"] += [str(windir / n) for n in ("YuGothB.ttc", "meiryob.ttc")]
    else:
        for d in ["/usr/share/fonts/opentype/noto", "/usr/share/fonts/noto-cjk", "/usr/share/fonts/google-noto-cjk"]:
            candidates["serif"] += [f"{d}/NotoSerifCJK-Regular.ttc"]
            candidates["sans"] += [f"{d}/NotoSansCJK-Regular.ttc"]
            candidates["sansbold"] += [f"{d}/NotoSansCJK-Bold.ttc"]

    resolved = {k: next((Path(p) for p in v if Path(p).exists()), None) for k, v in candidates.items()}
    if resolved["sans"] is None:
        resolved["sans"] = resolved["serif"]
    if resolved["serif"] is None:
        resolved["serif"] = resolved["sans"]
    if resolved["sansbold"] is None:
        resolved["sansbold"] = resolved["sans"]

    if resolved["serif"] is None:
        return Fonts(archive=None, css="")

    archive = pymupdf.Archive()
    for p in {p.parent for p in resolved.values() if p}:
        archive.add(str(p))
    css = (
        f'@font-face {{font-family: jpserif; src: url("{resolved["serif"].name}");}}\n'
        f'@font-face {{font-family: jpserif; font-weight: bold; src: url("{resolved["sansbold"].name}");}}\n'
        f'@font-face {{font-family: jpsans; src: url("{resolved["sans"].name}");}}\n'
        f'@font-face {{font-family: jpsans; font-weight: bold; src: url("{resolved["sansbold"].name}");}}\n'
    )
    return Fonts(archive=archive, css=css, files={k: v for k, v in resolved.items() if v})


def _unit_css(u: Unit, font_scale: float) -> str:
    family = "jpsans" if u.sans else "jpserif"
    weight = "bold" if u.bold else "normal"
    return (
        f"body, p {{margin: 0; padding: 0;}}\n"
        f"body {{font-family: {family}; font-size: {u.font_size * font_scale:.2f}px; line-height: {u.line_height:.2f};"
        f" color: {u.color}; font-weight: {weight}; text-align: {u.align};}}\n"
        f"code {{font-family: monospace; font-size: 0.92em;}}\n"
        f"i {{font-style: italic;}}\n"
        f"sup {{font-size: 0.6em;}}\n"
        # MuPDF's built-in a[href] style (blue, underlined) outranks a plain "a" selector
        f"a[href] {{color: inherit; text-decoration: none;}}\n"
        f"a[href].ul {{text-decoration: underline;}}\n"
        + ("body {white-space: nowrap;}\n" if u.nowrap else "")
    )


def _link_html(u: Unit) -> str:
    """Restore the underline that the source drew as a separate line under linked words."""
    text = u.translation or ""
    for idx in u.underlined:
        text = text.replace(f'<a href="#L{idx}">', f'<a href="#L{idx}" class="ul">')
    return text


def _shrink(r: pymupdf.Rect) -> pymupdf.Rect:
    """Shrink a span box so redaction never clips glyphs of neighbouring lines."""
    dy = r.height * 0.3
    dx = min(0.5, r.width * 0.1)
    return pymupdf.Rect(r.x0 + dx, r.y0 + dy, r.x1 - dx, r.y1 - dy)


def _others(u: Unit, obstacles: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    own = pymupdf.Rect(u.rect.x0 - 1, u.rect.y0 - 1, u.rect.x1 + 1, u.rect.y1 + 1)
    return [o for o in obstacles if not (own.contains(o) or o.contains(u.rect))]


def _grow_down(u: Unit, rect: pymupdf.Rect, obstacles: list[pymupdf.Rect], page_rect: pymupdf.Rect) -> pymupdf.Rect:
    limit = page_rect.y1 - 20
    for o in _others(u, obstacles):
        if o.y0 > u.rect.y0 + 1 and o.x0 < rect.x1 and o.x1 > rect.x0:
            limit = min(limit, o.y0)
    return pymupdf.Rect(rect.x0, rect.y0, rect.x1, max(rect.y1, limit - 1))


def _grow_right(u: Unit, rect: pymupdf.Rect, obstacles: list[pymupdf.Rect], right_margin: float) -> pymupdf.Rect:
    limit = right_margin if u.limit_x1 is None else min(right_margin, u.limit_x1 - 3)
    for o in _others(u, obstacles):
        if o.x0 >= u.rect.x1 - 1 and o.y0 < rect.y1 - 1 and o.y1 > rect.y0 + 1:
            limit = min(limit, o.x0 - 2)
    return pymupdf.Rect(rect.x0, rect.y0, max(rect.x1, limit), rect.y1)


def _grow_left(u: Unit, rect: pymupdf.Rect, obstacles: list[pymupdf.Rect], left_margin: float) -> pymupdf.Rect:
    limit = left_margin
    for o in _others(u, obstacles):
        if o.x1 <= u.rect.x0 + 1 and o.y0 < rect.y1 - 1 and o.y1 > rect.y0 + 1:
            limit = max(limit, o.x1 + 2)
    return pymupdf.Rect(min(rect.x0, limit), rect.y0, rect.x1, rect.y1)


def _link_spec(link: dict, rect: pymupdf.Rect | None = None) -> dict:
    """A link dict that insert_link accepts; it silently drops named destinations, so resolve them to a page."""
    spec = {k: v for k, v in link.items() if k not in ("xref", "id")}
    if spec.get("kind") == pymupdf.LINK_NAMED and spec.get("page", -1) >= 0:
        spec = {"kind": pymupdf.LINK_GOTO, "page": spec["page"], "to": spec.get("to", pymupdf.Point(0, 0)),
                "zoom": spec.get("zoom", 0), "from": spec["from"]}
    if rect is not None:
        spec["from"] = rect
    return spec


class Renderer:
    """Draws all translations into one scratch PDF first so fonts are embedded once, then stamps them onto pages."""

    def __init__(self, fonts: Fonts, font_scale: float = 1.0):
        self.fonts = fonts
        self.font_scale = font_scale
        self.buffer = io.BytesIO()
        self.writer = pymupdf.DocumentWriter(self.buffer)
        self.placements: list[tuple[int, pymupdf.Rect]] = []
        self.scales: list[float] = []

    def _story(self, u: Unit) -> pymupdf.Story:
        return pymupdf.Story(
            html=f"<body>{_link_html(u)}</body>",
            user_css=self.fonts.css + _unit_css(u, self.font_scale),
            archive=self.fonts.archive,
        )

    def _fit(self, u: Unit, rect: pymupdf.Rect, scale_low: float):
        story = self._story(u)
        fit = story.fit_scale(
            pymupdf.Rect(0, 0, rect.width, rect.height),
            scale_min=1,
            scale_max=None if scale_low == 0 else 1 / scale_low,
            # without this flag an unbreakable (nowrap) line may overflow sideways and still count as fitting
            flags=mupdf.FZ_PLACE_STORY_FLAG_NO_OVERFLOW if u.nowrap else 0,
        )
        return fit if fit.big_enough else None

    def _layout(self, u: Unit, page, obstacles, left_margin, right_margin):
        """Pick the box for a unit and the largest scale (<= 1) at which its text fits there."""
        # keep the baseline close to the original when the text is set smaller than the source
        drop = u.font_size * (1 - self.font_scale) * 0.8 if u.single_line else 0
        rect = pymupdf.Rect(u.rect.x0, u.rect.y0 - 1 + drop, u.rect.x1 + 1, u.rect.y1 + 1 + drop)
        min_h = u.font_size * self.font_scale * u.line_height + 2
        if rect.height < min_h:
            rect = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + min_h)
        candidates = [rect]
        flush_right = u.single_line and u.rect.x0 > left_margin + 40 and abs(u.rect.x1 - right_margin) < 3
        if flush_right:
            u.align = "right"
            candidates.append(_grow_left(u, rect, obstacles, left_margin))
        elif u.single_line:
            candidates.append(_grow_right(u, rect, obstacles, right_margin))
        else:
            candidates.append(_grow_down(u, rect, obstacles, page.rect))
        for cand in candidates:
            if self._fit(u, cand, 1):
                return cand, 1.0
        fit = self._fit(u, candidates[-1], 0)
        return (candidates[-1], 1 / fit.parameter) if fit else (None, 0.0)

    def add_page(self, page: pymupdf.Page, units: list[Unit]) -> None:
        todo = [u for u in units if u.translation]
        if not todo:
            return

        text_lines = [
            pymupdf.Rect(line["bbox"])
            for block in page.get_text("dict")["blocks"] if block["type"] == 0
            for line in block["lines"]
        ]
        right_margin = max((r.x1 for r in text_lines), default=page.rect.x1 - 36)
        left_margin = min((r.x0 for r in text_lines), default=page.rect.x0 + 36)
        obstacles = list(text_lines)
        obstacles += [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
        obstacles += [d["rect"] for d in page.get_drawings()]

        links = page.get_links()
        # links whose words are carried as <a> tags get new rects after layout; the rest keep theirs
        moved = {idx for u in todo for idx in u.links if f'href="#L{idx}"' in (u.translation or "")}
        # a unit that is one link as a whole keeps it even if the model dropped the <a> tag
        moved |= {u.whole_link for u in todo if u.whole_link is not None}
        for u in todo:
            for r in u.span_rects:
                page.add_redact_annot(_shrink(r), fill=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
        underlines = [r for u in todo for r in u.erase_rects]
        if underlines:
            for r in underlines:
                page.add_redact_annot(r + (-1, -1, 1, 1), fill=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_NONE,
            )
        # apply_redactions drops link annotations that overlap the redacted text
        key = lambda r: tuple(round(c, 1) for c in r)  # noqa: E731
        moved_keys = {key(links[i]["from"]) for i in moved}
        for link in page.get_links():
            if key(link["from"]) in moved_keys:
                page.delete_link(link)
        remaining = {key(l["from"]) for l in page.get_links()}
        for i, link in enumerate(links):
            if i not in moved and key(link["from"]) not in remaining:
                page.insert_link(_link_spec(link))

        rotated = [u for u in todo if u.frame]
        flat = [u for u in todo if not u.frame]
        plans = [(u, *self._layout(u, page, obstacles, left_margin, right_margin)) for u in flat]

        # one scale per kind of text on a page, so neighbouring paragraphs do not end up in different sizes;
        # an outlier that needs far more shrinking keeps its own smaller scale instead of dragging the rest down
        groups: dict[tuple, list[float]] = {}
        for u, target, scale in plans:
            if target is not None:
                groups.setdefault((round(u.font_size * 2), u.sans, u.bold, u.role), []).append(scale)
        common = {}
        for k, scales in groups.items():
            ordered = sorted(scales)
            median = ordered[len(ordered) // 2]
            common[k] = min(s for s in scales if s >= median * 0.9)

        for u, target, scale in plans:
            if target is None:
                continue
            scale = min(scale, common[(round(u.font_size * 2), u.sans, u.bold, u.role)])
            self._draw(page, u, target, scale, links)
        for u in rotated:
            self._place_rotated(page, u)

    def _draw(self, page: pymupdf.Page, u: Unit, target: pymupdf.Rect, scale: float, links: list[dict]) -> None:
        box = pymupdf.Rect(0, 0, target.width / scale, target.height / scale)
        story = self._story(u)
        dev = self.writer.begin_page(box)
        _, filled = story.place(box)
        positions: list[tuple[str, tuple]] = []
        story.element_positions(lambda pos: positions.append((pos.href, pos.rect)) if pos.href else None)
        story.draw(dev)
        self.writer.end_page()
        self.scales.append(scale)
        w, h = box.width * scale, box.height * scale
        if u.align == "right":
            dest = pymupdf.Rect(target.x1 - w, target.y0, target.x1, target.y0 + h)
        else:
            dest = pymupdf.Rect(target.x0, target.y0, target.x0 + w, target.y0 + h)
        self.placements.append((page.number, dest, 0))
        self._relink(page, u, positions, dest, scale, links)
        if u.whole_link is not None and f'href="#L{u.whole_link}"' not in (u.translation or ""):
            page.insert_link(_link_spec(links[u.whole_link], pymupdf.Rect(dest.x0, dest.y0, dest.x0 + filled[2] * scale,
                                                                          dest.y0 + filled[3] * scale)))
        if u.leader:
            plain = html.unescape(re.sub(r"<[^>]+>", "", u.translation or ""))
            width = self.fonts.measure(plain, u.font_size * self.font_scale, u.sans, u.bold) * scale
            self._draw_leader(page, u, dest.x0 + width)

    @staticmethod
    def _draw_leader(page: pymupdf.Page, u: Unit, title_end: float) -> None:
        end_x, baseline, size, color, pattern = u.leader
        unit = pymupdf.get_text_length(pattern, fontname="helv", fontsize=size)
        count = int((end_x - title_end - 4) / unit)
        if count > 0:
            text = (pattern * count).rstrip()
            width = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
            rgb = tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
            page.insert_text((end_x - width, baseline), text, fontname="helv", fontsize=size, color=rgb)

    @staticmethod
    def _relink(page, u: Unit, positions, dest: pymupdf.Rect, scale: float, links: list[dict]) -> None:
        # Story reports one box per glyph; merge them into one rect per line and link
        lines: dict[tuple[str, int], pymupdf.Rect] = {}
        for href, (x0, y0, x1, y1) in positions:
            m = re.fullmatch(r"#L(\d+)", href or "")
            if not m or int(m.group(1)) not in u.links:
                continue
            r = pymupdf.Rect(dest.x0 + x0 * scale, dest.y0 + y0 * scale, dest.x0 + x1 * scale, dest.y0 + y1 * scale)
            k = (m.group(1), round(y0))
            lines[k] = lines[k] | r if k in lines else r
        for (idx, _), rect in lines.items():
            if rect.width > 0.5:
                # glyph boxes include the line gap; trim so the hot area stays on its own line
                inset = rect.height * 0.15
                page.insert_link(_link_spec(links[int(idx)], rect + (0, inset, 0, -inset)))

    def _place_rotated(self, page: pymupdf.Page, u: Unit) -> None:
        cx, cy, w, h = u.frame
        fit = self._fit(u, pymupdf.Rect(0, 0, w, h), 0)
        if fit is None:
            return
        story = self._story(u)
        dev = self.writer.begin_page(fit.rect)
        story.place(fit.rect)
        story.draw(dev)
        self.writer.end_page()
        self.scales.append(1 / fit.parameter)
        # show_pdf_page keeps the aspect ratio, so the bounding box of the rotated frame reproduces it exactly
        a = math.radians(u.angle)
        bw = w * abs(math.cos(a)) + h * abs(math.sin(a))
        bh = w * abs(math.sin(a)) + h * abs(math.cos(a))
        dest = pymupdf.Rect(cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)
        self.placements.append((page.number, dest, -u.angle))

    def finish(self, doc: pymupdf.Document) -> None:
        if not self.placements:
            return
        self.writer.close()
        scratch = pymupdf.open("pdf", self.buffer.getvalue())
        for i, (pno, dest, rotate) in enumerate(self.placements):
            doc[pno].show_pdf_page(dest, scratch, i, rotate=rotate)
