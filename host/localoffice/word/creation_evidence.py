"""Bounded facts from actual DOCX XML; not intent, layout or visual certification."""
import io
import posixpath
import zipfile
from urllib.parse import unquote,urlsplit
from xml.etree import ElementTree as ET
from ..errors import HarnessError
from ..storage import canonical,digest

W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
P='{http://schemas.openxmlformats.org/package/2006/relationships}'
MAX_PACKAGE=50_000_000
MAX_PART=2_000_000
MAX_XML_TOTAL=8_000_000
MAX_NODES=50000
MAX_DEPTH=128
MAX_EVIDENCE=2_000_000


def _fail(message):
    raise HarnessError('creation_evidence',message)


class _SafeTree(ET.TreeBuilder):
    def doctype(self,*args):
        _fail('DTD/entity declarations are not permitted in creation evidence.')


class _Package:
    def __init__(self,data):
        if not isinstance(data,bytes) or len(data)>MAX_PACKAGE:_fail('Package exceeds 50 MB or is not bytes.')
        self.zip=zipfile.ZipFile(io.BytesIO(data))
        entries=self.zip.infolist();names=[e.filename for e in entries]
        if len(entries)>10000 or len(set(names))!=len(names):_fail('Invalid or oversized package directory.')
        if sum(e.file_size for e in entries)>200_000_000:_fail('Expanded package exceeds 200 MB.')
        for e in entries:
            if e.file_size>50_000_000 or e.flag_bits & 1:_fail('Oversized or encrypted package part.')
            if e.filename.startswith('/') or '\\' in e.filename or '..' in e.filename.split('/'):_fail('Unsafe package part name.')
        self.cache={};self.xml_bytes=0;self.nodes=0

    def xml(self,name):
        if name in self.cache:return self.cache[name]
        if name not in self.zip.namelist():_fail('Referenced XML part is missing: '+name)
        info=self.zip.getinfo(name)
        if info.file_size>MAX_PART:_fail('XML part exceeds 2 MB.')
        self.xml_bytes+=info.file_size
        if self.xml_bytes>MAX_XML_TOTAL:_fail('Read XML exceeds 8 MB.')
        raw=self.zip.read(name)
        parser=ET.XMLParser(target=_SafeTree())
        root=ET.fromstring(raw,parser=parser)
        stack=[(root,1)]
        while stack:
            node,depth=stack.pop();self.nodes+=1
            if self.nodes>MAX_NODES or depth>MAX_DEPTH:_fail('XML node/depth limit exceeded.')
            stack.extend((child,depth+1) for child in node)
        self.cache[name]=root
        return root

    def relationships(self,part):
        path=posixpath.join(posixpath.dirname(part),'_rels',posixpath.basename(part)+'.rels') if part else '_rels/.rels'
        if path not in self.zip.namelist():return {}
        root=self.xml(path)
        if root.tag!=P+'Relationships':_fail('Invalid relationship root.')
        result={}
        for node in root:
            if node.tag!=P+'Relationship':continue
            key=node.get('Id')
            if not key or key in result:_fail('Invalid duplicate relationship ID.')
            result[key]=dict(node.attrib)
        return result

    def target(self,part,rel,kind):
        if not rel or rel.get('Type')!=R[1:-1]+'/'+kind:_fail('Missing or mismatched '+kind+' relationship.')
        if rel.get('TargetMode') not in (None,'Internal'):_fail('External '+kind+' evidence is not read.')
        target=rel.get('Target','');url=urlsplit(target)
        if url.scheme or url.netloc or url.query or url.fragment:_fail('Invalid relationship target.')
        target=unquote(url.path)
        if not target or '\\' in target or '\x00' in target:_fail('Unsafe relationship target.')
        path=posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(posixpath.dirname(part),target))
        if path in ('.','..') or path.startswith('../'):_fail('Relationship escapes package.')
        return path


def _val(parent,path):
    node=parent.find(path)
    return node.get(W+'val') if node is not None else None


