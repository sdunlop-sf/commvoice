"""Simplest possible check: can my Gemini key use a model? Prints the model's reply.

    python test_model.py                       # tries the default model
    python test_model.py gemini-3.5-flash      # tries another model
    python test_model.py --list                # lists the Gemini models your key can use
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True, encoding="utf-8-sig")

import llm  # noqa: E402


def main() -> int:
    if not llm.has_key():
        print("No GEMINI_API_KEY found. Put it in a file named .env next to this script:\n  GEMINI_API_KEY=your-key")
        return 1
    from google.genai import errors

    client = llm.GeminiBackend().client()
    args = sys.argv[1:]
    try:
        if args and args[0] == "--list":
            names = sorted(m.name.replace("models/", "") for m in client.models.list() if "gemini" in m.name)
            print("Gemini models your key can see:\n  " + "\n  ".join(names))
            return 0
        model = args[0] if args else llm.GeminiBackend().model_main
        reply = client.models.generate_content(model=model, contents="Say hello in five words.")
        print(f"Model: {model}\nReply: {reply.text}")
        return 0
    except errors.APIError as exc:
        print("Not working: " + llm.GeminiBackend._explain(exc, args[0] if args else llm.GeminiBackend().model_main))
    except Exception as exc:  # network problems and the like
        print(f"Could not reach Google: {exc}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
