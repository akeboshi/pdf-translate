import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SYSTEM_PROMPT = """You are a professional technical translator. Translate English text segments extracted from a technical book PDF into natural, fluent Japanese suitable for a published Japanese edition.

Rules:
- Use the である調 (plain, written style) consistently.
- Keep the inline HTML tags (<i>, <b>, <code>, <sup>, <span style=...>) and wrap the corresponding Japanese words with them. Never invent new tags.
- <br> marks a deliberate line break (e.g. on a cover). Keep the same number of <br> and split the Japanese at the corresponding points.
- Never translate the contents of <code> tags, SQL keywords, commands, identifiers, file names, URLs, numbers, or units.
- Keep product and technology names in their original spelling (MySQL, InnoDB, Linux, Amazon Web Services, etc.).
- Headings, captions ("Figure 1-1." -> "図1-1", "Table 2-1." -> "表2-1", "Example 3-2." -> "例3-2"), and running headers ("Chapter 2:" -> "第2章") should be translated concisely.
- A segment may be a fragment (a table cell, label, or a sentence broken across pages). Translate it as-is without completing or merging it with other segments.
- If a segment should not be translated (e.g. a proper noun alone, code), return it unchanged.
- Return exactly one translation for every input id."""

SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


def build_system_prompt(glossary: str | None) -> str:
    if not glossary:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + "\n\nGlossary (follow these term translations):\n" + glossary


def _payload(texts: list[str]) -> str:
    segments = [{"id": i, "text": t} for i, t in enumerate(texts)]
    return json.dumps({"segments": segments}, ensure_ascii=False)


def _parse(raw: str, n: int) -> list[str | None]:
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0) if m else raw)
    out: list[str | None] = [None] * n
    for item in data["translations"]:
        if 0 <= item["id"] < n:
            out[item["id"]] = item["text"]
    return out


class AnthropicBackend:
    def __init__(self, model: str, effort: str, glossary: str | None, fallback: bool):
        import anthropic

        self.client = anthropic.Anthropic(max_retries=5)
        self.model = model
        self.effort = effort
        self.system = build_system_prompt(glossary)
        self.fallback = fallback

    def __call__(self, texts: list[str]) -> list[str | None]:
        kwargs = {}
        if self.fallback:
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=self.system,
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": _payload(texts)}],
            **kwargs,
        ) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            return [None] * len(texts)
        text = next(b.text for b in message.content if b.type == "text")
        return _parse(text, len(texts))


# keep GUI builds from flashing a console window for every request
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

KNOWN_LOCATIONS = {
    "agy": [
        Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe",
        Path.home() / ".local" / "bin" / "agy",
    ],
    "claude": [
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".local" / "bin" / "claude.exe",
    ],
}


def find_cli(name: str) -> str | None:
    """PATH first, then the installer's default location (PATH changes only reach new processes)."""
    found = shutil.which(name)
    if found:
        return found
    for p in KNOWN_LOCATIONS.get(name, []):
        if p.is_file():
            return str(p)
    return None


_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def kill_running_cli() -> None:
    """Stop every CLI request in flight so cancelling or quitting does not leave them running."""
    with _children_lock:
        procs = list(_children)
    for proc in procs:
        _kill_tree(proc)


