"""Read-only runtime provenance. Loading a model never grants qualification."""
from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

DEVELOPMENT_MODEL = 'mlx-community/Qwen3-4B-Instruct-2507-4bit'
DEVELOPMENT_REVISION = '50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b'
DEVELOPMENT_TEMPLATE = '40c21f34cf67d8c760ef72f8ad3ae5afad514299d4b06e91dd9a8d705af7b541'
METADATA_LIMIT = 32 * 1024 * 1024


def metadata_digest(path: Path) -> str | None:
    # Bounded reads even if files change after stat; never read tensor weights.
    try:
        with path.open('rb') as stream:
            data = stream.read(METADATA_LIMIT + 1)
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest() if len(data) <= METADATA_LIMIT else None


def runtime_profile(info: dict, *, context: int, structural_decoding: bool, native_tool_protocol: bool = False, native_constrained: bool = False, sampling: dict | None = None) -> dict:
    path = Path(info['path'])
    artifacts = {name: metadata_digest(path / name) for name in
                 ('config.json', 'tokenizer.json', 'tokenizer_config.json', 'chat_template.jinja')}
    try:
        runtime_version = version('mlx-lm')
    except PackageNotFoundError:
        runtime_version = None
    revision = path.name if path.parent.name == 'snapshots' else None
    candidate = (info.get('name') == DEVELOPMENT_MODEL and revision == DEVELOPMENT_REVISION
                 and artifacts['chat_template.jinja'] == DEVELOPMENT_TEMPLATE
                 and info.get('quantization', {}).get('bits') == 4 and context == 4096)
    settings = {
        'model': info.get('name'), 'revision': revision, 'backend': 'mlx',
        'quantization': info.get('quantization'), 'runtime_version': runtime_version,
        'metadata_sha256': artifacts, 'context_limit': context,
        'protocol': 'native_qwen_experimental' if native_tool_protocol else 'legacy_structured_action', 'native_tool_parser': 'localoffice.native_tools.parse_qwen_call' if native_tool_protocol else None,
        'decoding': 'experimental_native_outlines' if native_constrained else 'experimental_outlines_structure' if structural_decoding else 'unconstrained',
        'sampling': dict(sampling) if sampling is not None else {'temperature':0.2,'top_p':1.0,'top_k':0,'min_p':0.0},
    }
    fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {
        'manifest_version': 1, 'fingerprint': fingerprint, 'settings': settings,
        'development_candidate_match': candidate,
        'identity_evidence': 'local metadata and snapshot path; tensor contents not verified',
        'qualification': 'loadable', 'recommended': False, 'qualification_reports': [],
        'limitations': [('Native protocol is experimental' if native_tool_protocol else 'Legacy structured-action protocol'), 'Office workload qualification pending',
                        'Physical 16 GB qualification pending'],
    }
