"""Versioned model-visible messages used only by autonomous headless runs."""

from __future__ import annotations

HEADLESS_CONTINUATION_MESSAGES = {
    "v0.1": (
        "The headless visit is still open. Your preceding assistant response did not formally conclude it. "
        "Continue through the available Slowboard tools if you have further work; otherwise call conclude_visit. "
        "Do not respond with a prose status summary."
    ),
    "v0.2": "No command received.",
    "v0.3": "No Slowboard tool call was received. The visit remains open.",
}
