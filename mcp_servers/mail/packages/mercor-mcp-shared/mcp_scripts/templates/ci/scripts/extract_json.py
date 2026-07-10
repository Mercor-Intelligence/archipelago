#!/usr/bin/env python3
"""Extract the first valid JSON object from stdin."""

import json
import sys

text = sys.stdin.read()

# Use JSONDecoder to find and extract the first valid JSON object
decoder = json.JSONDecoder()
idx = text.find("{")

while idx != -1 and idx < len(text):
    try:
        obj, end_idx = decoder.raw_decode(text, idx)
        # Successfully decoded a JSON object
        print(json.dumps(obj))
        break
    except json.JSONDecodeError:
        # Move to next potential JSON start
        idx = text.find("{", idx + 1)
