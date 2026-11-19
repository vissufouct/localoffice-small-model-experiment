"""Small decoding grammar; the original host schema remains authoritative."""
import json
import re
from collections import OrderedDict

STYLE_ID_PATTERN=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$'
STYLE_ID_SYNTAX=r'^[A-Za-z][A-Za-z0-9_-]*$'

HOST_ONLY={'maxLength','minLength','minimum','maximum','exclusiveMinimum','exclusiveMaximum',
           'maxItems','minItems','pattern','title','description','default','discriminator'}


def _bounded_native_revision(value):
    """Exact small revision shape only; never preserve general object-array bounds."""
    if value.get('type')!='object' or value.get('additionalProperties') is not False:return False
    props=value.get('properties',{})
    if set(props)!= {'ref','changes'} or set(value.get('required',[]))!={'ref','changes'}:return False
    ref=props['ref'];changes=props['changes']
    if not (ref.get('pattern')==r'^cr_[0-9a-f]{32}$' or
            isinstance(ref.get('const'),str) and re.fullmatch(r'cr_[0-9a-f]{32}',ref['const'])):return False
    if (changes.get('type')!='array' or type(changes.get('minItems')) is not int
            or type(changes.get('maxItems')) is not int
            or changes['minItems']!=1 or changes['maxItems']!=4
            or changes.get('items')!={'$ref':'#/$defs/CreationChange'}):return False
    definition=value.get('$defs',{}).get('CreationChange',{})
    if (definition.get('type')!='object' or definition.get('additionalProperties') is not False
            or set(definition.get('required',[]))!={'op','path'}):return False
    fields=definition.get('properties',{})
    if set(fields)!={'op','path','value'}:return False
    # Strip only display annotations; arbitrary nested values must remain the
    # existing unrestricted JSON schema, not an expensive substituted schema.
    clean=lambda item:{k:v for k,v in item.items() if k not in ('title','description','default')}
    op=clean(fields['op']);op.pop('type',None)
    from .native_tools import creation_revision_path_pattern
    path=clean(fields['path'])
    if path.get('pattern')==creation_revision_path_pattern():path.pop('pattern')
    return (op=={'enum':['set','remove','insert']}
            and path=={'type':'string','minLength':1,'maxLength':100}
            and clean(fields['value'])=={})


def _bounded_native_block_edit(value):
    """Trust only the complete registered typed block-edit contract shape."""
    definitions=value.get('$defs',{})
    if not {'RemoveCreationBlock','SetCreationParagraph'}<=definitions.keys():return False
    if set(value.get('properties',{}))!={'ref','changes'}:return False
    from .word.contracts import EditCreationBlocks
    expected=EditCreationBlocks.model_json_schema()
    expected['properties'].pop('tool')
    expected['required']=[key for key in expected.get('required',[]) if key!='tool']
    from .native_tools import creation_block_parameters
    expected=creation_block_parameters(expected)
    actual_ref=value['properties']['ref']
    if 'const' in actual_ref:
        if not isinstance(actual_ref['const'],str) or not re.fullmatch(r'cr_[0-9a-f]{32}',actual_ref['const']):return False
        expected['properties']['ref']={'const':actual_ref['const']}
    for name in ('RemoveCreationBlock','SetCreationParagraph'):
        index=definitions.get(name,{}).get('properties',{}).get('index',{})
        if 'enum' in index:
            indices=index['enum']
            if (not isinstance(indices,list) or not 1<=len(indices)<=24
                    or any(type(n) is not int or not 0<=n<=23 for n in indices)
                    or indices!=sorted(set(indices))):return False
            expected['$defs'][name]['properties']['index']={'type':'integer','enum':indices}
    def clean(item):
        if isinstance(item,dict):
            result={key:clean(child) for key,child in item.items()
                    if key not in ('title','description','default','discriminator')}
            if result.get('type')=='string' and ('const' in result or 'enum' in result):result.pop('type')
            return result
        if isinstance(item,list):return [clean(child) for child in item]
        return item
    changes=expected['properties']['changes']
    return (changes.get('minItems')==1 and changes.get('maxItems')==4
            and clean(value)==clean(expected))


