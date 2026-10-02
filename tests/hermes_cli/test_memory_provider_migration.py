"""A memory provider that left core is installed from the catalog, config untouched; a provider the
catalog does not know is reported with the one-liner instead of silently dropping memory."""

from pathlib import Path

import pytest

from hermes_cli import memory_provider_migration as mig


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("memory:\n  provider: honcho\n  honcho:\n    workspace: keep-me\n")
    monkeypatch.setattr(mig, "provider_present", lambda name, home: (home / "plugins" / name).is_dir())
    return tmp_path


def test_missing_provider_installs_its_catalog_plugin_and_keeps_config(home, monkeypatch):
    monkeypatch.setattr(mig, "catalog_source", lambda name: name)
    calls: list[str] = []
    said: list[str] = []

    def fake_install(name: str) -> dict:
        calls.append(name)
        (home / "plugins" / name).mkdir(parents=True)
        return {"ok": True}

    assert mig.migrate_home(home, install=fake_install, say=said.append) == "honcho"
    assert calls == ["honcho"]
    assert "settings and data are unchanged" in said[0]
    assert "workspace: keep-me" in (home / "config.yaml").read_text()
    # present now → nothing to do, nothing said
    assert mig.migrate_home(home, install=fake_install, say=said.append) is None
    assert calls == ["honcho"]


def test_presence_is_checked_in_the_home_being_migrated(tmp_path, monkeypatch):
    """The update hook walks several profile homes from one process; a provider installed in profile B
    must count as present for B even when the process-level home (A) lacks it. Real lookup, no mock."""
    a, b = tmp_path / "a", tmp_path / "b"
    for h in (a, b):
        h.mkdir(); (h / "config.yaml").write_text("memory:\n  provider: twin\n")
    (b / "plugins" / "twin").mkdir(parents=True)
    (b / "plugins" / "twin" / "__init__.py").write_text("class Twin(MemoryProvider): ...\n")
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setattr(mig, "catalog_source", lambda name: name)
    installs: list[Path] = []
    assert mig.migrate_home(b, install=lambda n: installs.append(b) or {"ok": True}, say=lambda s: None) is None
    assert installs == []
    assert mig.migrate_home(a, install=lambda n: installs.append(a) or {"ok": True}, say=lambda s: None) == "twin"


def test_provider_unknown_to_catalog_is_reported_not_installed(home, monkeypatch):
    monkeypatch.setattr(mig, "catalog_source", lambda name: None)
    said: list[str] = []
    assert mig.migrate_home(home, install=lambda n: pytest.fail("must not install"), say=said.append) is None
    assert "not in the plugin catalog" in said[0] and "memory.provider" in said[0]


def test_startup_recovery_attempts_each_profile_home(tmp_path, monkeypatch):
    """One multiplexed process can start agents for two homes missing the same provider."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from pm import install as pm_install

    homes = [tmp_path / "a", tmp_path / "b"]
    for profile_home in homes:
        profile_home.mkdir()
        (profile_home / "config.yaml").write_text("memory:\n  provider: twin\n", encoding="utf-8")
    monkeypatch.setattr(mig, "_attempted", set())
    monkeypatch.setattr(mig, "catalog_source", lambda name: name)
    monkeypatch.setattr(pm_install, "lazy_installs_allowed", lambda: True)
    installed = []

    def fake_installer(profile_home):
        def install(name):
            plugin_dir = profile_home / "plugins" / name
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "__init__.py").write_text("class Twin(MemoryProvider): ...\n", encoding="utf-8")
            installed.append(profile_home)
            return {"ok": True}

        return install

    monkeypatch.setattr(mig, "_install_into", fake_installer)
    outcomes = []
    for profile_home in (homes[0], homes[1], homes[0]):
        token = set_hermes_home_override(profile_home)
        try:
            outcomes.append(mig.recover_at_startup("twin"))
        finally:
            reset_hermes_home_override(token)

    assert outcomes == [True, True, False]
    assert installed == homes


def test_missing_catalog_provider_recovery_names_the_profile_install_command(tmp_path, monkeypatch, capsys):
    """Offline, lazy installs off, or a failed migration: every surface a user turns to (doctor,
    ``memory status``, ``memory setup honcho``, ``hermes honcho``) names the one command that installs
    the catalog plugin into THIS profile. Real in-tree catalog, nothing installed."""
    from types import SimpleNamespace
    from hermes_cli import doctor_state, memory_setup
    from hermes_cli._parser import build_top_level_parser

    home = tmp_path / "profiles" / "work"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("memory:\n  provider: honcho\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    want = "hermes -p work plugins install honcho"

    doctor_state._memory_provider_generic("honcho")
    memory_setup.cmd_status(SimpleNamespace())
    memory_setup.cmd_setup_provider("honcho")
    assert capsys.readouterr().out.count(want) == 3
    parser, _subparsers, _chat = build_top_level_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["honcho", "status"])
    assert want in capsys.readouterr().err


def test_recovery_copy_without_a_catalog_memory_entry_keeps_the_generic_hint(tmp_path, monkeypatch, capsys):
    from hermes_cli import doctor_state

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # not a profile home: no -p to add
    assert mig.catalog_install_hint("honcho", category="memory") == "hermes plugins install honcho"
    assert mig.catalog_install_hint("honcho", category="tools") is None
    assert mig.catalog_install_hint("no-such-provider") is None
    doctor_state._memory_provider_generic("no-such-provider")
    assert "run: hermes memory setup" in capsys.readouterr().out
