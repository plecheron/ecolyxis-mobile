"""Utility functions for lm-eval-harness custom tasks.

Handles Qwen3 thinking-token quirks:
- extract_letter: Pulls the answer letter from model output that may contain
  thinking/reasoning tokens before the actual answer.
- extract_letter_answer: Wraps extract_letter for process_results.
"""

import re


def extract_letter(response, choices="ABCD"):
    """Extract a multiple-choice letter from model response.

    Handles responses that may have thinking tokens before the answer.
    """
    if not response or not isinstance(response, str):
        response = str(response) if response else ""

    # Try to find ANSWER: marker first
    marker_match = re.search(r"(?:ANSWER:|answer:)\s*([A-D])", response, re.IGNORECASE)
    if marker_match:
        return marker_match.group(1).upper()

    # Look for a standalone letter at the end of the response
    lines = response.strip().split("\n")
    for line in reversed(lines):
        line = line.strip().rstrip(".* ")
        if re.match(r"^[A-D]$", line.upper()) and line.upper() in choices:
            return line.upper()

    # Fallback: find any standalone A-D letter
    matches = re.findall(r"\b([A-D])\b", response.upper())
    if matches:
        return matches[-1]

    return ""


def extract_letter_answer(doc, results):
    """Process results for multiple-choice tasks."""
    # results is a list of strings from model generation
    answer = results[0] if results else ""
    letter = extract_letter(answer)
    return {"exact_match": letter}
