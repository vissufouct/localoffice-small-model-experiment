from localoffice import model_profiles as profiles
from localoffice.providers import MLXProvider


def fixture_model(tmp_path):
    path = tmp_path / 'snapshots' / profiles.DEVELOPMENT_REVISION
    path.mkdir(parents=True)
    for name in ('config.json', 'tokenizer.json', 'tokenizer_config.json', 'chat_template.jinja'):
        (path / name).write_text('{}')
    return {'path': str(path), 'name': profiles.DEVELOPMENT_MODEL,
            'quantization': {'bits': 4, 'group_size': 64}}


def test_loadable_is_never_recommended(tmp_path, monkeypatch):
    info = fixture_model(tmp_path)
    monkeypatch.setattr(profiles, 'DEVELOPMENT_TEMPLATE', profiles.metadata_digest(tmp_path / 'snapshots' / profiles.DEVELOPMENT_REVISION / 'chat_template.jinja'))
    report = profiles.runtime_profile(info, context=4096, structural_decoding=False)
    assert report['development_candidate_match']
    assert report['qualification'] == 'loadable'
    assert not report['recommended'] and not report['qualification_reports']
    assert report['settings']['native_tool_parser'] is None
    assert 'tensor contents not verified' in report['identity_evidence']


def test_fingerprint_tracks_template_context_and_decoder(tmp_path):
    info = fixture_model(tmp_path)
    def fingerprint(context=4096, constrained=False):
        return profiles.runtime_profile(info, context=context, structural_decoding=constrained)['fingerprint']
    baseline = fingerprint()
    assert baseline == fingerprint()
    assert baseline != fingerprint(2048)
    assert baseline != fingerprint(constrained=True)
    from pathlib import Path
    (Path(info['path']) / 'chat_template.jinja').write_text('changed template')
    assert baseline != fingerprint()
    assert not profiles.runtime_profile(info, context=4096, structural_decoding=False)['development_candidate_match']


def test_missing_and_oversized_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, 'METADATA_LIMIT', 4)
    assert profiles.metadata_digest(tmp_path / 'missing') is None
    (tmp_path / 'large').write_bytes(b'12345')
    assert profiles.metadata_digest(tmp_path / 'large') is None


def test_unloaded_provider_does_not_advertise_profile():
    provider = MLXProvider()
    provider.profile = {'recommended': True}
    assert provider.status()['profile'] is None
