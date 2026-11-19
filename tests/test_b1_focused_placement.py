"""B1 handoff acceptance only; no model, workers or rendering."""
import pytest

from localoffice.word import draft_fragments as draft
from test_draft_fragments import ctx, args
from test_word_body_assertions import body
from benchmark_word_creation_cases import verify_focused_placement, sha

INVITATION='Please take a moment to review the document for accuracy and clarity.'
TEXTS=[body.LITERALS[0],INVITATION,body.LITERALS[1]]


def submit(store,text):
    plan=draft.plan_draft(store,'task',**args())
    draft.submit_draft(store,'task',ref=plan['ref'],text=text)
    return draft.get(store,'task')


def check(store,report):
    verify_focused_placement(store,'task',TEXTS,'a'*64,body.configuration(),report)


@pytest.mark.parametrize('text,indices',[
    (body.LITERALS[0],[0]),(body.LITERALS[1],[2]),
    ('Please review the document and provide your feedback when convenient.',[]),
    (INVITATION+' ',[]),
])
def test_wrong_placement_rejected_with_evidence(ctx,text,indices):
    current=submit(ctx,text);report={}
    with pytest.raises(AssertionError,match='expected body paragraph'):
        check(ctx,report)
    evidence=report['focused_draft_placement']
    assert evidence['expected_body_index']==1
    assert evidence['matching_body_indices']==indices
    assert evidence['expected_body_paragraph_match'] is False
    assert evidence['ref']==current['ref'] and evidence['generation']==current['generation']
    assert evidence['candidate_revision']=='a'*64
    assert evidence['semantic_validation']=='pending_reviewer'


def test_exact_current_draft_passes_without_semantic_approval(ctx):
    current=submit(ctx,INVITATION);report={}
    before=list(ctx.db.iterdump())
    check(ctx,report)
    assert list(ctx.db.iterdump())==before
    evidence=report['focused_draft_placement']
    assert evidence['matching_body_indices']==[1]
    assert evidence['expected_body_paragraph_match'] is True
    assert evidence['ref']==current['ref'] and evidence['verifier_version']==2
    assert evidence['semantic_validation']=='pending_reviewer'


def test_superseded_matching_generation_cannot_pass(ctx):
    old=submit(ctx,INVITATION)
    current=submit(ctx,body.LITERALS[0]);report={}
    with pytest.raises(AssertionError,match='expected body paragraph'):
        check(ctx,report)
    evidence=report['focused_draft_placement']
    assert evidence['generation']==current['generation']>old['generation']
    assert evidence['ref']==current['ref']!=old['ref']


def test_new_matching_generation_is_used(ctx):
    submit(ctx,body.LITERALS[0])
    current=submit(ctx,INVITATION);report={}
    check(ctx,report)
    assert report['focused_draft_placement']['generation']==current['generation']==2


def test_pending_generation_cannot_reuse_old_submission(ctx):
    submit(ctx,INVITATION)
    draft.plan_draft(ctx,'task',**args())
    with pytest.raises(AssertionError,match='submitted generated draft'):
        check(ctx,{})


def test_b1_version_preserves_prompt_and_limits():
    config=body.configuration()
    assert config.expected_generated_body_index==1 and config.focused_verifier_version==2
    assert sha(config.prompt.encode())==body.PROMPT_HASH==config.prompt_hash
    assert (config.steps,config.watchdog)==(10,150)
