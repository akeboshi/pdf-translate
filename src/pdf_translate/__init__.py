import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pymupdf

from .extract import Unit, extract_units
from .render import Renderer, load_fonts
from .translate import AnthropicBackend, ClaudeCliBackend, DummyBackend, TranslationCache, batches


def parse_pages(spec: str | None, count: int) -> list[int]:
    if not spec:
        return list(range(count))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            start = int(a) if a else 1
            end = int(b) if b else count
            pages.update(range(start - 1, min(end, count)))
        else:
            pages.add(int(part) - 1)
    return sorted(p for p in pages if 0 <= p < count)


def make_backend(args):
    glossary = Path(args.glossary).read_text(encoding="utf-8") if args.glossary else None
    if args.backend == "anthropic":
        return AnthropicBackend(args.model, args.effort, glossary, fallback=not args.no_fallback)
    if args.backend == "claude-cli":
        return ClaudeCliBackend(args.model if args.model_explicit else None, glossary)
    return DummyBackend()


def translate_all(texts: list[str], backend, cache: TranslationCache, salt: str, workers: int,
                  max_chars: int, label: str) -> list[str | None]:
    keys = [TranslationCache.key(t, salt) for t in texts]
    results: list[str | None] = [cache.get(k) for k in keys]
    pending = [i for i, r in enumerate(results) if r is None]
    if not pending:
        return results

    groups = [[pending[j] for j in b] for b in batches([texts[i] for i in pending], max_chars, 80)]
    print(f"[{label}] {len(pending)} segments to translate in {len(groups)} requests "
          f"({len(texts) - len(pending)} cached)", file=sys.stderr)

    def run(group: list[int]):
        for attempt in range(3):
            try:
                return group, backend([texts[i] for i in group])
            except Exception as e:  # noqa: BLE001 - keep going; failed segments stay in English
                print(f"  request failed (attempt {attempt + 1}/3): {e}", file=sys.stderr)
                time.sleep(2 ** attempt)
        return group, [None] * len(group)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(run, g) for g in groups]):
            group, outs = fut.result()
            for i, out in zip(group, outs):
                if out is not None and out.strip():
                    results[i] = out
                    cache.put(keys[i], out)
            done += 1
            print(f"  [{label}] {done}/{len(groups)} requests done", file=sys.stderr)
    return results


def translate_toc(doc: pymupdf.Document, backend, cache, salt, args) -> None:
    toc = doc.get_toc(simple=False)
    if not toc:
        return
    titles = [t[1] for t in toc]
    outs = translate_all(titles, backend, cache, salt, args.workers, args.batch_chars, "toc")
    for entry, out in zip(toc, outs):
        if out:
            entry[1] = out
    doc.set_toc(toc)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="pdf-translate",
        description="Translate an English PDF into Japanese while keeping the original layout, figures and code.",
    )
    ap.add_argument("input", help="input PDF")
    ap.add_argument("-o", "--output", help="output PDF (default: <input>.ja.pdf)")
    ap.add_argument("--pages", help="pages to translate, 1-based (e.g. '1-20,25,30-')")
    ap.add_argument("--only-selected", action="store_true", help="drop pages not selected by --pages from the output")
    ap.add_argument("--backend", choices=["anthropic", "claude-cli", "dummy"], default="anthropic",
                    help="anthropic: Claude API (ANTHROPIC_API_KEY); claude-cli: local `claude -p`; "
                         "dummy: filler text for layout checks")
    ap.add_argument("--model", default=None, help="model id (default: claude-opus-5-5)")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--no-fallback", action="store_true", help="disable server-side refusal fallback (anthropic backend)")
    ap.add_argument("--glossary", help="text file with term translations appended to the prompt")
    ap.add_argument("--workers", type=int, default=4, help="concurrent translation requests")
    ap.add_argument("--batch-chars", type=int, default=6000, help="max source characters per request")
    ap.add_argument("--cache", help="translation cache file (default: <output>.cache.jsonl)")
    ap.add_argument("--no-toc", action="store_true", help="do not translate bookmarks")
    ap.add_argument("--font-scale", type=float, default=0.85,
                    help="base size of Japanese text relative to the original (text is shrunk further only if it overflows)")
    ap.add_argument("--serif-font", help="Japanese font file used for serif body text")
    ap.add_argument("--sans-font", help="Japanese font file used for sans-serif text")
    ap.add_argument("--sans-bold-font", help="Japanese font file used for bold text")
    args = ap.parse_args(argv)
    args.model_explicit = args.model is not None
    args.model = args.model or "claude-opus-5-5"

    src = Path(args.input)
    out = Path(args.output) if args.output else src.with_suffix(".ja.pdf")
    cache = TranslationCache(Path(args.cache) if args.cache else out.with_suffix(".cache.jsonl"))
    salt = f"{args.backend}:{args.model}:ja"

    doc = pymupdf.open(src)
    pages = parse_pages(args.pages, doc.page_count)
    print(f"{src}: {doc.page_count} pages, translating {len(pages)}", file=sys.stderr)

    units: list[Unit] = []
    for pno in pages:
        units += extract_units(doc[pno])
    print(f"extracted {len(units)} text segments", file=sys.stderr)

    backend = make_backend(args)
    outs = translate_all([u.source for u in units], backend, cache, salt, args.workers, args.batch_chars, "body")
    for u, t in zip(units, outs):
        u.translation = t
    missing = sum(1 for t in outs if t is None)

    fonts = load_fonts(args.serif_font, args.sans_font, args.sans_bold_font)
    by_page: dict[int, list[Unit]] = {}
    for u in units:
        by_page.setdefault(u.page, []).append(u)
    renderer = Renderer(fonts, args.font_scale)
    for i, pno in enumerate(pages, 1):
        renderer.add_page(doc[pno], by_page.get(pno, []))
        if i % 50 == 0:
            print(f"laid out {i}/{len(pages)} pages", file=sys.stderr)
    renderer.finish(doc)
    shrunk = [s for s in renderer.scales if s < 0.999]
    if shrunk:
        print(f"{len(shrunk)}/{len(renderer.scales)} segments shrunk to fit "
              f"(min scale {min(shrunk):.2f}, mean {sum(shrunk) / len(shrunk):.2f})", file=sys.stderr)

    if not args.no_toc:
        translate_toc(doc, backend, cache, salt, args)
    if args.only_selected and args.pages:
        doc.select(pages)

    doc.subset_fonts()
    doc.save(out, garbage=3, deflate=True)
    print(f"wrote {out}" + (f" ({missing} segments left untranslated)" if missing else ""), file=sys.stderr)
