"""Compile the deterministic LibreOffice UNO helper with the local verified JDK."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
homes=list((ROOT/'.runtime/java').glob('*/Contents/Home'))
if len(homes)!=1:raise SystemExit('Expected one verified project-local JDK; see docs/benchmarks/java-install.json.')
java=homes[0]
classes=ROOT/'workers/fields/classes';classes.mkdir(exist_ok=True)
jar=Path('/Applications/LibreOffice.app/Contents/Resources/java/libreoffice.jar')
if not jar.is_file():raise SystemExit('LibreOffice Java API is missing.')
subprocess.run([str(java/'bin/javac'),'--release','21','-cp',str(jar),'-d',str(classes),
                str(ROOT/'workers/fields/UpdateFields.java')],check=True)
print('Field worker compiled')
