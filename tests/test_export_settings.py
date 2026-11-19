from localoffice.server import Host

def test_export_settings_override(monkeypatch, tmp_path):
    location = tmp_path / "isolated-demo"
    monkeypatch.setenv("LOCALOFFICE_SETTINGS_DIR", str(location))
    host = Host()
    assert host.settings == location
    assert location.is_dir()
