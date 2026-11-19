"""Register existing pinned MLX weights for research scripts; no downloads or inference."""
import argparse
import json
from pathlib import Path
PIN = '50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b'
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('path', type=Path)
a = p.parse_args(); path = a.path.expanduser().resolve()
if not path.is_dir() or path.name != PIN or not (path/'config.json').is_file():
    p.error('Use the existing pinned model snapshot directory, named with revision '+PIN)
root = Path(__file__).resolve().parents[1]
target = root/'docs/benchmarks/approved-mlx-download.json'
data = json.loads(target.read_text())
data['local_path'] = str(path); data['model']['path'] = str(path); data['model']['id'] = str(path)
data['model']['state'] = 'local_user_selected'
data['registration_note'] = 'User-selected existing directory; this does not verify weight hashes or compatibility.'
target.write_text(json.dumps(data, indent=2)+'\n')
print('Registered local path. This file now contains a personal path: do not commit it.')
