"""Synthetic XML/PDF assertion checks; never invoke the benchmark runner/model."""
import importlib.util
import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape
import pytest
from test_creation_evidence import xml,rels,zipped

SPEC=importlib.util.spec_from_file_location('creation_cases',Path(__file__).resolve().parents[1]/'scripts/benchmark_word_creation_cases.py')
cases=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(cases)
DRAFT='Please review this document and share any suggestions for improvement.'


def paragraph(value,style='Body',before=False,alignment=None):
    props=f'<w:pStyle w:val="{style}"/>'
    if before:props+='<w:pageBreakBefore/>'
    if alignment:props+=f'<w:jc w:val="{alignment}"/>'
    return f'<w:p><w:pPr>{props}</w:pPr><w:r><w:t xml:space="preserve">{escape(value)}</w:t></w:r></w:p>'


def docx(case):
    body_values=[cases.C1_LITERALS[0],DRAFT,cases.C1_LITERALS[1]] if case=='C1' else [f'Body page {n}' for n in range(1,5)]
    blocks=''.join(paragraph(value,before=(i==2 if case=='C1' else i>0)) for i,value in enumerate(body_values))
    entries={'_rels/.rels':rels([('main','officeDocument','word/document.xml','')])}
    links=[('styles','styles','styles.xml',''),('settings','settings','settings.xml','')];refs=''
    for variant in (['default'] if case=='C1' else ['default','first','even']):
        for kind in ('header','footer'):
            identity=variant+'_'+kind;links.append((identity,kind,identity+'.xml',''))
            refs+=f'<w:{kind}Reference w:type="{variant}" r:id="{identity}"/>'
            label={'default':'Default','first':'First','even':'Even'}[variant]
            if kind=='header':
                p=paragraph('Relatório de revisão' if case=='C1' else label+' header',style='Header',alignment='right')
            else:
                p=paragraph('Página ' if case=='C1' else label+' page ',style='Footer',alignment='center')[:-6]
                p+='<w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>'
                p+='<w:r><w:t xml:space="preserve">'+(' de ' if case=='C1' else ' of ')+'</w:t></w:r>'
                p+='<w:fldSimple w:instr=" NUMPAGES "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>'
            entries['word/'+identity+'.xml']=xml('hdr' if kind=='header' else 'ftr',p)
    entries['word/document.xml']=xml('document','<w:body>'+blocks+'<w:sectPr>'+refs+
        '<w:pgSz w:w="11906" w:h="16838" w:orient="portrait"/>'+
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'+
        ('<w:titlePg/>' if case=='C2' else '')+'</w:sectPr></w:body>')
    entries['word/_rels/document.xml.rels']=rels(links)
    entries['word/settings.xml']=xml('settings','<w:evenAndOddHeaders/>' if case=='C2' else '')
    sizes={'Body':24 if case=='C1' else 22,'Header':20 if case=='C1' else 18,'Footer':18}
    entries['word/styles.xml']=xml('styles',''.join(f'<w:style w:type="paragraph" w:styleId="{name}"><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="{size}"/></w:rPr></w:style>' for name,size in sizes.items()))
    return zipped(entries),body_values


def altered(data,part,old,new):
    with zipfile.ZipFile(io.BytesIO(data)) as z:entries={name:z.read(name).decode() for name in z.namelist()}
    assert old in entries[part];entries[part]=entries[part].replace(old,new,1)
    return zipped(entries)


def pdf(page_lines):
    from reportlab.pdfgen.canvas import Canvas
    target=io.BytesIO();canvas=Canvas(target,pagesize=(595,842))
    for lines in page_lines:
        for index,line in enumerate(lines):canvas.drawString(40,800-index*25,line)
        canvas.showPage()
    canvas.save();return target.getvalue()


def pages(case,body):
    if case=='C1':return [['Relatório de revisão',*body[:2],'Página 1 de 2'],['Relatório de revisão',body[2],'Página 2 de 2']]
    return [[label+' header',body[i],f'{label} page {i+1} of 4'] for i,label in enumerate(['First','Even','Default','Even'])]


@pytest.mark.parametrize('case',['C1','C2'])
def test_frozen_prompts_and_supported_fixture(case):
    assert cases.prompt_for(case)
    data,body=docx(case);result=cases.assert_docx(data,case)
    assert result['body_texts']==body
    assert result['draft_semantics']==('pending_reviewer' if case=='C1' else 'not_applicable')
    result=cases.assert_pdf(pdf(pages(case,body)),case,body)
    assert result['visual_review']=='pending_reviewer'
    assert cases.CASES[case]['steps']==(10 if case=='C1' else 12)
    assert cases.CASES[case]['watchdog']==(150 if case=='C1' else 180)


@pytest.mark.parametrize('part,old,new',[
    ('word/document.xml','café','cafe'),
    ('word/document.xml','<w:pageBreakBefore/>',''),
    ('word/document.xml','<w:sectPr>','<w:p/><w:sectPr>'),
    ('word/document.xml','w:top="1440"','w:top="1134"'),
    ('word/document.xml','w:w="11906"','w:w="12000"'),
    ('word/styles.xml','w:val="24"','w:val="22"'),
    ('word/styles.xml','w:ascii="Arial"','w:ascii="Calibri"'),
    ('word/default_header.xml','w:val="right"','w:val="left"'),
    ('word/default_footer.xml',' PAGE ',' NUMPAGES '),
    ('word/default_footer.xml','Página ','Página'),
    ('word/default_footer.xml',' de ',' of '),
    ('word/default_footer.xml','<w:fldSimple w:instr=" NUMPAGES "><w:r><w:t>1</w:t></w:r></w:fldSimple>','<w:r><w:t>2</w:t></w:r>'),
    ('word/document.xml','<w:r><w:t','<w:r><w:rPr><w:sz w:val="20"/></w:rPr><w:t'),
])
def test_c1_false_positive_guards(part,old,new):
    data,_=docx('C1')
    with pytest.raises(AssertionError):cases.assert_docx(altered(data,part,old,new),'C1')


