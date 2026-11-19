"""Opt-in layout visual evidence. Run only in the coordinator's rendering slot.

Writes all evidence beneath --output; records pending inspection, never claims vision.
"""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_image_layout import FLOAT, placed, layout
from localoffice.word.render import WordRenderer


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    import pypdfium2 as pdfium
    store, plugin, original, revision = placed(root)
    evidence = []
    try:
        for label, props in [('before-inline', None), ('floating-square', FLOAT),
                             ('floating-top-bottom', {'wrap':'top_bottom'}),
                             ('floating-none', {'wrap':'none'}),
                             ('after-inline', {'mode':'inline'})]:
            if props:
                revision = layout(plugin, revision, props, label)['candidate_revision']
            rendered = await WordRenderer(store).render(path='report.docx', revision=revision)
            pdf_bytes = store.blob(rendered['pdf_hash'])
            (root/f'{label}.pdf').write_bytes(pdf_bytes)
            (root/f'{label}.docx').write_bytes(store.blob(revision))
            images = []
            document = pdfium.PdfDocument(pdf_bytes)
            try:
                for index in range(len(document)):
                    page = document[index]
                    bitmap = page.render(scale=1.25)
                    image = root/f'{label}-page-{index+1}.png'
                    bitmap.to_pil().save(image)
                    images.append(str(image))
                    bitmap.close(); page.close()
            finally:
                document.close()
            evidence.append(dict(label=label, revision=revision, pdf_hash=rendered['pdf_hash'],
                                 images=images, status='rendered_pending_inspection', reviewer=None))
        report = dict(status='rendered_pending_inspection', evidence=evidence,
                      source_sha256=hashlib.sha256(original).hexdigest(),
                      original_preserved=(root/'report.docx').read_bytes()==original)
        (root/'render-evidence.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report))
    finally:
        store.close()


if __name__ == '__main__':
    asyncio.run(main())