def _bounded_native_requirements(value):
    """Only the registered requirement contract and its exact evidence projections.

    Compare the complete schema, including every union member and host bound.
    This does not enable bounds on arbitrary arrays of objects or host unions.
    """
    if (value.get('type')!='object' or value.get('additionalProperties') is not False
            or set(value.get('properties',{}))!={'source_hash','requirements'}):return False
    requirements=value['properties']['requirements']
    if (not isinstance(requirements,dict) or type(requirements.get('maxItems')) is not int
            or requirements['maxItems']!=8):return False
    from .word.requirements import PlanRequirements
    from .native_tools import action_tools,requirement_evidence_tools
    tools=action_tools(PlanRequirements.model_json_schema())
    definitions=value.get('$defs',{})
    if not isinstance(definitions,dict):return False
    if 'NativeRequirementSourceRange' in definitions:
        selected=definitions['NativeRequirementSourceRange']
        if not isinstance(selected,dict):return False
        props=selected.get('properties',{})
        if not isinstance(props,dict) or not isinstance(props.get('start'),dict):return False
        ids=props['start'].get('enum')
        if (not isinstance(ids,list) or not 1<=len(ids)<=24
                or ids!=[f's{i:02d}' for i in range(1,len(ids)+1)]):return False
        tools=requirement_evidence_tools(tools,{'available':True,'complete':True,
                                               'segments':dict.fromkeys(ids)})
    elif 'SourceRange' not in definitions:
        tools=requirement_evidence_tools(tools,{'available':False})
    expected=tools[0]['function']['parameters']
    actual_hash=value['properties']['source_hash']
    if not isinstance(actual_hash,dict):return False
    if 'const' in actual_hash:
        fixed=actual_hash['const']
        if not isinstance(fixed,str) or not re.fullmatch(r'[0-9a-f]{64}',fixed):return False
        expected['properties']['source_hash']={'const':fixed}

    def clean(item):
        if isinstance(item,dict):
            result={key:({name:clean(schema) for name,schema in child.items()}
                         if key in ('properties','$defs','definitions') else clean(child))
                    for key,child in item.items()
                    if key not in ('title','description','default','discriminator')}
            if result.get('type')=='string' and ('const' in result or 'enum' in result):result.pop('type')
            return result
        if isinstance(item,list):return [clean(child) for child in item]
        return item
    # JSON comparison distinguishes booleans from integers (True == 1 in Python).
    return json.dumps(clean(value),sort_keys=True)==json.dumps(clean(expected),sort_keys=True)


