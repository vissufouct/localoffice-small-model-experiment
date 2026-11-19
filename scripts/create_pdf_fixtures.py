"""Generate small synthetic fixtures only; no private source material."""
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
from PIL import Image,ImageDraw,ImageFont
from pypdf import PdfReader,PdfWriter

root=Path('tests/fixtures/pdf');root.mkdir(parents=True,exist_ok=True)
c=Canvas(str(root/'text.pdf'));c.setTitle('Cedar source fixture')
c.setFont('Helvetica-Bold',20);c.drawString(60,770,'Cedar project brief')
c.setFont('Helvetica',12)
for y,line in [(725,'Project: Cedar.'),(700,'Budget: EUR 120.'),(675,'Owner: Ana.'),(625,'This synthetic source is used for local harness tests.')]:c.drawString(60,y,line)
c.showPage();c.setFont('Helvetica',14);c.drawString(60,750,'Delivery date: 20 November 2026.');c.save()
font_path='/System/Library/Fonts/Supplemental/Arial.ttf'
font=ImageFont.truetype(font_path,36)
for language,lines in [('english',['SCANNED SOURCE','Project: Cedar','Budget: EUR 120','Owner: Ana']),('portuguese',['DOCUMENTO DIGITALIZADO','Projecto: Cedro','Orçamento: 120 euros','Responsável: Ana'])]:
 image=Image.new('RGB',(1200,1500),'white');draw=ImageDraw.Draw(image)
 for i,line in enumerate(lines):draw.text((90,130+i*100),line,font=font,fill='#20382d')
 c=Canvas(str(root/f'scan-{language}.pdf'));c.drawImage(ImageReader(image),0,0,width=595,height=744);c.save()
writer=PdfWriter();writer.add_page(PdfReader(root/'text.pdf').pages[0]);writer.add_page(PdfReader(root/'scan-portuguese.pdf').pages[0])
with (root/'mixed.pdf').open('wb') as out:writer.write(out)
writer=PdfWriter();writer.add_page(PdfReader(root/'text.pdf').pages[0]);writer.encrypt('fixture-password')
with (root/'encrypted.pdf').open('wb') as out:writer.write(out)
(root/'malformed.pdf').write_bytes(b'%PDF-1.7\nmalformed fixture without xref or pages\n')
print('Created synthetic text, English/Portuguese scans, mixed, encrypted and malformed fixtures.')
