import json
import pytest
from localoffice.errors import HarnessError
from localoffice.native_tools import parse_qwen_call, render_prompt, validate_history

TOOLS=[{'type':'function','function':{'name':'finish','parameters':{'type':'object','properties':{'text':{'type':'string'}}}}}]


def test_native_call_maps_to_existing_action():
    raw='<tool_call>{"name":"finish","arguments":{"text":"Ready"}}</tool_call>'
    assert parse_qwen_call(raw,tools=TOOLS)=={'tool':'finish','text':'Ready'}


@pytest.mark.parametrize('raw',[
    '{"name":"finish","arguments":{}}',
    'prose <tool_call>{"name":"finish","arguments":{}}</tool_call>',
    '<tool_call>{"name":"finish","arguments":{}}',
    '<tool_call>{"name":"finish","arguments":{}}</tool_call><tool_call>{}</tool_call>',
    '<tool_call>{"name":"invented","arguments":{}}</tool_call>',
    '<tool_call>{"name":"finish","arguments":"{}"}</tool_call>',
    '<tool_call>{"name":"finish","arguments":{"tool":"invented"}}</tool_call>',
    '<tool_call>{"name":"finish","arguments":{"text":"a","text":"b"}}</tool_call>',
])
def test_invalid_native_output_never_becomes_an_action(raw):
    with pytest.raises(HarnessError):parse_qwen_call(raw,tools=TOOLS)


def history():
    return [{'role':'assistant','tool_calls':[{'id':'call-1','type':'function','function':{'name':'finish','arguments':{}}}]},
            {'role':'tool','tool_call_id':'call-1','content':'receipt'}]


def test_history_preserves_call_identity():
    validate_history(history())
    with pytest.raises(HarnessError):validate_history(history()[1:])
    with pytest.raises(HarnessError):validate_history(history()[:1])
    with pytest.raises(HarnessError):validate_history(history()+history())
    with pytest.raises(HarnessError):validate_history([history()[0],{'role':'user','content':'interrupted'}])


def test_render_uses_native_definitions_and_canonical_roles():
    class Tokenizer:
        def apply_chat_template(self,messages,**kwargs):
            return json.dumps({'messages':messages,**kwargs})
    native=json.loads(render_prompt(Tokenizer(),history(),tools=TOOLS))
    assert native['tools']==TOOLS
    assert native['messages'][1]['role']=='tool'
    assert native['messages'][1]['tool_call_id']=='call-1'
    assert 'tools' not in json.loads(render_prompt(Tokenizer(),[{'role':'user','content':'hello'}]))


def test_selected_word_tools_retain_constraints_and_local_refs():
    from localoffice.native_tools import action_tools
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    from jsonschema import Draft202012Validator
    schema=project_schema(WORD_ACTION.json_schema(),['format_text'])
    tools=action_tools(schema)
    by_name={tool['function']['name']:tool['function']['parameters'] for tool in tools}
    assert 'word.stage_batch' in by_name
    assert 'word.create_document' not in by_name
    assert set(by_name['finish']['properties'])=={'text'}
    assert set(by_name['read_file']['properties'])=={'path'}
    assert 'tool' not in by_name['word.stage_batch']['properties']
    for parameters in by_name.values():Draft202012Validator.check_schema(parameters)
    operations=by_name['word.stage_batch']['properties']['operations']['items']
    assert len(operations['oneOf'])==1
    assert 'format_text' in operations['discriminator']['mapping']


def test_native_arguments_are_validated_before_host_dispatch():
    from localoffice.native_tools import action_tools
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    tools=action_tools(project_schema(WORD_ACTION.json_schema(),[]))
    def parse(name,args):
        return parse_qwen_call('<tool_call>'+json.dumps({'name':name,'arguments':args})+'</tool_call>',tools=tools)
    assert parse('finish',{'text':'Done'})=={'tool':'finish','text':'Done'}
    for args in ({'path':'x.pdf','count':4},{'path':'x.pdf','page':0},{'path':'x.pdf','unexpected':True}):
        with pytest.raises(HarnessError,match='constraint'):parse('read_pdf',args)
    with pytest.raises(HarnessError):parse('finish',{'path':'unrelated'})


