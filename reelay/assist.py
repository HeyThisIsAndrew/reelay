"""Local, token-free writing help: spelling and grammar checks, and caption
rewriting with a small model running on this computer. Nothing is sent to a
paid API.

Checks (best available, automatically):
  1. LanguageTool, fully offline, when `language_tool_python` and Java are
     installed (first use downloads LanguageTool, about 250 MB).
  2. Built in: spelling (pyspellchecker) plus a few mechanical checks
     (repeated words, double spaces, a space before punctuation). Hashtags,
     @handles, links, numbers, emoji and capitalized names are never flagged,
     and lowercase captions are treated as a style choice, not a mistake.

Rewriting (optional), with a local model:
  - Ollama (free app from ollama.com, e.g. `ollama pull llama3.2:3b`), over its
    local HTTP API at 127.0.0.1:11434.
  - llama.cpp, via `llama-cpp-python` and a .gguf model file.
Rewrites are suggestions: they're never saved by themselves and never
shortened to fit (the length is shown against the platform's limit).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field

import requests

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.2:3b"
SETTINGS = {"engine": "assist.engine", "ollama_url": "assist.ollama_url", "model": "assist.model",
            "llamacpp_path": "assist.llamacpp_path", "dictionary": "assist.dictionary"}

STYLES = {
    "fix": "Fix spelling and grammar only. Keep the wording, tone, emoji, hashtags and lowercase style.",
    "shorter": "Make it shorter and tighter. Keep the meaning, emoji and hashtags.",
    "hook": "Rewrite the first line as a stronger hook that makes people stop scrolling. Keep the rest.",
    "friendly": "Make it warmer and more conversational.",
    "professional": "Make it polished and professional.",
    "cta": "Keep it, and end with a short, natural call to action.",
}
STYLE_LABELS = {"fix": "Fix grammar only", "shorter": "Shorter", "hook": "Stronger hook", "friendly": "Friendlier",
                "professional": "More professional", "cta": "Add a call to action"}


class AssistError(Exception):
    """Plain-language reason the helper can't do something right now."""


# ====================================================================== checks
@dataclass
class Issue:
    offset: int
    length: int
    message: str
    replacements: list[str] = field(default_factory=list)
    kind: str = "spelling"          # spelling | grammar | style
    word: str = ""

    def apply(self, text: str, replacement: str) -> str:
        return text[:self.offset] + replacement + text[self.offset + self.length:]


_SKIP = re.compile(r"(#\w+|@[\w.]+|https?://\S+|www\.\S+|\S+@\S+\.\w+)")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")


def _masked(text: str) -> str:
    """Blank out hashtags, handles and links (same length, so offsets still match)."""
    return _SKIP.sub(lambda m: " " * len(m.group(0)), text)


def builtin_check(text: str, dictionary: set[str] | None = None) -> list[Issue]:
    issues: list[Issue] = []
    masked = _masked(text)
    known = {w.lower() for w in (dictionary or set())}
    try:
        from spellchecker import SpellChecker
        spell = SpellChecker()
    except ImportError:
        spell = None
    words = [(m.start(), m.group(0)) for m in _WORD.finditer(masked)]
    if spell is not None:
        candidates = {w.lower().replace("’", "'") for _o, w in words
                      if len(w) > 2 and w[0].islower() and w.lower() not in known}
        unknown = spell.unknown(candidates)
        for offset, w in words:
            key = w.lower().replace("’", "'")
            if key in unknown and w[0].islower():
                best = spell.correction(key)
                options = [best] if best and best != key else []
                options += sorted((spell.candidates(key) or set()) - {key, best})[:3]
                issues.append(Issue(offset, len(w), f"“{w}” may be misspelled.", options[:4], "spelling", w))
    for m in re.finditer(r"\b(\w+)\s+\1\b", masked, flags=re.IGNORECASE):
        if m.group(1).isalpha():
            issues.append(Issue(m.start(), len(m.group(0)), f"“{m.group(1)}” is repeated.", [m.group(1)], "grammar"))
    skipped = [m.span() for m in _SKIP.finditer(text)]
    for m in re.finditer(r"(?<=\S)  +(?=\S)", text):
        issues.append(Issue(m.start(), len(m.group(0)), "Two spaces in a row.", [" "], "style"))
    for m in re.finditer(r"(?<=\w) +([,.!?;:])(?=\s|$)", text):
        issues.append(Issue(m.start(), len(m.group(0)), "A space before punctuation.", [m.group(1)], "style"))
    issues = [i for i in issues if not any(a < i.offset + i.length and i.offset < b for a, b in skipped)]
    return sorted(issues, key=lambda i: i.offset)


def languagetool_available() -> tuple[bool, str]:
    try:
        import language_tool_python  # noqa: F401
    except ImportError:
        return False, "Full grammar checking needs the free LanguageTool add-on: install Java, then " \
                      "`pip install language_tool_python`."
    if not shutil.which("java"):
        return False, "Full grammar checking needs Java (LanguageTool runs on it). Install Java to turn it on."
    return True, ""


_TOOL = None


def languagetool_check(text: str, language: str = "en-US") -> list[Issue]:
    global _TOOL
    import language_tool_python
    if _TOOL is None:
        _TOOL = language_tool_python.LanguageTool(language)   # local server; first run downloads LanguageTool
    masked = _masked(text)
    issues = []
    for m in _TOOL.check(masked):
        if m.ruleId.startswith(("UPPERCASE_SENTENCE_START", "EN_QUOTES")):
            continue      # lowercase captions and curly quotes are style choices
        issues.append(Issue(m.offset, m.errorLength, m.message, list(m.replacements[:4]),
                            "spelling" if "MORFOLOGIK" in m.ruleId else "grammar",
                            text[m.offset:m.offset + m.errorLength]))
    return issues


