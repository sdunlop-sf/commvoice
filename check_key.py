"""Check that your API key works before you start the app.

    python check_key.py

Makes one tiny request (a fraction of a cent) and tells you in plain words what is wrong, if anything.
"""
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True, encoding="utf-8-sig")  # same way the app reads it

import llm  # noqa: E402

TOOL = {
    "name": "report_ok",
    "description": "Report that the connection works.",
    "input_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
}


def where_is_the_key() -> str:
    """Say what is wrong with the key file, without ever printing the key."""
    var = "GEMINI_API_KEY"
    if (ROOT / ".env.txt").exists() and not (ROOT / ".env").exists():
        return "Found a file named .env.txt. Rename it to exactly .env (no .txt)."
    if not (ROOT / ".env").exists():
        return f"There is no file named .env in this folder:\n  {ROOT}\nCreate it there with one line:  {var}=your-key"
    return f"Found .env in {ROOT}, but it has no {var} line with a value.\nIt should be one line, no quotes:  {var}=your-key"


def main() -> int:
    label = llm.provider_label()
    backend = llm.get_backend()
    print(f"Provider: {label}   Model: {backend.model_main}")
    if not llm.has_key():
        print("No API key found.\n" + where_is_the_key())
        return 1
    try:
        out = llm.call_tool("main", ["You are a connection test."], "Call report_ok with ok=true.", TOOL, max_tokens=50)
    except llm.LLMUnavailable as exc:
        print(f"Not working: {exc}")
        return 2
    print("Working. The key is valid and the model answered." if out.get("ok") else f"Connected, but the reply was unexpected: {out}")
    return 0 if out.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