def test_nonfinite_numbers_and_remote_refs_are_rejected():
    from localoffice.native_tools import validate_native_arguments
    with pytest.raises(HarnessError):
        parse_qwen_call('<tool_call>{"name":"finish","arguments":{"text":NaN}}</tool_call>',tools=TOOLS)
    remote=[{'type':'function','function':{'name':'remote','parameters':{'type':'object','properties':{'x':{'$ref':'https://example.invalid/schema'}}}}}]
    with pytest.raises(HarnessError,match='local schema'):validate_native_arguments('remote',{'x':1},remote)


def test_batch_cannot_smuggle_an_unselected_operation():
    from localoffice.native_tools import action_tools, validate_native_arguments
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    tools=action_tools(project_schema(WORD_ACTION.json_schema(),['format_text']))
    args={'path':'report.docx','revision':'a'*64,'operations':[{
        'op':'format_text','target':'paragraph-1','expected_hash':'b'*64,
        'start':0,'end':3,'properties':{'bold':True}}]}
    validate_native_arguments('word.stage_batch',args,tools)
    WORD_ACTION.validate_python({'tool':'word.stage_batch',**args})
    args['operations'][0]={'op':'replace_text','target':'paragraph-1','expected_hash':'b'*64,'text':'unrequested'}
    with pytest.raises(HarnessError,match='constraint'):
        validate_native_arguments('word.stage_batch',args,tools)


def test_compaction_preserves_constraints_and_property_names():
    from localoffice.native_tools import compact_tools
    tools=[{'type':'function','function':{'name':name,'parameters':{'type':'object','additionalProperties':False,'properties':{'title':{'type':'string','title':'Display','maxLength':3},'default':{'type':'boolean','default':False}},'required':['title']}}} for name in ('format_text','word.format_text')]
    compact=compact_tools(tools)
    assert len(compact)==1 and compact[0]['function']['name']=='word.format_text'
    parameters=compact[0]['function']['parameters']
    assert set(parameters['properties'])=={'title','default'}
    assert parameters['properties']['title']=={'type':'string','maxLength':3}
    assert parameters['required']==['title'] and parameters['additionalProperties'] is False
    assert 'default' in tools[0]['function']['parameters']['properties']['default']


def test_character_style_batch_schema_does_not_require_a_nested_tool():
    from localoffice.native_tools import action_tools,validate_native_arguments
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    schema=project_schema(WORD_ACTION.json_schema(),['create_character_style'])
    assert 'tool' not in schema['$defs']['CreateCharacterStyle']['required']
    args={'path':'report.docx','revision':'a'*64,'operations':[{'op':'create_character_style','target':'style:Body','expected_hash':'b'*64,'style_id':'Callout','name':'Report callout','inherit_base':False,'properties':{'bold':True,'color':'17365D'}}]}
    validate_native_arguments('word.stage_batch',args,action_tools(schema))
    WORD_ACTION.validate_python({'tool':'word.stage_batch',**args})


def test_exposed_batch_requires_operation_discriminator_even_when_model_has_default():
    from localoffice.native_tools import action_tools,validate_native_arguments
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    schema=project_schema(WORD_ACTION.json_schema(),['create_character_style'])
    assert 'op' in schema['$defs']['CreateCharacterStyle']['required']
    args={'path':'report.docx','revision':'a'*64,'operations':[{'target':'style:Body','expected_hash':'b'*64,'style_id':'Callout','name':'Callout'}]}
    with pytest.raises(HarnessError,match='missing required op'):
        validate_native_arguments('word.stage_batch',args,action_tools(schema))


