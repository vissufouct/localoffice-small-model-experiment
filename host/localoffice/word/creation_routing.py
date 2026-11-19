"""Capability routing from validated proposal diagnostics, never edit selection."""


def next_operation(proposal):
    if proposal.get('status')!='invalid_design':return 'finalize_creation'
    errors=proposal.get('validation_errors',{})
    items=errors.get('items',[])
    if (items and errors.get('omitted_count')==0
            and all(item.get('code')=='page_break_position' for item in items)):
        return 'edit_creation_blocks'
    return 'revise_creation'


def next_operations(proposal):
    """Expose correction and completion choices without selecting any edit."""
    if proposal.get('status')=='prepared':
        if style_correction_useful(proposal):
            return ['edit_creation_blocks','edit_creation_styles','finalize_creation']
        return ['edit_creation_blocks','revise_creation','finalize_creation']
    return [next_operation(proposal)]


def style_correction_useful(proposal):
    """Expose a capability for observed font-size mismatches, never select an edit."""
    checks=proposal.get('selected_requirements',{})
    return (proposal.get('status') in ('prepared','invalid_design')
            and checks.get('status')=='mismatch'
            and any(item.get('kind')=='paragraph_font_size' and item.get('status')=='mismatch'
                    for item in checks.get('items',[]) if isinstance(item,dict)))