def check(text: str, dictionary: set[str] | None = None) -> tuple[list[Issue], str]:
    """(issues, engine name). Uses LanguageTool when it's installed, else the built-in check."""
    ok, _why = languagetool_available()
    if ok:
        try:
            known = {w.lower() for w in (dictionary or set())}
            return [i for i in languagetool_check(text) if i.word.lower() not in known], "LanguageTool (offline)"
        except Exception:  # noqa: BLE001 — fall back rather than fail
            pass
    return builtin_check(text, dictionary), "Built-in spelling check"


# ====================================================================== rewriting
def build_prompt(text: str, style: str, platform: str = "", limit: int | None = None) -> str:
    rules = STYLES.get(style, STYLES["fix"])
    where = f" for {platform}" if platform else ""
    length = f" It must be at most {limit} characters." if limit else ""
    return (f"You edit short social media captions{where}. {rules}{length} "
            "Don't add hashtags that aren't there, don't explain, and don't use quotation marks around it. "
            "Reply with only the new caption.\n\nCaption:\n" + text.strip())


def clean_reply(reply: str) -> str:
    reply = (reply or "").strip()
    reply = re.sub(r"^(here(?:'s| is)[^:\n]*:|new caption:|caption:)\s*", "", reply, flags=re.IGNORECASE).strip()
    if len(reply) > 1 and reply[0] == reply[-1] and reply[0] in "\"'“”":
        reply = reply[1:-1].strip()
    return reply.strip("“”").strip()


class OllamaRewriter:
    name = "Ollama"

    def __init__(self, base_url: str = DEFAULT_OLLAMA_URL, model: str = DEFAULT_MODEL, session=None):
        self.base_url, self.model, self.http = base_url.rstrip("/"), model, session or requests

    def models(self) -> list[str]:
        try:
            resp = self.http.get(f"{self.base_url}/api/tags", timeout=5)
        except requests.RequestException:
            raise AssistError("Ollama isn't running. Install it free from ollama.com and open it, then try again.") \
                from None
        return [m.get("name", "") for m in (resp.json() or {}).get("models", []) if m.get("name")]

    def status(self) -> tuple[bool, str]:
        try:
            names = self.models()
        except AssistError as exc:
            return False, str(exc)
        if self.model not in names and f"{self.model}:latest" not in names:
            return False, (f"Ollama is running, but the model “{self.model}” isn't downloaded. Run "
                           f"`ollama pull {self.model}` (about 2 GB for a 3B model).")
        return True, f"Ollama is ready with {self.model}."

    def complete(self, prompt: str) -> str:
        try:
            resp = self.http.post(f"{self.base_url}/api/generate", json={
                "model": self.model, "prompt": prompt, "stream": False, "options": {"temperature": 0.6}}, timeout=180)
        except requests.RequestException:
            raise AssistError("Couldn't reach Ollama. Is it open?") from None
        try:
            data = resp.json() or {}
        except ValueError:
            data = {}
        if resp.status_code != 200:
            raise AssistError(f"Ollama said: {data.get('error') or resp.status_code}")
        return data.get("response", "")


class LlamaCppRewriter:
    name = "llama.cpp"
    _cache: dict = {}

    def __init__(self, model_path: str):
        self.model_path = model_path

    def status(self) -> tuple[bool, str]:
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            return False, "llama.cpp needs the `llama-cpp-python` package (pip install llama-cpp-python)."
        from pathlib import Path
        if not self.model_path or not Path(self.model_path).is_file():
            return False, "Choose a .gguf model file (for example a 1B or 3B instruct model)."
        return True, "llama.cpp is ready."

    def complete(self, prompt: str) -> str:
        ok, why = self.status()
        if not ok:
            raise AssistError(why)
        from llama_cpp import Llama
        llm = self._cache.get(self.model_path)
        if llm is None:
            llm = self._cache[self.model_path] = Llama(model_path=self.model_path, n_ctx=2048, verbose=False)
        out = llm.create_chat_completion(messages=[{"role": "user", "content": prompt}], temperature=0.6,
                                         max_tokens=600)
        return out["choices"][0]["message"]["content"]


def rewriter_from(store, session=None):
    """The rewriter chosen in Settings, or None when rewriting is off."""
    engine = store.get_setting(SETTINGS["engine"], "ollama")
    if engine == "llamacpp":
        return LlamaCppRewriter(store.get_setting(SETTINGS["llamacpp_path"], ""))
    if engine == "ollama":
        return OllamaRewriter(store.get_setting(SETTINGS["ollama_url"], DEFAULT_OLLAMA_URL) or DEFAULT_OLLAMA_URL,
                              store.get_setting(SETTINGS["model"], DEFAULT_MODEL) or DEFAULT_MODEL, session)
    return None


def rewrite(rewriter, text: str, style: str, platform: str = "", limit: int | None = None) -> str:
    if rewriter is None:
        raise AssistError("Rewriting is turned off. Turn it on in Settings → Local AI helper.")
    if not text.strip():
        raise AssistError("Write something first; the helper rewrites what's there.")
    reply = clean_reply(rewriter.complete(build_prompt(text, style, platform, limit)))
    if not reply:
        raise AssistError("The model didn't return anything. Try again or pick a different model.")
    return reply


def dictionary(store) -> set[str]:
    try:
        return set(json.loads(store.get_setting(SETTINGS["dictionary"], "[]") or "[]"))
    except ValueError:
        return set()


def add_to_dictionary(store, word: str) -> None:
    words = dictionary(store) | {word.strip().lower()}
    store.set_setting(SETTINGS["dictionary"], json.dumps(sorted(words)))
