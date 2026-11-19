"""Pure in-memory DOCX fixtures: no worker, renderer or model execution."""
import copy
import io
import zipfile
from xml.sax.saxutils import escape
import pytest
from localoffice.errors import HarnessError
from localoffice.storage import canonical,digest
from localoffice.word.creation_evidence import inspect,compact

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P='http://schemas.openxmlformats.org/package/2006/relationships'


def xml(root,body):return f'<w:{root} xmlns:w="{W}" xmlns:r="{R}">{body}</w:{root}>'
def para(text='',props='',runs=None):return '<w:p>'+('<w:pPr>'+props+'</w:pPr>' if props else '')+(runs if runs is not None else '<w:r><w:t>'+escape(text)+'</w:t></w:r>')+'</w:p>'
def rels(items):return f'<Relationships xmlns="{P}">'+''.join(f'<Relationship Id="{key}" Type="{R}/{kind}" Target="{target}"{extra}/>' for key,kind,target,extra in items)+'</Relationships>'


def package(body,header=None,footer=None,styles=None,extra=None):
    entries={'_rels/.rels':rels([('main','officeDocument','word/document.xml','')])}
    references='';links=[]
    for kind,content in [('header',header),('footer',footer)]:
        if content is None:continue
        entries[f'word/{kind}.xml']=xml('hdr' if kind=='header' else 'ftr',content)
        references+=f'<w:{kind}Reference w:type="default" r:id="{kind}"/>'
        links.append((kind,kind,kind+'.xml',''))
    if styles is not None:
        entries['word/styles.xml']=xml('styles',styles);links.append(('styles','styles','styles.xml',''))
    entries['word/document.xml']=xml('document','<w:body>'+body+'<w:sectPr>'+references+'</w:sectPr></w:body>')
    entries['word/_rels/document.xml.rels']=rels(links)
    entries.update(extra or {})
    return zipped(entries)


def zipped(entries):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for key,value in entries.items():z.writestr(key,value)
    return buf.getvalue()


def field(code,cached='1'):
    return f'<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> {code} </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>{cached}</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'


def test_actual_order_empty_break_paragraph_and_counts_no_object_ids():
    data=package(para('First')+para(runs='<w:r><w:br w:type="page"/></w:r>')+para('Second','<w:pageBreakBefore w:val="0"/><w:pStyle w:val="Body"/>'))
    evidence=inspect(data)
    assert evidence['revision']==digest(data)
    assert [p['text'] for p in evidence['body']]==['First','\n','Second']
    assert evidence['body'][1]['explicit_page_breaks']==1
    assert evidence['body'][2]['page_break_before']=={'val':'0'}
    assert evidence['body'][0]['style_id'] is None and evidence['body'][0]['alignment'] is None
    assert evidence['body_top_level_counts']['paragraph']==3
    assert evidence['body_descendant_counts']['paragraphs']==3
    assert 'paragraph:1' not in canonical(evidence)
    assert 'no intent' in canonical(evidence)


def test_bands_fields_and_style_properties_are_from_package():
    footer=para(props='<w:pStyle w:val="Band"/><w:jc w:val="center"/>',runs='<w:r><w:t>Page </w:t></w:r>'+field('PAGE')+'<w:r><w:t> of </w:t></w:r>'+field('NUMPAGES','2'))
    styles='<w:style w:type="paragraph" w:styleId="Band"><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="20"/></w:rPr></w:style>'
    evidence=inspect(package(para('First')+para('Second'),para('Quarterly report'),footer,styles))
    band=evidence['sections'][0]['default_footer']['blocks'][0]
    assert band['text']=='Page 1 of 2' and band['alignment']=='center' and band['style_id']=='Band'
    assert [x['text'] for x in band['sequence'] if x['kind']=='field_instruction']==[' PAGE ',' NUMPAGES ']
    assert evidence['styles'][0]['fonts']=={'ascii':'Calibri','hAnsi':'Calibri'}
    assert evidence['styles'][0]['size_half_points']=='20'
    assert evidence['styles'][0]['based_on']=='Normal'
    assert evidence['body'][0]['style_id'] is None
    small=compact(evidence)
    assert len(canonical(small).encode())<=1100
    assert 'Quarterly report' in canonical(small)
    compact_footer=next(b for b in small['bands'] if b['kind']=='footer')['blocks'][0]
    assert compact_footer['field_context_unknown'] is True
    assert compact_footer['sequence_omitted'] is True
    assert 'sequence' not in compact_footer
    assert 'Calibri' in canonical(small) and 'Second' in canonical(small)