@pytest.mark.parametrize('part,old,new',[
    ('word/document.xml','<w:titlePg/>','<w:titlePg w:val="0"/>'),
    ('word/settings.xml','<w:evenAndOddHeaders/>',''),
    ('word/document.xml','w:type="first"','w:type="default"'),
    ('word/document.xml','r:id="first_header"','r:id="default_header"'),
    ('word/first_header.xml','First header','Default header'),
    ('word/even_footer.xml','Even page ','Default page '),
    ('word/_rels/document.xml.rels','Target="even_footer.xml"','Target="even_footer.xml" TargetMode="External"'),
])
def test_c2_false_positive_guards(part,old,new):
    data,_=docx('C2')
    with pytest.raises(AssertionError):cases.assert_docx(altered(data,part,old,new),'C2')


@pytest.mark.parametrize('case',['C1','C2'])
@pytest.mark.parametrize('change',['extra_page','wrong_total','wrong_page','body_wrong_page','duplicate_body','header_missing'])
def test_pdf_page_local_assertions(case,change):
    _,body=docx(case);lines=pages(case,body)
    if change=='extra_page':lines.append(['Extra page'])
    elif change=='wrong_total':lines[0][-1]=lines[0][-1][:-1]+'9'
    elif change=='wrong_page':lines[0][-1]=lines[0][-1].replace('1','9')
    elif change=='body_wrong_page':lines[0][1],lines[1][1]=lines[1][1],lines[0][1]
    elif change=='duplicate_body':lines[0].append(body[0])
    else:lines[0].pop(0)
    with pytest.raises(AssertionError):cases.assert_pdf(pdf(lines),case,body)


def test_pdf_alternate_story_leakage():
    _,body=docx('C2');lines=pages('C2',body);lines[1].append('First header')
    with pytest.raises(AssertionError):cases.assert_pdf(pdf(lines),'C2',body)


def test_draft_semantics_never_certified_by_word_count():
    data,_=docx('C1')
    bad='Our organisation completed its review yesterday and approved all financial claims.'
    result=cases.assert_docx(altered(data,'word/document.xml',DRAFT,bad),'C1')
    assert result['draft_semantics']=='pending_reviewer'


def test_short_draft_fails_machine_bound():
    data,_=docx('C1')
    with pytest.raises(AssertionError):cases.assert_docx(altered(data,'word/document.xml',DRAFT,'Please review.'),'C1')


@pytest.mark.asyncio
async def test_failed_task_still_records_metrics_events_and_exact_candidate(tmp_path,monkeypatch):
    """Fake lifecycle only: no provider start, worker or image rendering."""
    import asyncio,json
    from test_word_progress import task
    import localoffice.providers
    import localoffice.runtime
    model=tmp_path/cases.PIN;model.mkdir()
    approval=tmp_path/'docs/benchmarks';approval.mkdir(parents=True)
    (approval/'approved-mlx-download.json').write_text(json.dumps({'revision':cases.PIN,'model':{'path':str(model)}}))
    monkeypatch.setattr(cases,'ROOT',tmp_path)
    data,_=docx('C1')
    class Provider:
        sampling={'temperature':0.2,'top_p':1.0,'top_k':0,'min_p':0.0}
        def __init__(self,**kwargs):pass
        async def start(self,*args):pass
        async def close(self):pass
    class Runner:
        def __init__(self,store,provider,**kwargs):self.store=store;self.events=[]
        async def start(self,*args,**kwargs):
            task(self.store)
            revision=self.store.put_blob(data)
            with self.store.transaction():
                self.store.db.execute("UPDATE tasks SET status='paused' WHERE id='task'")
                self.store.db.execute('INSERT INTO candidates VALUES(?,?,?,?)',('report.docx','fixture-op',None,revision))
            async def done():self.events.append({'type':'error','code':'step_limit'})
            self.job=asyncio.create_task(done());return 'task'
        async def pause(self):pass
    monkeypatch.setattr(localoffice.providers,'MLXProvider',Provider)
    monkeypatch.setattr(localoffice.runtime,'TaskRunner',Runner)
    def forbidden(*args):raise AssertionError('Must not manufacture a missing render')
    monkeypatch.setattr(cases,'export_review_images',forbidden)
    out=tmp_path/'result'
    report=await cases.run('C1',out)
    stored=json.loads((out/'report.json').read_text())
    assert report['status']==stored['status']=='failed'
    assert stored['machine_status']=='failed' and stored['task_status']=='paused'
    assert stored['metrics']['model_calls']==0 and stored['metrics']['usage_complete'] is False
    assert stored['events']==[{'type':'error','code':'step_limit'}]
    assert (out/'candidate.docx').read_bytes()==data
    assert stored['candidate']['candidate_hash']==cases.sha(data)
    assert not (out/'preview.pdf').exists()
    assert stored['review']['visual']=='pending' and stored['review']['draft_semantics']=='pending'