def _attrs(node):
    return {key.rsplit('}',1)[-1]:value for key,value in node.attrib.items()} if node is not None else None


def _on_off(parent, name):
    nodes=parent.findall(W+name) if parent is not None else []
    if len(nodes)>1:_fail('Duplicate activation setting: '+name)
    if not nodes:return {'explicit':False,'enabled':False}
    raw=nodes[0].get(W+'val')
    if raw not in (None,'1','true','on','0','false','off'):
        _fail('Invalid activation setting: '+name)
    return {'explicit':True,'enabled':raw in (None,'1','true','on'),'attributes':_attrs(nodes[0])}


def _paragraph(p,omissions):
    events=[];literal_coverage_safe=True
    def walk(node):
        nonlocal literal_coverage_safe
        tag=node.tag
        if tag in (W+'pPr',W+'rPr'):return
        if tag not in {W+name for name in ('p','r','t','tab','br','cr','lastRenderedPageBreak','fldSimple','fldChar','instrText')}:
            literal_coverage_safe=False
        if tag in (W+'drawing',W+'pict',W+'object',W+'txbxContent'):
            omissions.add('drawings, objects and text boxes are not inspected');return
        if tag in (W+'del',W+'moveFrom'):
            omissions.add('tracked deletion/move-from content is not inspected');return
        if tag in (W+'ins',W+'moveTo'):omissions.add('tracked changes are not resolved; inserted/move-to text is reported')
        if tag==W+'fldSimple':
            events.append({'kind':'field_begin','form':'simple','instruction':node.get(W+'instr')})
            for child in node:walk(child)
            events.append({'kind':'field_end','form':'simple'});return
        if tag==W+'fldChar':events.append({'kind':'field_'+str(node.get(W+'fldCharType')),'form':'complex'})
        elif tag==W+'instrText':events.append({'kind':'field_instruction','text':node.text or ''})
        elif tag==W+'t':events.append({'kind':'text','text':node.text or ''})
        elif tag==W+'tab':events.append({'kind':'tab'})
        elif tag==W+'br':events.append({'kind':'break','type':node.get(W+'type'),'clear':node.get(W+'clear')})
        elif tag==W+'cr':events.append({'kind':'carriage_return'})
        elif tag==W+'lastRenderedPageBreak':events.append({'kind':'cached_page_break'})
        elif tag in (W+'sym',W+'footnoteReference',W+'endnoteReference'):
            events.append({'kind':'unsupported_inline','tag':tag[len(W):]})
            omissions.add('symbols and footnote/endnote bodies are not expanded')
        for child in node:walk(child)
    walk(p)
    text=''.join(e.get('text','') if e['kind']=='text' else '\t' if e['kind']=='tab' else '\n' if e['kind'] in ('break','carriage_return') else '' for e in events)
    before=p.find(W+'pPr/'+W+'pageBreakBefore')
    section=p.find(W+'pPr/'+W+'sectPr')
    return {'kind':'paragraph','text':text,'sequence':events,'literal_coverage_safe':literal_coverage_safe,
            'style_id':_val(p,W+'pPr/'+W+'pStyle'),'alignment':_val(p,W+'pPr/'+W+'jc'),
            'page_break_before':_attrs(before),'explicit_page_breaks':sum(e['kind']=='break' and e['type']=='page' for e in events),
            'section_break':{'type':_val(section,W+'type')} if section is not None else None,
            'field_events_present':any(e['kind'].startswith('field_') for e in events)}


def _revision_safe(node):
    return not any(child.tag in {W+name for name in (
        'ins','del','moveFrom','moveTo','cellIns','cellDel','cellMerge',
        'trPrChange','tcPrChange','tblPrChange','tblGridChange')} for child in node.iter())


