"""
Batch task generation — CSV import to queue multiple video tasks at once.

CSV format (header row required):
    video_subject,video_source,video_aspect,voice_name,video_language,paragraph_number

Only ``video_subject`` is required; all other columns have sensible defaults.

Also supports JSON array input for programmatic clients.
"""

import csv
from typing import Any

from loguru import logger

from app.models.schema import TaskVideoRequest


# Default CSV column mapping
CSV_COLUMNS = [
    "video_subject",
    "video_source",
    "video_aspect",
    "voice_name",
    "video_language",
    "paragraph_number",
    "video_count",
]


def parse_csv(content: str) -> list[dict[str, Any]]:
    """
    Parse CSV text into a list of parameter dicts.

    First non-comment line MUST be the header.  Comment lines start with ``#``.
    """
    # Strip BOM and comment lines
    lines = [
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        raise ValueError("CSV is empty")

    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        raise ValueError("No header row found in CSV")

    # Normalize: strip whitespace from all values
    rows: list[dict[str, Any]] = []
    for row in reader:
        cleaned: dict[str, Any] = {}
        for k, v in row.items():
            key = k.strip().lower()
            val = v.strip()
            # Convert numeric fields
            if key in ("paragraph_number", "video_count"):
                try:
                    cleaned[key] = int(val) if val else None
                except ValueError:
                    cleaned[key] = None
            elif key == "video_subject":
                cleaned[key] = val
            elif val:
                cleaned[key] = val
        # Only include rows with a subject
        if cleaned.get("video_subject"):
            rows.append(cleaned)
        else:
            logger.warning(f"Skipping CSV row without video_subject: {row}")

    return rows


def rows_to_requests(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], TaskVideoRequest | None, str | None]]:
    """
    Convert parsed rows into TaskVideoRequest objects.

    Returns list of (raw_row, TaskVideoRequest, error_message).
    For each row, exactly one of TaskVideoRequest or error_message is non-None.
    """
    results: list[tuple[dict[str, Any], TaskVideoRequest | None, str | None]] = []

    for i, row in enumerate(rows):
        # Build kwargs with defaults
        kwargs: dict[str, Any] = {
            "video_subject": row.get("video_subject", ""),
            "video_source": row.get("video_source") or "pexels",
            "video_aspect": row.get("video_aspect") or "9:16",
            "voice_name": row.get("voice_name") or "",
            "video_language": row.get("video_language") or "zh",
            "paragraph_number": row.get("paragraph_number") or 3,
            "video_count": row.get("video_count") or 1,
        }

        # Validate
        if not kwargs["video_subject"].strip():
            results.append((row, None, f"Row {i + 1}: video_subject is empty"))
            continue

        try:
            req = TaskVideoRequest(**kwargs)
            results.append((row, req, None))
        except Exception as exc:
            results.append((row, None, f"Row {i + 1}: {exc}"))

    return results