def _run_cli(cmd: list[str], stdin: str, timeout: int, cwd: str | None = None) -> subprocess.CompletedProcess:
    # own process group so the CLI and anything it spawns can be killed together
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", cwd=cwd, creationflags=NO_WINDOW,
        start_new_session=sys.platform != "win32",
    )
    with _children_lock:
        _children.add(proc)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        raise
    finally:
        with _children_lock:
            _children.discard(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


class ClaudeCliBackend:
    """Uses the local `claude` CLI (Claude Code subscription) instead of an API key."""

    def __init__(self, model: str | None, glossary: str | None):
        self.exe = find_cli("claude")
        if not self.exe:
            raise RuntimeError("claude CLI not found. Install Claude Code first.")
        self.model = model
        self.workdir = tempfile.mkdtemp(prefix="pdf-translate-claude-")
        # passed as a file: the prompt plus a glossary can exceed the Windows command-line limit
        self.system_file = Path(self.workdir) / "system.txt"
        self.system_file.write_text(build_system_prompt(glossary), encoding="utf-8")

    def __call__(self, texts: list[str]) -> list[str | None]:
        cmd = [
            self.exe, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(SCHEMA),
            "--system-prompt-file", str(self.system_file),
            "--tools", "",
            "--no-session-persistence",
            "--strict-mcp-config",
        ]
        if self.model:
            cmd += ["--model", self.model]
        proc = _run_cli(cmd, _payload(texts), timeout=1800, cwd=self.workdir)
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {proc.stderr.strip() or proc.stdout.strip()}")
        result = json.loads(proc.stdout)
        structured = result.get("structured_output")
        if structured:
            return _parse(json.dumps(structured), len(texts))
        return _parse(result["result"], len(texts))


class AgyBackend:
    """Uses the Antigravity CLI (`agy`) in headless mode."""

    def __init__(self, model: str | None, glossary: str | None, timeout_minutes: int = 30):
        self.exe = find_cli("agy")
        if not self.exe:
            raise RuntimeError("Antigravity CLI (agy) not found.")
        self.model = model
        self.timeout_minutes = timeout_minutes
        self.instructions = (
            build_system_prompt(glossary)
            + "\n\nTranslate every segment in the JSON below. Do not use any tools; reply only with the result.\n\n"
        )
        # an empty working directory keeps the agent away from unrelated files
        self.workdir = tempfile.mkdtemp(prefix="pdf-translate-agy-")
        self.schema_file = Path(self.workdir) / "schema.json"
        self.schema_file.write_text(json.dumps(SCHEMA), encoding="utf-8")

    def __call__(self, texts: list[str]) -> list[str | None]:
        # the prompt goes through stdin (stream-json) to stay clear of the Windows command-line limit
        message = {"event": "user", "message": {"content": self.instructions + _payload(texts)}}
        cmd = [
            self.exe,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--json-schema", str(self.schema_file),
            "--disable-slash-commands",
            "--print-timeout", f"{self.timeout_minutes}m",
        ]
        if self.model:
            cmd += ["--model", self.model]
        proc = _run_cli(cmd, json.dumps(message, ensure_ascii=False) + "\n",
                        timeout=self.timeout_minutes * 60 + 60, cwd=self.workdir)
        result = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "result":
                result = event.get("result") or {}
        if result is None:
            raise RuntimeError(f"agy returned no result (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")
        if result.get("status") != "SUCCESS":
            raise RuntimeError(f"agy {result.get('status')}: {result.get('error') or ''}")
        structured = result.get("structured_output")
        if structured:
            return _parse(json.dumps(structured), len(texts))
        if not (result.get("response") or "").strip():
            # known agy issue: SUCCESS with an empty response; treated as a failure so the caller retries
            raise RuntimeError("agy returned an empty response")
        return _parse(result["response"], len(texts))


class DummyBackend:
    """Layout check without an LLM: replaces text with Japanese filler of similar length."""

    def __call__(self, texts: list[str]) -> list[str | None]:
        filler = "これはレイアウト確認用のダミー翻訳文である。"
        out = []
        for t in texts:
            plain = re.sub(r"<[^>]+>", "", t)
            n = max(2, int(len(plain) * 0.55))
            out.append((filler * (n // len(filler) + 1))[:n])
        return out


class TranslationCache:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.data[rec["k"]] = rec["v"]

    @staticmethod
    def key(text: str, salt: str) -> str:
        return hashlib.sha256((salt + "\0" + text).encode()).hexdigest()

    def get(self, k: str) -> str | None:
        return self.data.get(k)

    def put(self, k: str, v: str) -> None:
        with self.lock:
            self.data[k] = v
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"k": k, "v": v}, ensure_ascii=False) + "\n")


def batches(texts: list[str], max_chars: int, max_items: int):
    batch: list[int] = []
    size = 0
    for i, t in enumerate(texts):
        if batch and (size + len(t) > max_chars or len(batch) >= max_items):
            yield batch
            batch, size = [], 0
        batch.append(i)
        size += len(t)
    if batch:
        yield batch
