# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import re
from typing import List


def extract_sign_in_codes(text: str) -> List[str]:
    """Extract 5-7 digit Telegram codes with optional interleaved hyphens."""
    if not text:
        return []
    codes = []
    for match in re.finditer(r"(?<![0-9])[0-9][0-9-]*(?![0-9])", text):
        candidate = match.group(0)
        before = text[:match.start()].rstrip()
        after = text[match.end():].lstrip()
        if before.endswith("(") and after.startswith(")"):
            continue
        code = candidate.replace("-", "")
        if 5 <= len(code) <= 7:
            codes.append(code)
    return codes
