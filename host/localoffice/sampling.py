"""Explicit development presets; neither preset grants model qualification."""
from .errors import HarnessError

PRESETS={
    'low_temperature': {'temperature':0.2,'top_p':1.0,'top_k':0,'min_p':0.0},
    'qwen_recommended': {'temperature':0.7,'top_p':0.8,'top_k':20,'min_p':0.0},
}


def settings(name='low_temperature'):
    if name not in PRESETS:
        raise HarnessError('sampling_profile','Choose a registered sampling profile.')
    return dict(PRESETS[name])


def sampler_arguments(name='low_temperature'):
    values=settings(name)
    return {'temp':values.pop('temperature'),**values}
