import argparse
import sys
from pathlib import Path

from .pipeline import BACKENDS, Cancelled, Options, run


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _progress(stage: str, done: int, total: int) -> None:
    if stage == "layout" and done % 50 and done != total:
        return
    print(f"  [{stage}] {done}/{total}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pdf-translate",
        description="Translate an English PDF into Japanese while keeping the original layout, figures and code. "
                    "Run without arguments to open the GUI.",
    )
    ap.add_argument("input", help="input PDF")
    ap.add_argument("-o", "--output", help="output PDF (default: <input>.ja.pdf)")
    ap.add_argument("--pages", help="pages to translate, 1-based (e.g. '1-20,25,30-')")
    ap.add_argument("--only-selected", action="store_true", help="drop pages not selected by --pages from the output")
    ap.add_argument("--backend", choices=BACKENDS, default="anthropic",
                    help="anthropic: Claude API (ANTHROPIC_API_KEY); claude-cli: local `claude -p`; "
                         "agy: Antigravity CLI; dummy: filler text for layout checks")
    ap.add_argument("--model", default=None, help="model id (anthropic default: claude-opus-5-5; CLIs: their default)")
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
    return ap


def _ensure_agy() -> None:
    from .agy import install_agy, install_command
    from .translate import find_cli

    if find_cli("agy"):
        return
    print("Antigravity CLI (agy) is not installed.", file=sys.stderr)
    print(f"Install command: {' '.join(install_command())}", file=sys.stderr)
    if not sys.stdin.isatty() or input("Install it now? [y/N] ").strip().lower() != "y":
        sys.exit(1)
    install_agy(_log)
    print("Installed. Run `agy` once in a terminal to sign in, then rerun this command.", file=sys.stderr)
    sys.exit(0)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .gui import main as gui_main

        gui_main()
        return

    args = build_parser().parse_args(argv)
    if args.backend == "agy":
        _ensure_agy()
    opts = Options(
        input=Path(args.input),
        output=Path(args.output) if args.output else None,
        pages=args.pages,
        only_selected=args.only_selected,
        backend=args.backend,
        model=args.model,
        effort=args.effort,
        fallback=not args.no_fallback,
        glossary=Path(args.glossary) if args.glossary else None,
        workers=args.workers,
        batch_chars=args.batch_chars,
        cache=Path(args.cache) if args.cache else None,
        translate_toc=not args.no_toc,
        font_scale=args.font_scale,
        serif_font=args.serif_font,
        sans_font=args.sans_font,
        sans_bold_font=args.sans_bold_font,
    )
    try:
        run(opts, _log, _progress)
    except Cancelled:
        sys.exit(130)
