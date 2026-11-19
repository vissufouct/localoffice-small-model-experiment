"""Versioned exact objective spans; mechanical navigation, never intent extraction.

s01..s24 are local positions in a source-hash/version catalogue, not standalone
ownership tokens. Live APIs validate task/chat ownership through content_plan.source.
Historical callers must verify their saved owner and source envelope before using
pure text APIs. Offsets are zero-based Unicode code points, end-exclusive; ID
ranges are inclusive. Joining every available segment reconstructs the source.
"""
import re

from ..errors import HarnessError
from ..storage import canonical, digest
from .content_plan import source

VERSION = 1
MAX_SEGMENTS = 24
MAX_CATALOGUE_BYTES = 2048
MAX_SOURCE_BYTES = 65536
# Punctuation only breaks before whitespace; no abbreviation/quote/meaning rules.
# Keep delimiters and following whitespace, including CRLF, in the preceding span.
_BOUNDARY = re.compile(r'(?:[.!?;:](?=\s)|\r\n|\r|\n)\s*')
_ID = re.compile(r's(?:0[1-9]|1[0-9]|2[0-4])\Z')


def _validate_text(text, version):
    if type(version) is not int or version != VERSION:
        raise HarnessError('objective_evidence_version', 'Unsupported objective evidence version.')
    if not isinstance(text, str):
        raise HarnessError('content_source', 'Objective evidence requires source text.')
    try:
        encoded = text.encode('utf-8')
    except UnicodeError as exc:
        raise HarnessError('content_source', 'Objective source must be valid Unicode.') from exc
    if len(encoded) > MAX_SOURCE_BYTES:
        raise HarnessError('content_source', 'Content source exceeds the bounded planning limit.')
    return digest(encoded)


def _unavailable(revision, reason):
    return {'available': False, 'source_hash': revision, 'version': VERSION,
            'complete': False, 'segments': {}, 'reason': reason, 'fallback': 'source_quote'}


def catalogue_text(text, *, version=VERSION):
    """Pure historical/current text projection; no ownership assertion or writes."""
    revision = _validate_text(text, version)
    if not text:
        return _unavailable(revision, 'empty_source')
    segments = {}
    start = 0
    for boundary in _BOUNDARY.finditer(text):
        end = boundary.end()
        if end > start:
            if len(segments) == MAX_SEGMENTS:
                return _unavailable(revision, 'segment_limit')
            segments[f's{len(segments)+1:02d}'] = {'text': text[start:end], 'start': start, 'end': end}
            start = end
    if start < len(text):
        if len(segments) == MAX_SEGMENTS:
            return _unavailable(revision, 'segment_limit')
        segments[f's{len(segments)+1:02d}'] = {'text': text[start:], 'start': start, 'end': len(text)}
    result = {'available': True, 'source_hash': revision, 'version': VERSION,
              'complete': True, 'segments': segments}
    if len(canonical(result).encode('utf-8')) > MAX_CATALOGUE_BYTES:
        return _unavailable(revision, 'catalogue_byte_limit')
    return result


def catalogue(store, task_id):
    """Complete exact current-objective catalogue, or explicit quote fallback."""
    with store.lock:
        _, text, revision = source(store, task_id)
        result = catalogue_text(text)
        if result['source_hash'] != revision:
            raise HarnessError('content_source', 'Objective source identity changed.')
        return result


def resolve_text(text, source_hash, start, end=None, *, version=VERSION):
    """Resolve an inclusive ID range against exact historical text.

    This pure function cannot authenticate a task/chat. Its caller owns that
    check; it never consults or substitutes the current task objective.
    """
    revision = _validate_text(text, version)
    if not isinstance(source_hash, str) or source_hash != revision:
        raise HarnessError('objective_evidence_stale', 'Objective source hash does not match.')
    end = start if end is None else end
    if any(not isinstance(value, str) or not _ID.fullmatch(value) for value in (start, end)):
        raise HarnessError('objective_evidence_range', 'Use exact catalogue IDs s01 through s24.')
    evidence = catalogue_text(text, version=version)
    if not evidence['available']:
        raise HarnessError('objective_evidence_unavailable', 'Complete objective catalogue unavailable; use source_quote.')
    segments = evidence['segments']
    if start not in segments or end not in segments or int(start[1:]) > int(end[1:]):
        raise HarnessError('objective_evidence_range', 'Select an available segment or forward contiguous range.')
    first, last = segments[start]['start'], segments[end]['end']
    return {'source_hash': revision, 'version': version, 'start_id': start, 'end_id': end,
            'start': first, 'end': last, 'text': text[first:last]}


def resolve(store, task_id, source_hash, start, end=None, *, version=VERSION):
    """Validate current task/chat and source before exact span resolution."""
    with store.lock:
        _, text, _ = source(store, task_id)
        return resolve_text(text, source_hash, start, end, version=version)