def _blocks(parent,omissions):
    result=[]
    for node in parent:
        if node.tag==W+'p':result.append(_paragraph(node,omissions))
        elif node.tag==W+'tbl':
            rows=[]
            for row in node.findall(W+'tr'):
                cells=[]
                for cell in row.findall(W+'tc'):
                    cells.append({'literal_coverage_safe':_revision_safe(row.find(W+'trPr')) if row.find(W+'trPr') is not None else True, 'blocks':_blocks(cell,omissions),'grid_span':_val(cell,W+'tcPr/'+W+'gridSpan'),
                                  'vertical_merge':_attrs(cell.find(W+'tcPr/'+W+'vMerge'))})
                    if cell.find(W+'tcPr') is not None and not _revision_safe(cell.find(W+'tcPr')):
                        cells[-1]['literal_coverage_safe']=False
                rows.append(cells)
            result.append({'kind':'table','literal_coverage_safe':all(_revision_safe(n) for n in node if n.tag in (W+'tblPr',W+'tblGrid')),'style_id':_val(node,W+'tblPr/'+W+'tblStyle'),'rows':rows})
            if any(n.tag not in (W+'tblPr',W+'tblGrid',W+'tr') for n in node):omissions.add('non-row table wrappers are not expanded')
        elif node.tag==W+'sdt':
            content=node.find(W+'sdtContent')
            result.append({'kind':'content_control','blocks':_blocks(content,omissions) if content is not None else []})
            omissions.add('content-control/TOC metadata and field evaluation are not inspected')
        elif node.tag not in (W+'sectPr',W+'tcPr'):
            result.append({'kind':'unsupported_block','tag':node.tag})
            omissions.add('unsupported block contents are not expanded')
    return result


def _counts(root):
    return {'paragraphs':sum(n.tag==W+'p' for n in root.iter()),'tables':sum(n.tag==W+'tbl' for n in root.iter()),
            'simple_fields':sum(n.tag==W+'fldSimple' for n in root.iter()),
            'complex_field_begins':sum(n.tag==W+'fldChar' and n.get(W+'fldCharType')=='begin' for n in root.iter())}


def _typography(parent):
    return {'fonts':_attrs(parent.find(W+'rFonts')) if parent is not None else None,
            'size_half_points':_val(parent,W+'sz') if parent is not None else None,
            'complex_script_size_half_points':_val(parent,W+'szCs') if parent is not None else None}