def test_simple_fields_keep_native_sequence_and_cached_values():
    body=para(runs='<w:r><w:t>Count </w:t></w:r><w:fldSimple w:instr="NUMPAGES"><w:r><w:t>9</w:t></w:r></w:fldSimple>')
    evidence=inspect(package(body))
    p=evidence['body'][0]
    assert p['text']=='Count 9' and p['field_events_present']
    assert p['sequence'][1]=={'kind':'field_begin','form':'simple','instruction':'NUMPAGES'}
    assert evidence['body_descendant_counts']['simple_fields']==1
    assert 'NUMPAGES' in canonical(compact(evidence))


def test_nested_tables_and_toc_have_explicit_coverage_and_real_counts():
    inner='<w:tbl><w:tr><w:tc>'+para('Nested')+'</w:tc></w:tr></w:tbl>'
    table='<w:tbl><w:tblPr><w:tblStyle w:val="Grid"/></w:tblPr><w:tr><w:tc>'+para('Cell')+inner+'</w:tc></w:tr></w:tbl>'
    toc='<w:sdt><w:sdtContent>'+para(runs=field('TOC \\o &quot;1-3&quot;','Entry'))+'</w:sdtContent></w:sdt>'
    evidence=inspect(package(para('Intro')+table+toc+para('End')))
    assert [b['kind'] for b in evidence['body']]==['paragraph','table','content_control','paragraph']
    assert evidence['body_top_level_counts']['table']==1
    assert evidence['body_descendant_counts']['tables']==2 and evidence['body_descendant_counts']['paragraphs']==5
    assert evidence['body'][1]['rows'][0][0]['blocks'][1]['rows'][0][0]['blocks'][0]['text']=='Nested'
    assert any('TOC' in text for text in evidence['omissions'])
    assert evidence['body'][2]['blocks'][0]['sequence'][1]['text'].strip().startswith('TOC')


@pytest.mark.parametrize('budget',[64,100,400,1100,2000])
def test_compact_unicode_bytes_omissions_and_nonmutation(budget):
    evidence=inspect(package(para('😀漢'*300)+para('Tail'),para('😀'*200),para('漢'*300)))
    before=copy.deepcopy(evidence)
    result=compact(evidence,budget)
    assert len(canonical(result).encode())<=min(budget,1100)
    assert result['omitted'] is True and evidence==before
    if 'body' in result:
        first=next((x for x in result['body'] if x['xml_block_index']==0),None)
        if first:assert first['text_omitted'] is True


def test_unknown_blocks_and_drawings_are_explicitly_uncovered():
    evidence=inspect(package('<w:altChunk r:id="external"/>'+para(runs='<w:r><w:drawing><w:t>Not body text</w:t></w:drawing></w:r>')))
    assert evidence['body'][0]['kind']=='unsupported_block'
    assert evidence['body'][1]['text']==''
    assert any('drawings' in text for text in evidence['omissions'])


@pytest.mark.parametrize('doctype',[
    '<!DOCTYPE w:document [<!ENTITY x "injected">]>',
    '<!DOCTYPE w:document SYSTEM "file:///etc/passwd">',
])
def test_dtd_and_entities_rejected(doctype):
    main=doctype+xml('document','<w:body>'+para('Text')+'</w:body>')
    with pytest.raises(HarnessError):inspect(package('',extra={'word/document.xml':main}))
    with pytest.raises(HarnessError):inspect(package('',extra={'word/document.xml':main.encode('utf-16')}))


def test_external_default_band_and_duplicate_zip_parts_rejected():
    links=rels([('header','header','https://invalid.example/header.xml',' TargetMode="External"')])
    with pytest.raises(HarnessError):inspect(package('',header=para('Hi'),extra={'word/_rels/document.xml.rels':links}))
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('word/document.xml','a')
        with pytest.warns(UserWarning):z.writestr('word/document.xml','b')
    with pytest.raises(HarnessError):inspect(buf.getvalue())


