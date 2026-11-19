import asyncio
from types import SimpleNamespace
import pytest
from localoffice.errors import HarnessError
from localoffice.providers import admit_memory


def test_admission_waits_for_reclamation_without_lowering_threshold(monkeypatch):
    readings=iter([100,150,300])
    monkeypatch.setattr('localoffice.providers.psutil.virtual_memory',lambda:SimpleNamespace(available=next(readings)))
    async def release(_):pass
    monkeypatch.setattr('localoffice.providers.asyncio.sleep',release)
    assert asyncio.run(admit_memory(200))==300


def test_admission_refuses_persistent_shortage_and_oversize(monkeypatch):
    monkeypatch.setattr('localoffice.providers.psutil.virtual_memory',lambda:SimpleNamespace(available=100))
    with pytest.raises(HarnessError,match='screening requires'):
        asyncio.run(admit_memory(200,settle_seconds=0))
    monkeypatch.setattr('localoffice.providers.psutil.virtual_memory',lambda:SimpleNamespace(available=20*2**30))
    with pytest.raises(HarnessError,match='screening requires'):
        asyncio.run(admit_memory(12*2**30))


def test_admission_wait_can_be_cancelled(monkeypatch):
    monkeypatch.setattr('localoffice.providers.psutil.virtual_memory',lambda:SimpleNamespace(available=100))
    async def cancel(_):raise asyncio.CancelledError
    monkeypatch.setattr('localoffice.providers.asyncio.sleep',cancel)
    with pytest.raises(asyncio.CancelledError):asyncio.run(admit_memory(200))
