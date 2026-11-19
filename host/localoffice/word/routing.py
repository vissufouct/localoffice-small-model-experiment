"""Conservative initial capability hints, never document mutation authority."""
import re


def initial_operations(objective):
    """Recognize explicit English whole-paragraph removal, otherwise abstain.

    This selects guidance only. It does not locate objects, approve edits, prove
    intent coverage, or restrict the model from loading additional capabilities.
    Never call it on document text, tool output, or generated summaries.
    """
    if len(objective)>65536:return []
    pattern=r'\b(?:delete|remove)\s+(?:(?:the|this|that|an?)\s+)?(?:(?:entire|whole)\s+)?paragraph\b'
    for match in re.finditer(pattern,objective,re.IGNORECASE):
        prefix=objective[max(0,match.start()-60):match.start()]
        # Conservative abstention around negation and hypothetical discussion.
        if re.search(r"\b(?:not|never|avoid|without|whether|if|example|should|explain|describe)\b|n't",prefix,re.IGNORECASE):continue
        suffix=objective[match.end():match.end()+24]
        if re.match(r"(?:'s)?\s+(?:text|contents?|formatting|style|border|spacing|numbering|indentation|alignment|shading|mark)\b",suffix,re.IGNORECASE):continue
        return ['delete_paragraph']
    return []
