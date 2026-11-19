import pytest
from localoffice.sampling import settings,sampler_arguments
from localoffice.providers import MLXProvider
from localoffice.errors import HarnessError


def test_sampling_presets_are_explicit_and_not_shared_mutable_state():
    assert sampler_arguments()=={'temp':.2,'top_p':1.,'top_k':0,'min_p':0.}
    assert sampler_arguments('qwen_recommended')=={'temp':.7,'top_p':.8,'top_k':20,'min_p':0.}
    a=settings();a['temperature']=2
    assert settings()['temperature']==.2
    with pytest.raises(HarnessError):MLXProvider(sampling_profile='unknown')
    assert MLXProvider(sampling_profile='qwen_recommended').status()['sampling']==settings('qwen_recommended')
