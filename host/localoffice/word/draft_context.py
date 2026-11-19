"""Scoped drafting prompt/grammar; callers must verify pending ledger ownership first.

This constructs context only. It never selects an instruction, calls a model,
changes the parent objective, saves text or dispatches document operations.
"""
import re
from ..errors import HarnessError
from ..storage import canonical

SYSTEM=('Focused drafting step: write only the newly requested prose within the selected instruction. '
        'Do not substitute a passage marked for exact copying for the requested new prose. '
        'Document assembly, formatting and completion instructions are handled in the parent task. '
        'Perform its requested purpose, language and tone. Call word.submit_draft with the actual text, '
        'or ask_user if essential information is missing. Submission retains generated text; '
        'it is not source evidence or document completion. No file operations.')


def build(instruction,ref,*,constraints=()):
    """Use exact validated instruction and effective parent decisions, without inference."""
    if not isinstance(ref,str) or not re.fullmatch(r'df_[0-9a-f]{32}',ref):
        raise HarnessError('draft_context','Expected the current draft reference.')
    if not isinstance(instruction,str) or not instruction.strip():
        raise HarnessError('draft_context','Selected drafting instruction is empty.')
    try:
        if len(instruction.encode('utf-8'))>65536:raise ValueError('Instruction bound')
        decisions=canonical(list(constraints))
        if len(decisions.encode('utf-8'))>8192:raise ValueError('Decision bound')
    except (UnicodeError,ValueError,TypeError) as exc:
        raise HarnessError('draft_context','Draft instruction or decisions exceed the context contract.') from exc
    system=SYSTEM+('\nEffective parent decisions: '+decisions if constraints else '')
    messages=[{'role':'system','content':system},{'role':'user','content':instruction}]
    tools=[{'type':'function','function':{'name':'word.submit_draft','parameters':{
        'type':'object','additionalProperties':False,'properties':{
            'ref':{'const':ref},'text':{'type':'string','minLength':1,'maxLength':8000}},'required':['ref','text']}}},
        {'type':'function','function':{'name':'ask_user','parameters':{
        'type':'object','additionalProperties':False,'properties':{
            'text':{'type':'string','minLength':1,'maxLength':2000}},'required':['text']}}}]
    return messages,tools


def planning_tool(catalogue):
    """Offer exact range selection only for a complete bounded source catalogue.

    This does not choose a span or interpret which instructions require drafting.
    Unavailable catalogues leave ordinary creation/editing capabilities intact.
    """
    if not isinstance(catalogue, dict) or catalogue.get('available') is not True or catalogue.get('complete') is not True:
        return None
    segments = catalogue.get('segments', {})
    ids = list(segments) if isinstance(segments, dict) else []
    source_hash = catalogue.get('source_hash')
    if (not 1 <= len(ids) <= 24 or ids != [f's{i:02d}' for i in range(1, len(ids) + 1)]
            or not isinstance(source_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', source_hash)):
        raise HarnessError('draft_context', 'Invalid complete objective catalogue.')
    return {'type': 'function', 'function': {
        'name': 'word.plan_draft',
        'description': ('Select the exact objective instruction for requested generated prose. '
                        'Use inclusive source range IDs. The next step drafts that text separately; '
                        'this does not insert it or complete the document. Do not select copied literals.'),
        'parameters': {'type': 'object', 'additionalProperties': False,
                       'properties': {'source_hash': {'const': source_hash},
                                      'source_range': {'type': 'object', 'additionalProperties': False,
                                                       'properties': {'start': {'enum': ids}, 'end': {'enum': ids}},
                                                       'required': ['start', 'end']}},
                       'required': ['source_hash', 'source_range']}}}