@pytest.mark.parametrize('part',['../escape.xml','/absolute.xml','word\\bad.xml'])
def test_unsafe_package_paths_rejected(part):
    with pytest.raises(HarnessError):inspect(package(para('ok'),extra={part:'bad'}))


def test_part_depth_and_full_output_limits(monkeypatch):
    from localoffice.word import creation_evidence as module
    with pytest.raises(HarnessError):inspect(package('',extra={'word/document.xml':'x'*(module.MAX_PART+1)}))
    nested='<w:sdt>'*130+'<w:sdtContent/>'+'</w:sdt>'*130
    with pytest.raises(HarnessError):inspect(package(nested))
    monkeypatch.setattr(module,'MAX_EVIDENCE',100)
    with pytest.raises(HarnessError):inspect(package(para('too much for test ceiling')))


def test_missing_relationship_no_default_style_invention_and_invalid_zip():
    evidence=inspect(package(para('Hi')))
    assert evidence['styles']==[] and evidence['document_run_defaults'] is None
    assert evidence['sections']==[{'default_header':None,'default_footer':None,
        'first_header':None,'first_footer':None,'even_header':None,'even_footer':None,
        'title_page':{'explicit':False,'enabled':False},'page_size':None,'page_margins':None}]
    assert evidence['even_and_odd_headers']=={'explicit':False,'enabled':False}
    with pytest.raises(HarnessError):inspect(b'not zip')
    for budget in (0,63,True):
        with pytest.raises(ValueError):compact(evidence,budget)


