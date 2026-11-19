import pytest
from localoffice.word.creation_routing import next_operation


@pytest.mark.parametrize('errors',[
    {'items':[],'omitted_count':0},
    {'items':[{'code':'page_break_position'}],'omitted_count':1},
    {'items':[{'code':'page_break_position'},{'code':'value_error'}],'omitted_count':0},
    {},
])
def test_incomplete_or_mixed_diagnostics_keep_full_revision(errors):
    assert next_operation({'status':'invalid_design','validation_errors':errors})=='revise_creation'


def test_complete_break_diagnostic_routes_capability_without_choosing_edits():
    state={'status':'invalid_design','validation_errors':{'items':[{'code':'page_break_position','path':'blocks.3'}],'omitted_count':0}}
    assert next_operation(state)=='edit_creation_blocks'
    assert state['validation_errors']['items']==[{'code':'page_break_position','path':'blocks.3'}]
    assert next_operation({'status':'prepared'})=='finalize_creation'
