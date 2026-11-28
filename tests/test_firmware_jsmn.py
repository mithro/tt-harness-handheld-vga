# SPDX-License-Identifier: Apache-2.0
"""`firmware/third_party/jsmn.h` is upstream jsmn with one recorded change: the token is
8 bytes (`firmware/third_party/README.md`)."""

from __future__ import annotations

import hashlib
from pathlib import Path

JSMN = Path(__file__).resolve().parent.parent / "firmware" / "third_party" / "jsmn.h"
UPSTREAM_SHA256 = "c04533e9181e1e33baceb0f55ac449b05145bb936e8c68cc77dfe0d8277514fb"
UPSTREAM_TOKEN = """typedef struct jsmntok {
  jsmntype_t type;
  int start;
  int end;
  int size;
#ifdef JSMN_PARENT_LINKS
  int parent;
#endif
} jsmntok_t;
"""
BEGIN = "/* tt-harness: 8-byte token (third_party/README.md). */\n"
END = "} jsmntok_t;\n"


def test_jsmn_is_upstream_but_for_the_token_struct() -> None:
    text = JSMN.read_text()
    start = text.index(BEGIN)
    end = text.index(END, start) + len(END)
    restored = text[:start] + UPSTREAM_TOKEN + text[end:]
    assert hashlib.sha256(restored.encode()).hexdigest() == UPSTREAM_SHA256
    assert text.count("tt-harness") == 1