def inspect(data:bytes):
    """Return bounded JSON facts; unsupported coverage is explicit, never certified."""
    package=None
    try:
        package=_Package(data)
        roots=package.relationships('')
        main_rels=[r for r in roots.values() if r.get('Type')==R[1:-1]+'/officeDocument']
        if len(main_rels)!=1:_fail('Exactly one main-document relationship is required.')
        main=package.target('',main_rels[0],'officeDocument');document=package.xml(main)
        if document.tag!=W+'document':_fail('Unsupported main-document namespace/root.')
        body=document.find(W+'body')
        if body is None:_fail('Document has no body.')
        omissions={'not rendered; no intent, visual, pagination or correctness certification',
                   'style inheritance, themes and effective formatting are not resolved',
                   'field instructions and cached results are reported, never evaluated',
                   'cross-paragraph field boundaries are not paired; text may include cached results',
                   'direct run typography and unlisted paragraph/table properties are not inspected',
                   'inherited section bands are not resolved; enabled variants do not prove rendered page use'}
        blocks=_blocks(body,omissions);rels=package.relationships(main);sections=[]
        settings_rels=[r for r in rels.values() if r.get('Type')==R[1:-1]+'/settings']
        if len(settings_rels)>1:_fail('Multiple settings relationships.')
        settings=None
        if settings_rels:
            settings=package.xml(package.target(main,settings_rels[0],'settings'))
            if settings.tag!=W+'settings':_fail('Invalid settings root.')
        even=_on_off(settings,'evenAndOddHeaders')
        # Only current, direct section properties; never historical sectPrChange
        # or section properties hidden in tracked/content-control wrappers.
        section_nodes=[]
        for node in body:
            if node.tag==W+'sectPr':section_nodes.append(node)
            elif node.tag==W+'p':section_nodes.extend(node.findall(W+'pPr/'+W+'sectPr'))
        for section in section_nodes:
            item={'page_size':_attrs(section.find(W+'pgSz')),
                  'page_margins':_attrs(section.find(W+'pgMar')),
                  'title_page':_on_off(section,'titlePg')}
            for kind in ('header','footer'):
                refs=section.findall(W+kind+'Reference')
                if any(r.get(W+'type') not in ('default','first','even') for r in refs):
                    _fail('Unsupported section band reference type.')
                for variant in ('default','first','even'):
                    selected=[r for r in refs if r.get(W+'type')==variant]
                    if len(selected)>1:_fail('Multiple '+variant+' references for one section band.')
                    item[variant+'_'+kind]=None
                    if selected:
                        rel=rels.get(selected[0].get(R+'id'));part=package.target(main,rel,kind)
                        root=package.xml(part)
                        if root.tag!=W+('hdr' if kind=='header' else 'ftr'):_fail('Invalid band root.')
                        active=(variant=='default' or (item['title_page'] if variant=='first' else even)['enabled'])
                        item[variant+'_'+kind]={'part':part,'variant':variant,'active':active,
                            'blocks':_blocks(root,omissions),'descendant_counts':_counts(root)}
            sections.append(item)
        styles=[];defaults=None
        style_rels=[r for r in rels.values() if r.get('Type')==R[1:-1]+'/styles']
        if len(style_rels)>1:_fail('Multiple style relationships.')
        if style_rels:
            root=package.xml(package.target(main,style_rels[0],'styles'))
            if root.tag!=W+'styles':_fail('Invalid styles root.')
            for s in root.findall(W+'style'):
                styles.append({'id':s.get(W+'styleId'),'type':s.get(W+'type'),'default':s.get(W+'default'),
                               'based_on':_val(s,W+'basedOn'),'alignment':_val(s,W+'pPr/'+W+'jc'),
                               **_typography(s.find(W+'rPr'))})
            defaults=_typography(root.find(W+'docDefaults/'+W+'rPrDefault/'+W+'rPr'))
        else:omissions.add('no style relationship; style definitions unavailable')
        counts={kind:sum(block['kind']==kind for block in blocks) for kind in ('paragraph','table','content_control','unsupported_block')}
        result={'version':1,'revision':digest(data),'scope':'actual package XML; ordered blocks, explicit properties and native field events; no worker object IDs',
                'body':blocks,'body_top_level_counts':counts,'body_descendant_counts':_counts(body),
                'sections':sections,'even_and_odd_headers':even,'styles':styles,'document_run_defaults':defaults,'omissions':sorted(omissions)}
        if len(canonical(result).encode())>MAX_EVIDENCE:_fail('Full creation evidence exceeds 2 MB.')
        return result
    except (zipfile.BadZipFile,ET.ParseError,KeyError,ValueError,OSError,RuntimeError,NotImplementedError) as exc:
        raise HarnessError('creation_evidence','Invalid or unsupported DOCX evidence package.') from exc
    finally:
        if package is not None:package.zip.close()