def structural_schema(value):
    """Retain JSON structure/types/enums; avoid huge finite repetition automata.

    Nonempty strings, small string-enum array bounds and trusted style syntax are
    retained. Other length/range/pattern checks remain
    with Pydantic after generation.
    Do not call this a complete JSON Schema validator.
    """
    if isinstance(value,dict):
        result={key:({name:structural_schema(schema) for name,schema in child.items()} if key in ('properties','$defs','definitions') else structural_schema(child)) for key,child in value.items() if key not in HOST_ONLY}
        trusted_revision=_bounded_native_revision(value)
        if trusted_revision or _bounded_native_block_edit(value):
            result['properties']['changes'].update(minItems=1,maxItems=4)
        if _bounded_native_requirements(value):
            result['properties']['requirements']['maxItems']=8
        if trusted_revision:
            from .native_tools import creation_revision_path_pattern
            path=value['$defs']['CreationChange']['properties']['path']
            if path.get('pattern')==creation_revision_path_pattern():
                result['$defs']['CreationChange']['properties']['path']['pattern']=creation_revision_path_pattern()
                # Outlines gives minLength precedence over pattern. The rooted
                # anchored syntax already implies nonempty; retain host bounds.
                result['$defs']['CreationChange']['properties']['path'].pop('minLength',None)
            # Outlines emits an ungrouped alternation for {}. Nested directly
            # in a property, only its first (boolean) branch composes correctly.
            # A one-branch anyOf groups the SAME unrestricted backend grammar;
            # it adds no JSON-type/path restrictions. Backend nesting remains
            # bounded (currently three containers), not the host's depth 32.
            result['$defs']['CreationChange']['properties']['value']={'anyOf':[{}]}
        # Exact trusted projection only: ASCII identifier syntax is cheap;
        # the 64-character limit stays host-only, avoiding repeated finite
        # length automata across every creation style slot.
        if value.get('type')=='string' and value.get('pattern')==STYLE_ID_PATTERN:
            result['pattern']=STYLE_ID_SYNTAX
        # One-or-more characters needs no large finite repetition automaton.
        # Keep this universal lower bound; larger lengths remain host checks.
        if value.get('type')=='string' and value.get('minLength')==1:
            result['minLength']=1
        # Small finite capability lists are cheap to bound. Keep large or
        # nested document arrays host-validated to avoid oversized automata.
        items=value.get('items',{})
        choices=items.get('enum') if isinstance(items,dict) else None
        upper=value.get('maxItems');lower=value.get('minItems',0)
        if (value.get('type')=='array' and type(upper) is int and type(lower) is int
                and 0<=lower<=upper<=8 and isinstance(choices,list) and 1<=len(choices)<=64
                and all(isinstance(choice,str) and len(choice)<=80 for choice in choices)):
            result['maxItems']=upper
            if 'minItems' in value:result['minItems']=lower
        # Outlines regex generation fixes property order. Let the model choose
        # the tool before its arguments, matching our tool-call protocol.
        for selector in ('tool','op','kind'):
            if 'properties' in result and selector in result['properties']:
                props=result['properties'];result['properties']={selector:props[selector],**{k:v for k,v in props.items() if k!=selector}}
        return result
    if isinstance(value,list):return [structural_schema(child) for child in value]
    return value


class MLXDecoding:
    def __init__(self,model,tokenizer):
        from outlines.models.mlxlm import MLXLM
        from outlines.backends.outlines_core import OutlinesCoreBackend
        self.backend=OutlinesCoreBackend(MLXLM(model,tokenizer))
        self.cache=OrderedDict()

    def processor(self,schema):
        from outlines.backends.outlines_core import OutlinesCoreLogitsProcessor
        key=json.dumps(structural_schema(schema),separators=(',',':'))
        if key not in self.cache:
            # Evict before compilation to bound retained automata. Each generation
            # gets a fresh guide; only immutable compiled indexes are reused.
            if len(self.cache)>=2:self.cache.popitem(last=False)
            self.cache[key]=self.backend.get_json_schema_logits_processor(key,whitespace_pattern='[ ]?').index
        self.cache.move_to_end(key)
        return OutlinesCoreLogitsProcessor(self.cache[key],'mlx')


def native_tool_regex(tools):
    """Qwen wrapper plus exact exposed names and structural argument schemas."""
    import re
    from outlines_core.json_schema import build_regex_from_schema
    from .native_tools import tool_names
    names=tool_names(tools)
    # Completion can be unavailable while a host prerequisite is unresolved.
    # A non-mutating pause/question path must always remain legal.
    if 'ask_user' not in names:
        raise ValueError('Native grammar requires the ask_user pause exit path.')
    branches=[]
    for tool in tools:
        function=tool['function']
        arguments=structural_schema(function['parameters'])
        # Compile each independent definition scope without rewriting $refs.
        argument_regex=build_regex_from_schema(json.dumps(arguments),whitespace_pattern='[ ]?')
        name=re.escape(json.dumps(function['name']))
        branches.append(r'\{"name": ?'+name+r', ?"arguments": ?'+argument_regex+r'\}')
    return r'<tool_call>\n?('+ '|'.join(branches) +r')\n?</tool_call>'


def native_processor(decoder,tools):
    """Fresh per-generation guide; at most two retained immutable indexes."""
    from outlines.backends.outlines_core import OutlinesCoreLogitsProcessor
    regex=native_tool_regex(tools)
    key='native:'+regex
    if key not in decoder.cache:
        if len(decoder.cache)>=2:decoder.cache.popitem(last=False)
        decoder.cache[key]=decoder.backend.get_regex_logits_processor(regex).index
    decoder.cache.move_to_end(key)
    return OutlinesCoreLogitsProcessor(decoder.cache[key],'mlx')
