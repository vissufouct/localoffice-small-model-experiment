"""Comparison-only compatibility for creation receipts predating band variants."""

VARIANT_FIELDS = frozenset(('first_header', 'first_footer', 'even_header', 'even_footer'))


def normalize_creation_identity(request):
    """Return a new mapping, omitting only null optional variant fields.

    Callers must verify the original ledger hash first. Never persist this
    projection or normalize other defaults, nested values or non-null bands.
    """
    if not isinstance(request, dict):
        return request
    return {key: value for key, value in request.items()
            if key not in VARIANT_FIELDS or value is not None}


def normalize_repair_identity(request):
    """Comparison only: historical repair calls omitted empty binding containers.

    Validate the original ledger hash before using this projection. Nonempty
    bindings and all other request fields remain part of exact identity.
    """
    if not isinstance(request,dict):return request
    return {key:value for key,value in request.items()
            if not ((key=='source_bindings' and value=={}) or (key=='bindings' and value==[]))}