def test_inspection_capability_exposes_reads_without_mutation_schema():
    from localoffice.native_tools import action_tools
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema,build_state,prompt_schemas
    state=build_state(['inspect'])
    tools=action_tools(project_schema(WORD_ACTION.json_schema(),state['operations']))
    names={tool['function']['name'] for tool in tools}
    assert {'word.read_objects','word.find_text','word.list_objects'}<=names
    assert 'word.stage_batch' not in names and 'word.create' not in names
    assert prompt_schemas(['inspect'])=={}


def test_creation_blocks_require_kind_discriminator():
    from localoffice.native_tools import action_tools,validate_native_arguments
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    schema=project_schema(WORD_ACTION.json_schema(),['create'])
    assert 'kind' in schema['$defs']['ParagraphBlock']['required']
    with pytest.raises(HarnessError):
        validate_native_arguments('word.create',{'path':'new.docx','blocks':[{'text':'Exact text'}]},action_tools(schema))



def test_tool_loading_limit_error_is_actionable_and_bounded():
    from localoffice.native_tools import validate_native_arguments
    from localoffice.errors import HarnessError
    tools=[{'type':'function','function':{'name':'word.load_tools','parameters':{'type':'object','properties':{'operations':{'type':'array','maxItems':4,'items':{'type':'string'}}},'required':['operations']}}}]
    with pytest.raises(HarnessError) as exc:
        validate_native_arguments('word.load_tools',{'operations':['PRIVATE'*1000]*7},tools)
    assert 'at most 4 items; received 7' in str(exc.value)
    assert 'load the remainder later' in str(exc.value)
    assert 'PRIVATE' not in str(exc.value) and len(str(exc.value))<600


def test_compact_creation_annotations_preserve_validation_and_decoding_grammar():
    from jsonschema import Draft202012Validator
    from localoffice.native_tools import action_tools,compact_tools
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    from localoffice.decoding import native_tool_regex
    before=action_tools(project_schema(WORD_ACTION.json_schema(),['create']))
    after=compact_tools(before)
    assert native_tool_regex(before)==native_tool_regex(after)
    raw=next(x['function']['parameters'] for x in before if x['function']['name']=='word.create')
    compact=next(x['function']['parameters'] for x in after if x['function']['name']=='word.create')
    ref={'ref':'ct_'+'a'*24}
    cases=[({'path':'report.docx','blocks':[{'kind':'paragraph','text':'draft'}]},True),
           ({'path':'report.docx','blocks':[{'kind':'paragraph','text':ref}], 'footer':{'text':ref}},True),
           ({'path':'report.docx','blocks':[{'kind':'table','rows':[[ref,'draft']]}]},True),
           ({'path':'report.docx','blocks':[{'text':ref}]},False),
           ({'path':'report.docx','blocks':[{'kind':'paragraph','text':{'ref':'bad'}}]},False),
           ({'path':'report.docx','blocks':[{'kind':'paragraph','text':{**ref,'extra':True}}]},False),
           ({'path':'report.docx','blocks':[{'kind':'paragraph','text':'draft'}]*25},False)]
    for args,valid in cases:
        assert Draft202012Validator(raw).is_valid(args)==valid
        assert Draft202012Validator(compact).is_valid(args)==valid


def test_native_grammar_allows_pause_but_masks_unavailable_completion():
    import re
    from localoffice.native_tools import compact_tools,action_tools
    from localoffice.decoding import native_tool_regex
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    available=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['replace_text'])))
    pending=[t for t in available if t['function']['name']!='finish']
    regex=native_tool_regex(pending)
    pause='<tool_call>{"name":"ask_user","arguments":{"text":"Please review."}}</tool_call>'
    finish='<tool_call>{"name":"finish","arguments":{"text":"Ready."}}</tool_call>'
    assert re.fullmatch(regex,pause)
    assert not re.fullmatch(regex,finish)
    assert re.fullmatch(native_tool_regex(available),finish)
    with pytest.raises(ValueError,match='pause exit'):
        native_tool_regex([t for t in available if t['function']['name']!='ask_user'])
