"""Complete explicit style IDs from a fresh retained proposal; no writes."""
import re

from pydantic import ValidationError

from ..errors import HarnessError
from . import creation_repair
from .contracts import WordStyle


def _unavailable(ref, reason, payload_hash=None):
    # None is deliberately distinct from a verified empty explicit inventory.
    return {'version': 1, 'ref': ref, 'payload_hash': payload_hash,
            'status': 'unavailable', 'complete': False, 'count': None, 'ids': None,
            'reason': reason}


def explicit_style_inventory(store, task_id, ref):
    """Read ordered explicit IDs, excluding implicit defaults and style uses.

    Missing/superseded/stale/ambiguous proposals provide no restrictive evidence.
    Ownership, integrity and malformed retained-style failures propagate. An
    omitted styles property means no explicit declarations under the contract.
    """
    with store.transaction():
        state = creation_repair.get(store, task_id)
        if not isinstance(ref, str) or not re.fullmatch(r'cr_[0-9a-f]{32}', ref):
            raise HarnessError('creation_repair_reference', 'Expected a retained proposal reference.')
        if state is None:
            return _unavailable(ref, 'missing')
        if state['ref'] != ref:
            return _unavailable(ref, 'superseded')
        try:
            creation_repair._fresh(store, state)
        except HarnessError as exc:
            if exc.code not in creation_repair.FRESHNESS_ERRORS:
                raise
            return _unavailable(ref, 'stale', state['payload_hash'])
        try:
            styles = state['payload'].get('styles', [])
            if not isinstance(styles, list) or len(styles) > 16:
                raise ValueError('Style list bound')
            ids = [WordStyle.model_validate(style).id for style in styles]
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            raise HarnessError('state_corrupt', 'Retained explicit styles violate the WordStyle contract.') from exc
        if len(ids) != len(set(ids)):
            return _unavailable(ref, 'ambiguous', state['payload_hash'])
        return {'version': 1, 'ref': ref, 'payload_hash': state['payload_hash'],
                'status': 'verified', 'complete': True, 'count': len(ids), 'ids': ids}
