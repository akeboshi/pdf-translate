import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .extract import Unit, extract_units
from .render import Renderer, load_fonts
from .translate import (
    AgyBackend,
    AnthropicBackend,
    ClaudeCliBackend,
    DummyBackend,
    TranslationCache,
    batches,
)

BACKENDS = ["agy", "claude-cli", "anthropic", "dummy"]
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5"}


@dataclass
class Options:
    input: Path
    output: Path | None = None
    pages: str | None = None
    only_selected: bool = False
    backend: str = "anthropic"
    model: str | None = None
    effort: str = "low"
    fallback: bool = True
    glossary: Path | None = None
    workers: int = 4
    batch_chars: int = 6000
    cache: Path | None = None
    translate_toc: bool = True
    font_scale: float = 0.85
    serif_font: str | None = None
    sans_font: str | None = None
    sans_bold_font: str | None = None

    @property
    def output_path(self) -> Path:
        return self.output or self.input.with_suffix(".ja.pdf")


class Cancelled(Exception):
    pass


Log = Callable[[str], None]
Progress = Callable[[str, int, int], None]


def parse_pages(spec: str | None, count: int) -> list[int]:
    if not spec or not spec.strip():
        return list(range(count))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = int(a) if a.strip() else 1
            end = int(b) if b.strip() else count
            pages.update(range(start - 1, min(end, count)))
        else:
            pages.add(int(part) - 1)
    return sorted(p for p in pages if 0 <= p < count)


def make_backend(opts: Options):
    glossary = opts.glossary.read_text(encoding="utf-8") if opts.glossary else None
    if opts.backend == "anthropic":
        return AnthropicBackend(opts.model or DEFAULT_MODELS["anthropic"], opts.effort, glossary, opts.fallback)
    if opts.backend == "claude-cli":
        return ClaudeCliBackend(opts.model, glossary)
    if opts.backend == "agy":
        return AgyBackend(opts.model, glossary)
    return DummyBackend()


class Translator:
    def __init__(self, opts: Options, log: Log, progress: Progress, cancel: threading.Event):
        self.opts = opts
        self.log = log
        self.progress = progress
        self.cancel = cancel
        self.backend = make_backend(opts)
        self.cache = TranslationCache(opts.cache or opts.output_path.with_suffix(".cache.jsonl"))
        self.salt = f"{opts.backend}:{opts.model or DEFAULT_MODELS.get(opts.backend, '')}:ja"

    def translate(self, texts: list[str], label: str) -> list[str | None]:
        keys = [TranslationCache.key(t, self.salt) for t in texts]
        results: list[str | None] = [self.cache.get(k) for k in keys]
        pending = [i for i, r in enumerate(results) if r is None]
        if not pending:
            return results

        groups = [[pending[j] for j in b] for b in batches([texts[i] for i in pending], self.opts.batch_chars, 80)]
        self.log(f"[{label}] {len(pending)} segments to translate in {len(groups)} requests "
                 f"({len(texts) - len(pending)} cached)")

        def run(group: list[int]):
            for attempt in range(3):
                if self.cancel.is_set():
                    break
                try:
                    return group, self.backend([texts[i] for i in group])
                except Exception as e:  # noqa: BLE001 - keep going; failed segments stay in English
                    self.log(f"  request failed (attempt {attempt + 1}/3): {e}")
                    time.sleep(2 ** attempt)
            return group, [None] * len(group)

        done = 0
        self.progress(label, 0, len(groups))
        with ThreadPoolExecutor(max_workers=self.opts.workers) as pool:
            for fut in as_completed([pool.submit(run, g) for g in groups]):
                group, outs = fut.result()
                for i, out in zip(group, outs):
                    if out is not None and out.strip():
                        results[i] = out
                        self.cache.put(keys[i], out)
                done += 1
                self.progress(label, done, len(groups))
        if self.cancel.is_set():
            raise Cancelled
        return results


def run(opts: Options, log: Log = print, progress: Progress = lambda *_: None,
        cancel: threading.Event | None = None) -> Path:
    cancel = cancel or threading.Event()
    out = opts.output_path
    translator = Translator(opts, log, progress, cancel)

    doc = pymupdf.open(opts.input)
    pages = parse_pages(opts.pages, doc.page_count)
    log(f"{opts.input.name}: {doc.page_count} pages, translating {len(pages)}")

    units: list[Unit] = []
    for pno in pages:
        units += extract_units(doc[pno])
    log(f"extracted {len(units)} text segments")

    outs = translator.translate([u.source for u in units], "body")
    for u, t in zip(units, outs):
        u.translation = t
    missing = sum(1 for t in outs if t is None)

    fonts = load_fonts(opts.serif_font, opts.sans_font, opts.sans_bold_font)
    by_page: dict[int, list[Unit]] = {}
    for u in units:
        by_page.setdefault(u.page, []).append(u)
    renderer = Renderer(fonts, opts.font_scale)
    for i, pno in enumerate(pages, 1):
        if cancel.is_set():
            raise Cancelled
        renderer.add_page(doc[pno], by_page.get(pno, []))
        progress("layout", i, len(pages))
    renderer.finish(doc)
    shrunk = [s for s in renderer.scales if s < 0.999]
    if shrunk:
        log(f"{len(shrunk)}/{len(renderer.scales)} segments shrunk to fit "
            f"(min scale {min(shrunk):.2f}, mean {sum(shrunk) / len(shrunk):.2f})")

    if opts.translate_toc:
        toc = doc.get_toc(simple=False)
        if toc:
            titles = translator.translate([t[1] for t in toc], "toc")
            for entry, title in zip(toc, titles):
                if title:
                    entry[1] = title
            doc.set_toc(toc)
    if opts.only_selected and opts.pages:
        doc.select(pages)

    log("saving...")
    doc.subset_fonts()
    # write next to the target and swap in, so an interrupted save never leaves a broken PDF
    partial = out.with_name(out.name + ".part")
    doc.save(partial, garbage=3, deflate=True)
    doc.close()
    partial.replace(out)
    log(f"wrote {out}" + (f" ({missing} segments left untranslated)" if missing else ""))
    return out
