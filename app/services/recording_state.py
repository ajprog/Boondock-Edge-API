"""Shared classification and persistence helpers for recording state."""

import re
import time
from typing import Iterable, Mapping, Optional

from .settings_manager import get_settings_manager


def is_hallucination(
    transcription: str,
    patterns: Optional[Iterable[Mapping]] = None,
) -> bool:
    """Classify one successfully returned transcription.

    An empty or whitespace-only successful result is a hallucination. Literal
    patterns use substring matching; regex patterns use Python ``re`` syntax.
    This function is only for successful transcription results. Failed or
    skipped attempts must not call it.
    """
    if not isinstance(transcription, str):
        raise TypeError('Successful transcription must be a string')
    if not transcription.strip():
        return True

    if patterns is None:
        patterns = get_settings_manager().get_all_hallucinations()

    for item in patterns:
        pattern = item['pattern']
        case_sensitive = item.get('case_sensitive', False)
        if item.get('match_type', 'literal') == 'regex':
            flags = 0 if case_sensitive else re.IGNORECASE
            if re.search(pattern, transcription, flags=flags):
                return True
        else:
            haystack = transcription if case_sensitive else transcription.casefold()
            needle = pattern if case_sensitive else pattern.casefold()
            if needle in haystack:
                return True
    return False


def update_transcription(connection, recording_id: int, transcription: str) -> bool:
    """Persist a successful transcription, classification, and update boundary."""
    hallucination = is_hallucination(transcription)
    cursor = connection.execute(
        '''UPDATE recordings
           SET transcription = ?, is_hallucination = ?,
               updated_at = MAX(updated_at + 1, ?)
           WHERE id = ?''',
        (
            transcription, hallucination,
            time.time_ns() // 1_000_000, recording_id,
        ),
    )
    return cursor.rowcount > 0
