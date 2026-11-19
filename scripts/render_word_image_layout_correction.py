"""Correct the retained crowded figure fixture using one compact registered patch.

Deterministic layout evidence only; no real-model or visual approval is inferred.
Run in the coordinator's render slot, after worker builds settle.
"""
import argparse
import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer

W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
WP='{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}'

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    source=Path('docs/benchmarks/word-image-layout-20261117/floating-square.docx')
    original=source.read_bytes()
    source_hash=hashlib.sha256(original).hexdigest()
    retained=json.loads((source.parent/'render-evidence.json').read_text())
    assert source_hash==next(e['revision'] for e in retained['evidence'] if e['label']=='floating-square')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    (root/'report.docx').write_bytes(original)
    store=ProjectStore(root);plugin=WordPlugin(store)
    try:
        image=plugin.read_objects(path='report.docx',revision=source_hash,ids=['image:1'],include_format=True)['objects'][0]
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            doc=ET.fromstring(archive.read('word/document.xml'))
            sect=doc.find('.//'+W+'sectPr');page=sect.find(W+'pgSz');margins=sect.find(W+'pgMar')
            content_inches=(int(page.get(W+'w'))-int(margins.get(W+'left'))-int(margins.get(W+'right')))/1440
        width=image['direct_format']['properties']['width_inches']
        offset=(content_inches-width)/2
        patch={'id':'figure-layout','op':'set_image_layout','target':image['id'],'expected_hash':image['hash'],
               'properties':{'wrap':'top_bottom','horizontal_origin':'margin','horizontal_offset_inches':offset}}
        receipt=plugin.stage_batch(path='report.docx',revision=source_hash,operations=[patch],idempotency_key='correct-figure')
        revision=receipt['candidate_revision'];updated=store.blob(revision)
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(updated)) as new:
            assert set(old.namelist())==set(new.namelist())
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name),name
            updated_doc=ET.fromstring(new.read('word/document.xml'))
            assert [n.text for n in doc.iter(W+'t')]==[n.text for n in updated_doc.iter(W+'t')]
            # Only the explicitly requested horizontal placement and wrap may differ.
            for tree in (doc,updated_doc):
                anchors=tree.findall('.//'+WP+'anchor');assert len(anchors)==1
                for child in list(anchors[0]):
                    if child.tag in (WP+'positionH',WP+'wrapSquare',WP+'wrapTopAndBottom'):
                        anchors[0].remove(child)
            assert ET.canonicalize(ET.tostring(doc))==ET.canonicalize(ET.tostring(updated_doc))
        render=await WordRenderer(store).render(path='report.docx',revision=revision)
        (root/'corrected.docx').write_bytes(updated);(root/'corrected.pdf').write_bytes(store.blob(render['pdf_hash']))
        assert (root/'report.docx').read_bytes()==original
        store.apply(receipt['receipt_id'],expected_candidate=revision,action='apply')
        undo=store.undo(receipt['receipt_id'],'undo-correction');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        restore=store.undo(undo['id'],'restore-correction');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==updated
        evidence={'status':'rendered_pending_inspection','source':str(source),'source_revision':source_hash,
                  'revision':revision,'pdf_hash':render['pdf_hash'],'pages':render['pages'],'patch':patch,
                  'unchanged_package_parts':True,'unselected_document_properties_preserved':True,'text_preserved':True,'exact_undo_restore':True,
                  'reviewer':None,'viewed_pages':[],
                  'limitation':'Deterministic correction of retained crowded fixture; no local-model or full professional layout qualification.'}
        (root/'evidence.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print(json.dumps(evidence))
    except Exception as exc:
        (root/'failure.json').write_text(json.dumps({'status':'failed','error':repr(exc),'source_revision':source_hash,
            'candidate':store.candidate('report.docx'),'review_status':'not_reviewed'},indent=2)+'\n')
        raise
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