def compact(evidence,max_bytes=1100):
    """Bound model projection in UTF-8 bytes, retaining explicit omission signals.

    Smaller budgets are supported down to a minimal honest envelope. The hard
    maximum remains 1100, even if callers request more. Full evidence is unchanged.
    """
    if type(max_bytes) is not int or max_bytes<64:raise ValueError('max_bytes must be an integer of at least 64.')
    budget=min(max_bytes,1100)
    result={'scope':'XML only; no intent/vision certification','omitted':True}
    if len(canonical(result).encode())>budget:
        return {'scope':'XML only','omitted':True}
    def fits(value):return len(canonical(value).encode())<=budget
    def add(key,value):
        trial={**result,key:value}
        if fits(trial):result[key]=value;return True
        return False
    add('counts',evidence.get('body_top_level_counts',{}))
    # Round-robin facts keep bands/styles visible before filling body details.
    body=[];bands=[];styles=[]
    def complex_context(blocks):
        # Scan the entire story, including tables/content controls. Paragraph
        # boundaries are not field boundaries; even an isolated end/instruction
        # means we cannot classify following text as literal versus cache.
        stack=list(blocks)
        while stack:
            node=stack.pop()
            if isinstance(node,list):stack.extend(node)
            elif isinstance(node,dict):
                if node.get('form')=='complex' or node.get('kind')=='field_instruction':return True
                stack.extend(value for value in node.values() if isinstance(value,(list,dict)))
        return False
    def paragraph(p,unknown_context=False):
        text=p.get('text','')
        item={'text':text[:48]}
        if len(text)>48:item['text_omitted']=True
        if p.get('style_id') is not None:item['style']=p['style_id']
        if p.get('alignment') is not None:item['align']=p['alignment']
        if p.get('page_break_before') is not None:
            raw=p['page_break_before'].get('val')
            if raw in (None,'1','true','on'):item['page_break_before']=True
            elif raw in ('0','false','off'):item['page_break_before']=False
            else:item['page_break_before_invalid']=True
        if p.get('explicit_page_breaks'):item['page_breaks']=p['explicit_page_breaks']
        if unknown_context:
            item.update(field_context_unknown=True,sequence_omitted=True,text_may_include_field_cache=True)
            return item
        seq=[];depth=0;instruction=None
        for event in p.get('sequence',[]):
            kind=event['kind']
            if kind=='text' and depth==0:seq.append({'t':event['text'][:32],**({'omitted':True} if len(event['text'])>32 else {})})
            elif kind=='field_begin':
                depth+=1;instruction={'field':event.get('instruction','')};seq.append(instruction)
            elif kind=='field_instruction':
                if instruction is None:instruction={'field':''};seq.append(instruction)
                instruction['field']=(instruction['field'] or '')+event['text']
            elif kind=='field_end':depth=max(0,depth-1);instruction=None
            elif kind in ('break','tab','carriage_return') and depth==0:seq.append(kind)
        for token in seq:
            if isinstance(token,dict) and isinstance(token.get('field'),str) and len(token['field'])>48:
                token['field']=token['field'][:48];token['omitted']=True
        if p.get('field_events_present'):
            item['sequence']=seq[:8]
            item['sequence_excludes_field_cache']=True
            if len(seq)>8:item['sequence_omitted']=True
        return item
    body_blocks=evidence.get('body',[])
    body_unknown=complex_context(body_blocks)
    for index,block in enumerate(body_blocks):
        body.append({'xml_block_index':index,**(paragraph(block,body_unknown) if block['kind']=='paragraph' else {'kind':block['kind'],'details_omitted':True})})
    for i,section in enumerate(evidence.get('sections',[])):
        for kind in ('header','footer'):
            for variant in ('default','first','even'):
                band=section.get(variant+'_'+kind)
                if band:
                    items=band.get('blocks',[])
                    unknown=complex_context(items)
                    bands.append({'section':i,'kind':kind,'variant':variant,'active':band.get('active',variant=='default'),
                        'blocks':[paragraph(p,unknown) if p['kind']=='paragraph' else {'kind':p['kind'],'details_omitted':True} for p in items]})
    for s in evidence.get('styles',[]):
        styles.append({k:v for k,v in {'id':s.get('id'),'fonts':s.get('fonts'),'size_half_points':s.get('size_half_points')}.items() if v is not None})
    # Insert one entry per category before extending any category.
    for index in range(max(len(body),len(bands),len(styles),0)):
        for key,values in (('body',body),('bands',bands),('styles',styles)):
            if index>=len(values):continue
            existing=result.get(key,[])
            add(key,[*existing,values[index]])
    for index,section in enumerate(evidence.get('sections',[])):
        layout={key:section[key] for key in ('page_size','page_margins') if section.get(key) is not None}
        if section.get('title_page',{}).get('explicit'):layout['title_page']=section['title_page']['enabled']
        if layout:add('section_layout',[*result.get('section_layout',[]),{'section':index,**layout}])
    if evidence.get('even_and_odd_headers',{}).get('explicit'):
        add('even_and_odd_headers',evidence['even_and_odd_headers']['enabled'])
    return result
