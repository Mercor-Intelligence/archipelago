# flake8: noqa
#!/usr/bin/env python3
"""GDM Docker World final answer helper.

Standalone script packaged into the GDM export ZIP. Runs inside the
single-container GDM environment at grading time.

Extracts the last assistant message from the conversation JSON.

Env vars:
    CONVERSATION_PATH  - path to conversation JSON file
    CACHE_DIR          - helper cache directory (default: /app/output/.helpers_cache)
"""

import json
import os
import sys
from pathlib import Path


CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/app/output/.helpers_cache"))
CACHE_FILE = "final_answer.json"


def main() -> int:
    cache_path = CACHE_DIR / CACHE_FILE
    if cache_path.exists():
        print(f"final_answer: cache hit at {cache_path}", file=sys.stderr)
        return 0

    conversation_path = os.environ.get("CONVERSATION_PATH", "")
    if not conversation_path or not Path(conversation_path).exists():
        print(
            f"final_answer: CONVERSATION_PATH not found: {conversation_path}",
            file=sys.stderr,
        )
        output = {"content": "", "role": "assistant"}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(output), encoding="utf-8")
        return 0

    try:
        conversation = json.loads(Path(conversation_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"final_answer: failed to read conversation: {e}", file=sys.stderr)
        output = {"content": "", "role": "assistant"}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(output), encoding="utf-8")
        return 0

    messages = (
        conversation
        if isinstance(conversation, list)
        else conversation.get("messages", [])
    )

    content = ""
    role = "assistant"
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = str(msg.get("content", ""))
            role = "assistant"
            break

    output = {"content": content, "role": role}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"final_answer: wrote {cache_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
