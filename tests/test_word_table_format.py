import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.word.contracts import TableFormat
from test_word_tables import saved_table
from test_word_creation import W


def table(plugin,revision):
    return plugin.read_objects(path='report.docx',revision=revision,ids=['table:1'],include_format=True)['objects'][0]


def patch(plugin,revision,target,properties,key='format'):
    return plugin.stage_batch(path='report.docx',revision=revision,operations=[{'id':'format','op':'format_table','target':target['id'],'expected_hash':target['hash'],'properties':properties}],idempotency_key=key)


def parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as package:return {name:package.read(name) for name in package.namelist()}


def modify_source(root,original,change):
    package=parts(original);doc=ET.fromstring(package['word/document.xml']);change(doc)
    package['word/document.xml']=ET.tostring(doc)
    with zipfile.ZipFile(root/'report.docx','w') as z:
        for name,data in package.items():z.writestr(name,data)


def test_table_layout_preserves_text_parts_and_supports_exact_undo(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        target=table(plugin,revision)
        properties={'alignment':'center','column_widths_twips':[4200,2400],
                    'borders':{'style':'single','color':'17365D','size_eighth_points':8},
                    'padding_top_twips':120,'padding_bottom_twips':120,'padding_left_twips':160,'padding_right_twips':160}
        result=patch(plugin,revision,target,properties);current=result['candidate_revision'];after=table(plugin,current)
        assert after['text']==target['text']
        fmt=after['direct_format']['properties']
        assert fmt['column_widths_twips']==['4200','2400'] and fmt['width']=='6600' and fmt['layout']=='fixed'
        assert fmt['alignment']=='center' and len(fmt['borders'])==6
        assert all(b['color']=='17365D' and b['size_eighth_points']=='8' for b in fmt['borders'])
        before_parts=parts(original);after_parts=parts(store.blob(current))
        for name,value in before_parts.items():
            if name!='word/document.xml':assert after_parts[name]==value
        before_doc=ET.fromstring(before_parts['word/document.xml']);after_doc=ET.fromstring(after_parts['word/document.xml'])
        assert [ET.canonicalize(ET.tostring(p)) for p in before_doc.iter('{'+W+'}p')]==[ET.canonicalize(ET.tostring(p)) for p in after_doc.iter('{'+W+'}p')]
        widths=[e.get('{'+W+'}w') for e in after_doc.iter('{'+W+'}tcW')]
        assert widths==['4200','2400','4200','2400']
        assert patch(plugin,revision,target,properties)==result
        assert patch(plugin,current,after,properties,'noop')['status']=='unchanged'
        with pytest.raises(HarnessError):patch(plugin,current,target,{'alignment':'left'},'stale')
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=current,action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_grid_spans_and_nested_tables_remain_intact(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    def change(doc):
        t=doc.find('.//{'+W+'}tbl');rows=t.findall('{'+W+'}tr');first=rows[0].find('{'+W+'}tc')
        ET.SubElement(first.find('{'+W+'}tcPr'),'{'+W+'}gridSpan',{'{'+W+'}val':'2'})
        rows[0].remove(rows[0].findall('{'+W+'}tc')[1])
        nested=ET.fromstring(ET.tostring(t));cell=rows[1].find('{'+W+'}tc');cell.insert(1,nested)
    try:
        modify_source(tmp_path,original,change);revision=store.file_hash('report.docx')
        old=parts((tmp_path/'report.docx').read_bytes());old_doc=ET.fromstring(old['word/document.xml']);nested=old_doc.find('.//{'+W+'}tbl/{'+W+'}tr/{'+W+'}tc/{'+W+'}tbl')
        result=patch(plugin,revision,table(plugin,revision),{'column_widths_twips':[3000,2000]})
        doc=ET.fromstring(parts(store.blob(result['candidate_revision']))['word/document.xml']);t=doc.find('.//{'+W+'}tbl')
        assert t.find('{'+W+'}tr/{'+W+'}tc/{'+W+'}tcPr/{'+W+'}tcW').get('{'+W+'}w')=='5000'
        assert ET.canonicalize(ET.tostring(t.find('.//{'+W+'}tbl')))==ET.canonicalize(ET.tostring(nested))
    finally:store.close()


def test_table_defaults_preserve_and_report_cell_overrides(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    def change(doc):
        cell=doc.find('.//{'+W+'}tcPr');borders=ET.SubElement(cell,'{'+W+'}tcBorders')
        ET.SubElement(borders,'{'+W+'}top',{'{'+W+'}val':'double','{'+W+'}color':'FF0000','{'+W+'}sz':'8'})
    try:
        modify_source(tmp_path,original,change);revision=store.file_hash('report.docx')
        result=patch(plugin,revision,table(plugin,revision),{'borders':{'style':'nil'}})
        after=table(plugin,result['candidate_revision']);assert after['direct_format']['overrides']['cells_with_borders']==1
        assert all(b['style']=='nil' for b in after['direct_format']['properties']['borders'])
        doc=ET.fromstring(parts(store.blob(result['candidate_revision']))['word/document.xml'])
        assert doc.find('.//{'+W+'}tcBorders/{'+W+'}top').get('{'+W+'}color')=='FF0000'
    finally:store.close()


@pytest.mark.parametrize('case',['wrong_count','grid_offset','floating','wrong_target'])
def test_table_rejects_ambiguous_layout_without_candidate(tmp_path,case):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        if case in ('grid_offset','floating'):
            def change(doc):
                if case=='grid_offset':
                    row=doc.find('.//{'+W+'}tr');rp=row.find('{'+W+'}trPr');rp.insert(0,ET.Element('{'+W+'}gridBefore',{'{'+W+'}val':'1'}))
                else:
                    p=doc.find('.//{'+W+'}tblPr');p.insert(0,ET.Element('{'+W+'}tblpPr',{'{'+W+'}vertAnchor':'text','{'+W+'}horzAnchor':'margin','{'+W+'}tblpX':'0','{'+W+'}tblpY':'0'}))
            modify_source(tmp_path,original,change);revision=store.file_hash('report.docx')
        target=table(plugin,revision)
        if case=='wrong_target':target=plugin.read_objects(path='report.docx',revision=revision,ids=['cell:1'])['objects'][0]
        properties={'alignment':'center'} if case in ('floating','wrong_target') else {'column_widths_twips':[3000] if case=='wrong_count' else [3000,2000]}
        with pytest.raises(HarnessError):patch(plugin,revision,target,properties)
        assert not store.candidate('report.docx')
        assert store.file_hash('report.docx')==revision
    finally:store.close()


@pytest.mark.parametrize('properties',[{}, {'column_widths_twips':[100]}, {'column_widths_twips':[20000,20000]}, {'column_widths_twips':[300]*13}, {'padding_left_twips':-1}, {'borders':{'color':'blue'}}, {'borders':{'size_eighth_points':0}}, {'alignment':'both'}])
def test_table_format_contract_bounds(properties):
    with pytest.raises(ValidationError):TableFormat(**properties)


def test_vertical_merge_widths_preserve_merge_markers(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    def change(doc):
        for i,row in enumerate(doc.find('.//{'+W+'}tbl').findall('{'+W+'}tr')):
            cp=row.find('{'+W+'}tc/{'+W+'}tcPr')
            ET.SubElement(cp,'{'+W+'}vMerge',{'{'+W+'}val':'restart' if i==0 else 'continue'})
    try:
        modify_source(tmp_path,original,change);revision=store.file_hash('report.docx')
        result=patch(plugin,revision,table(plugin,revision),{'column_widths_twips':[3000,2000]})
        doc=ET.fromstring(parts(store.blob(result['candidate_revision']))['word/document.xml'])
        assert [e.get('{'+W+'}val') for e in doc.iter('{'+W+'}vMerge')]==['restart','continue']
        assert [e.get('{'+W+'}w') for e in doc.iter('{'+W+'}tcW')]==['3000','2000','3000','2000']
    finally:store.close()


@pytest.mark.parametrize('scope',['selection','artifact'])
def test_table_layout_requires_broader_than_paragraph_scope(tmp_path,scope):
    from localoffice.annotations import annotate,bind_annotations
    from localoffice.chats import current_chat
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        paragraph=plugin.read_objects(path='report.docx',revision=revision,ids=['paragraph:5'])['objects'][0]
        annotation=annotate(store,path='report.docx',revision=revision,scope=scope,
                           locator={'object_id':paragraph['id'],'object_hash':paragraph['hash']},comment='Center this table')
        with store.transaction():
            store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('table-task','Center table','running','background','report.docx',1,1))
            store.db.execute('INSERT INTO task_chats VALUES(?,?)',('table-task',current_chat(store)))
            bind_annotations(store,'table-task','report.docx',[annotation])
        target=table(plugin,revision)
        args={'path':'report.docx','revision':revision,'operations':[{'id':'layout','op':'format_table','target':target['id'],'expected_hash':target['hash'],'properties':{'alignment':'center'}}], 'idempotency_key':'scoped','task_id':'table-task'}
        if scope=='selection':
            with pytest.raises(HarnessError,match='outside'):plugin.stage_batch(**args)
            assert not store.candidate('report.docx')
        else:
            result=plugin.stage_batch(**args)
            assert store.db.execute("SELECT head_revision FROM task_scopes WHERE task_id='table-task'").fetchone()[0]==result['candidate_revision']
    finally:store.close()


@pytest.mark.parametrize('unit,values,expected',[
    ('in',[3,2],[4320,2880]),('cm',[2.54,5.08],[1440,2880]),
    ('pt',[72,144],[1440,2880]),('twip',[1000.5,2000.4],[1001,2000])])
def test_deterministic_width_units_match_host_and_worker(tmp_path,unit,values,expected):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        properties={'column_widths':{'unit':unit,'values':values}}
        assert TableFormat(**properties).column_widths.twips()==expected
        result=patch(plugin,revision,table(plugin,revision),properties)
        assert table(plugin,result['candidate_revision'])['direct_format']['properties']['column_widths_twips']==[str(n) for n in expected]
    finally:store.close()


@pytest.mark.parametrize('widths',[{'unit':'in','values':[0.05,2]},{'unit':'in','values':[20,20]}, {'unit':'mm','values':[100,100]}, {'unit':'in','values':[float('inf'),2]}])
def test_invalid_converted_widths_are_rejected(widths):
    with pytest.raises(ValidationError):TableFormat(column_widths=widths)


def test_ambiguous_dual_unit_input_rejected():
    with pytest.raises(ValidationError):TableFormat(column_widths={'unit':'in','values':[3,2]},column_widths_twips=[4320,2880])