def test_cross_paragraph_fields_report_events_without_claiming_cache_context():
    start=para(runs='<w:r><w:fldChar w:fldCharType="begin"/><w:instrText> TOC </w:instrText><w:fldChar w:fldCharType="separate"/></w:r>')
    result=para('Cached entry')
    end=para(props='<w:sectPr><w:type w:val="nextPage"/></w:sectPr>',runs='<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    evidence=inspect(package(start+result+end))
    assert evidence['body'][1]['text']=='Cached entry'
    assert evidence['body'][1]['field_events_present'] is False
    assert 'text_includes_field_cache' not in evidence['body'][1]
    assert evidence['body'][2]['section_break']=={'type':'nextPage'}
    assert any('cross-paragraph' in item for item in evidence['omissions'])


def test_actual_section_page_geometry_preserves_attrs_without_defaults():
    geometry='<w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/><w:pgMar w:top="720" w:right="900" w:bottom="1080" w:left="1260" w:header="300" w:footer="400" w:gutter="0"/>'
    main=xml('document','<w:body>'+para('Geometry')+'<w:sectPr>'+geometry+'</w:sectPr></w:body>')
    evidence=inspect(package('',extra={'word/document.xml':main}))
    section=evidence['sections'][0]
    assert section['page_size']=={'w':'16838','h':'11906','orient':'landscape'}
    assert section['page_margins']=={'top':'720','right':'900','bottom':'1080','left':'1260','header':'300','footer':'400','gutter':'0'}
    projected=compact(evidence)
    assert projected['section_layout'][0]=={'section':0,'page_size':section['page_size'],'page_margins':section['page_margins']}
    assert len(canonical(projected).encode())<=1100
    tiny=compact(evidence,100)
    assert 'section_layout' not in tiny and tiny['omitted'] is True
    partial=xml('document','<w:body><w:sectPr><w:pgSz w:w="11906"/><w:pgMar w:top="720"/></w:sectPr></w:body>')
    limited=inspect(package('',extra={'word/document.xml':partial}))['sections'][0]
    assert limited['page_size']=={'w':'11906'} and limited['page_margins']=={'top':'720'}


@pytest.mark.parametrize('raw,expected',[(None,True),('1',True),('true',True),('on',True),('0',False),('false',False),('off',False)])
def test_compact_page_break_before_on_off_keeps_full_raw_attrs(raw,expected):
    attrs='' if raw is None else f' w:val="{raw}"'
    evidence=inspect(package(para('Break',f'<w:pageBreakBefore{attrs}/>')))
    assert evidence['body'][0]['page_break_before']==({} if raw is None else {'val':raw})
    assert compact(evidence)['body'][0]['page_break_before'] is expected


def test_compact_does_not_infer_missing_or_invalid_page_break_flag():
    evidence=inspect(package(para('Absent')+para('Invalid','<w:pageBreakBefore w:val="invalid"/>')))
    projection=compact(evidence)
    assert 'page_break_before' not in projection['body'][0]
    assert 'page_break_before' not in projection['body'][1]
    assert projection['body'][1]['page_break_before_invalid'] is True
    assert evidence['body'][1]['page_break_before']=={'val':'invalid'}


def test_sequence_cache_omission_does_not_claim_preview_excludes_cache():
    evidence=inspect(package(para(runs='<w:r><w:t>Page </w:t></w:r><w:fldSimple w:instr=" PAGE "><w:r><w:t>7</w:t></w:r></w:fldSimple>')))
    projected=compact(evidence)['body'][0]
    assert projected['text']=='Page 7'
    assert projected['sequence_excludes_field_cache'] is True
    assert 'field_cache_omitted' not in projected
    assert projected['sequence']==[{'t':'Page '},{'field':' PAGE '}]


def assert_unknown_projection(item):
    assert item['field_context_unknown'] is True
    assert item['sequence_omitted'] is True
    assert item['text_may_include_field_cache'] is True
    assert 'sequence' not in item
    assert 'sequence_excludes_field_cache' not in item


def test_compact_complex_continuation_cache_never_becomes_literal_sequence():
    begin=para(runs='<w:r><w:fldChar w:fldCharType="begin"/><w:instrText> TOC </w:instrText><w:fldChar w:fldCharType="separate"/></w:r>')
    cache=para('Cached middle')
    end=para(runs='<w:r><w:t>Cached end</w:t><w:fldChar w:fldCharType="end"/></w:r>')
    evidence=inspect(package(begin+cache+end))
    before=copy.deepcopy(evidence)
    projected=compact(evidence)
    assert len(projected['body'])==3
    for item in projected['body']:assert_unknown_projection(item)
    assert projected['body'][2]['text']=='Cached end'
    assert evidence==before
    assert len(canonical(projected).encode())<=1100


@pytest.mark.parametrize('wrapper',['table','content_control'])
def test_compact_complex_context_scans_nested_story_blocks(wrapper):
    nested=para(runs=field('PAGE'))
    if wrapper=='table':nested='<w:tbl><w:tr><w:tc>'+nested+'</w:tc></w:tr></w:tbl>'
    else:nested='<w:sdt><w:sdtContent>'+nested+'</w:sdtContent></w:sdt>'
    evidence=inspect(package(para('Before')+nested+para('After')))
    projected=compact(evidence)
    paragraphs=[b for b in projected['body'] if 'text' in b]
    assert len(paragraphs)==2
    for item in paragraphs:assert_unknown_projection(item)
    assert len(canonical(projected).encode())<=1100


def test_compact_complex_band_does_not_contaminate_body_or_other_band():
    simple=para(runs='<w:fldSimple w:instr="PAGE"><w:r><w:t>1</w:t></w:r></w:fldSimple>')
    footer=para('Continuation')+para(runs='<w:r><w:t>Cache</w:t><w:fldChar w:fldCharType="end"/></w:r>')
    evidence=inspect(package(simple,header=simple,footer=footer))
    projected=compact(evidence)
    assert projected['body'][0]['sequence_excludes_field_cache'] is True
    header=next(b for b in projected['bands'] if b['kind']=='header')
    assert header['blocks'][0]['sequence_excludes_field_cache'] is True
    footer=next(b for b in projected['bands'] if b['kind']=='footer')
    for item in footer['blocks']:assert_unknown_projection(item)
    assert len(canonical(projected).encode())<=1100


@pytest.mark.parametrize('budget',[64,400,1100])
def test_compact_unknown_context_remains_byte_bounded(budget):
    evidence=inspect(package(para('😀'*300)+para(runs=field('PAGE'))))
    projection=compact(evidence,budget)
    assert len(canonical(projection).encode())<=budget
    for item in projection.get('body',[]):
        if 'text' in item:assert_unknown_projection(item)


def variant_package(*, variants=None, title='', even=None, extra_sections='', settings_extra='', refs_extra=''):
    """Explicit variant fixture independent of worker/build availability."""
    variants=variants or {}
    entries={};links=[];refs=''
    for (variant,kind),content in variants.items():
        key=variant+'_'+kind
        entries['word/'+key+'.xml']=xml('hdr' if kind=='header' else 'ftr',content)
        links.append((key,kind,key+'.xml',''))
        refs+=f'<w:{kind}Reference w:type="{variant}" r:id="{key}"/>'
    if even is not None or settings_extra:
        entries['word/settings.xml']=xml('settings',(even or '')+settings_extra)
        links.append(('settings','settings','settings.xml',''))
    entries['word/_rels/document.xml.rels']=rels(links)
    entries['word/document.xml']=xml('document','<w:body>'+para('Body')+extra_sections+
        '<w:sectPr>'+refs+refs_extra+title+'</w:sectPr></w:body>')
    return package('',extra=entries)


def test_explicit_variants_and_activation_settings_preserved_without_inheritance():
    variants={(v,k):para(v+' '+k) for v in ('default','first','even') for k in ('header','footer')}
    evidence=inspect(variant_package(variants=variants,title='<w:titlePg/>',even='<w:evenAndOddHeaders/>'))
    section=evidence['sections'][0]
    assert section['title_page']=={'explicit':True,'enabled':True,'attributes':{}}
    assert evidence['even_and_odd_headers']=={'explicit':True,'enabled':True,'attributes':{}}
    for (v,k) in variants:
        band=section[v+'_'+k]
        assert band['active'] is True and band['variant']==v
        assert band['blocks'][0]['text']==v+' '+k
        assert band['part']=='word/'+v+'_'+k+'.xml'
    assert 'rendered page use' in ' '.join(evidence['omissions'])


@pytest.mark.parametrize('raw,enabled',[(None,True),('true',True),('1',True),('on',True),('false',False),('0',False),('off',False)])
def test_variant_activation_on_off_values(raw,enabled):
    attr='' if raw is None else f' w:val="{raw}"'
    evidence=inspect(variant_package(variants={('first','header'):para('First'),('even','footer'):para('Even')},
        title=f'<w:titlePg{attr}/>',even=f'<w:evenAndOddHeaders{attr}/>'))
    assert evidence['sections'][0]['first_header']['active'] is enabled
    assert evidence['sections'][0]['even_footer']['active'] is enabled
    assert evidence['sections'][0]['title_page']['attributes']==({} if raw is None else {'val':raw})


@pytest.mark.parametrize('title,even',[
    ('<w:titlePg w:val="maybe"/>',None),('<w:titlePg/><w:titlePg/>',None),
    ('','<w:evenAndOddHeaders w:val="maybe"/>'),('','<w:evenAndOddHeaders/><w:evenAndOddHeaders/>')])
def test_invalid_or_ambiguous_activation_fails_closed(title,even):
    with pytest.raises(HarnessError,match='activation'):
        inspect(variant_package(title=title,even=even))


@pytest.mark.parametrize('variant',['default','first','even'])
def test_duplicate_variant_references_rejected_even_when_inactive(variant):
    with pytest.raises(HarnessError,match='Multiple'):
        inspect(variant_package(variants={(variant,'footer'):para('F')},
            refs_extra=f'<w:footerReference w:type="{variant}" r:id="{variant}_footer"/>'))


def test_historical_section_properties_not_promoted_to_current_stories():
    historical='<w:sectPrChange><w:sectPr><w:headerReference w:type="first" r:id="missing"/><w:titlePg/></w:sectPr></w:sectPrChange>'
    evidence=inspect(variant_package(refs_extra=historical))
    assert len(evidence['sections'])==1
    assert evidence['sections'][0]['first_header'] is None


@pytest.mark.parametrize('budget',[64,300,600,1100])
def test_compact_variant_attribution_and_bounds(budget):
    evidence=inspect(variant_package(variants={('first','header'):para('First'),('even','footer'):para('Even')},
        title='<w:titlePg/>',even='<w:evenAndOddHeaders w:val="0"/>'))
    before=copy.deepcopy(evidence)
    result=compact(evidence,budget)
    assert len(canonical(result).encode())<=budget and evidence==before
    for band in result.get('bands',[]):
        assert band['variant'] in ('first','even')
        assert band['active'] is (band['variant']=='first')
    if budget==1100:
        assert len(result['bands'])==2
        assert result['even_and_odd_headers'] is False
        assert result['section_layout']==[{'section':0,'title_page':True}]
