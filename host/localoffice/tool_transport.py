"""Normalize an exact presentation wrapper; never repair or extract arbitrary JSON."""
import re


def json_payload(raw):
    match=re.fullmatch(r'```(?:json)?\r?\n(.*)\r?\n```',raw.strip(),flags=re.DOTALL)
    return (match.group(1),True) if match else (raw,False)


def reject_duplicate_keys(payload):
    """Reject ambiguous JSON before schema parsing can silently keep the last value."""
    import json
    from .errors import HarnessError

    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise HarnessError('duplicate_json_key','Repeated JSON keys are not allowed. Put each edit in its own object inside operations; never combine targets/properties in one object. No tool executed.')
            result[key]=value
        return result

    try:
        json.loads(payload,object_pairs_hook=unique)
    except (json.JSONDecodeError,RecursionError):
        # Preserve the existing schema parser's diagnostics for malformed JSON.
        return
