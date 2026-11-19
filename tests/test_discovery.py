import json
from localoffice.discovery import inspect_model


def test_huggingface_snapshot_preserves_model_identity(tmp_path):
    folder=tmp_path/'models--mlx-community--Qwen3-4B-Instruct-2507-4bit'/'snapshots'/'abcdef'
    folder.mkdir(parents=True)
    (folder/'config.json').write_text(json.dumps({'model_type':'qwen3','quantization':{'bits':4,'group_size':64}}))
    (folder/'model.safetensors').write_bytes(b'fixture metadata only')
    model=inspect_model(folder)
    assert model['name']=='mlx-community/Qwen3-4B-Instruct-2507-4bit'
    assert model['parameters_b']==4
    assert model['parameter_evidence']=='label; not independently counted'
    assert model['backend']=='mlx'
