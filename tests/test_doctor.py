import json
import os

import pytest

from backend import doctor

SECRETS = {
    "GEMINI_API_KEY": "sekret-gemini-key-123",
    "JARVIS_SEARCH_API_KEY": "sekret-search-key-456",
    "JARVIS_AUTH_SIGNING_KEY": "sekret-signing-key-" + "x" * 20,
    "OPENAI_API_KEY": "sekret-openai-key-789",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        keys = ("GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY")
        if name.startswith("JARVIS_") or name in keys:
            monkeypatch.delenv(name)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "x.sqlite3"))


def by_area(host="127.0.0.1"):
    return {c.area: c for c in doctor.run_checks(host=host)}


def set_gemini(monkeypatch):
    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", SECRETS["GEMINI_API_KEY"])
    monkeypatch.setenv("JARVIS_GEMINI_MODEL", "gemini-test-model")


def test_defaults_are_off_and_ok_exit(capsys):
    checks = by_area()
    assert [checks[a].status for a in ("chat", "research", "router", "login")] == ["OFF"] * 4
    assert doctor.main([]) == 0


def test_gemini_complete_and_incomplete(monkeypatch):
    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "gemini")
    assert by_area()["chat"].code == "API_KEY_MISSING"
    monkeypatch.setenv("GEMINI_API_KEY", SECRETS["GEMINI_API_KEY"])
    assert by_area()["chat"].code == "MODEL_MISSING"
    monkeypatch.setenv("JARVIS_GEMINI_MODEL", "bad model!")
    assert by_area()["chat"].code == "MODEL_MISSING"
    monkeypatch.setenv("JARVIS_GEMINI_MODEL", "gemini-test-model")
    assert (by_area()["chat"].status, by_area()["chat"].code) == ("OK", "READY")


def test_groq_complete_and_incomplete(monkeypatch):
    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "groq")
    assert by_area()["chat"].code == "API_KEY_MISSING"
    monkeypatch.setenv("GROQ_API_KEY", "fake-key")
    assert by_area()["chat"].code == "MODEL_MISSING"
    monkeypatch.setenv("JARVIS_GROQ_MODEL", "bad model!")
    assert by_area()["chat"].code == "MODEL_MISSING"
    monkeypatch.setenv("JARVIS_GROQ_MODEL", "vendor/model-x")
    chat = by_area()["chat"]
    assert (chat.status, chat.code) == ("OK", "READY")
    assert "fake-key" not in chat.detail and "vendor/model-x" not in chat.detail


def test_research_requirements(monkeypatch):
    monkeypatch.setenv("JARVIS_RESEARCH_ENABLED", "1")
    assert by_area()["research"].code == "SEARCH_PROVIDER_MISSING"
    monkeypatch.setenv("JARVIS_SEARCH_PROVIDER", "tavily")
    assert doctor.run_checks()[0].code == "CONFIG_INVALID"  # key required by Settings
    monkeypatch.setenv("JARVIS_SEARCH_API_KEY", SECRETS["JARVIS_SEARCH_API_KEY"])
    assert by_area()["research"].code == "CHAT_PROVIDER_MISSING"
    set_gemini(monkeypatch)
    assert by_area()["research"].status == "OK"


def test_invalid_values_become_config_invalid(monkeypatch):
    for name, value in [
        ("JARVIS_ROUTER", "bogus"),
        ("JARVIS_CHAT_RESEARCH_LEVEL", "extensive"),
        ("JARVIS_SEARCH_MONTHLY_LIMIT", "0"),
        ("JARVIS_LLM_PROVIDER", "nope"),
    ]:
        monkeypatch.setenv(name, value)
        checks = doctor.run_checks()
        assert [(c.status, c.code) for c in checks] == [("INCOMPLETE", "CONFIG_INVALID")]
        assert doctor.main([]) == 1
        monkeypatch.delenv(name)


def test_router_and_chat_research(monkeypatch):
    monkeypatch.setenv("JARVIS_ROUTER", "llm")
    assert by_area()["router"].code == "ROUTER_NEEDS_CHAT_PROVIDER"
    monkeypatch.setenv("JARVIS_ROUTER", "rule")
    assert by_area()["router"].status == "OK"
    assert by_area()["chat_research"].code == "CHAT_RESEARCH_OFF"
    monkeypatch.setenv("JARVIS_RESEARCH_ENABLED", "true")
    assert by_area()["chat_research"].code == "RESEARCH_NOT_READY"
    monkeypatch.setenv("JARVIS_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("JARVIS_SEARCH_API_KEY", SECRETS["JARVIS_SEARCH_API_KEY"])
    set_gemini(monkeypatch)
    assert by_area()["chat_research"].status == "OK"
    monkeypatch.setenv("JARVIS_ROUTER", "off")
    assert by_area()["chat_research"].code == "ROUTER_OFF"


def test_login_and_bind(monkeypatch):
    assert by_area("0.0.0.0")["login"].code == "BIND_REQUIRES_AUTH"
    assert doctor.main(["--host", "0.0.0.0"]) == 1
    from backend.auth.passwords import hash_passphrase

    monkeypatch.setenv("JARVIS_AUTH_PASSPHRASE_HASH", hash_passphrase("correct horse battery"))
    assert by_area("0.0.0.0")["login"].code == "SIGNING_KEY_EPHEMERAL"
    monkeypatch.setenv("JARVIS_AUTH_SIGNING_KEY", SECRETS["JARVIS_AUTH_SIGNING_KEY"])
    assert by_area("0.0.0.0")["login"].code == "READY"
    monkeypatch.setenv("JARVIS_AUTH_COOKIE_SECURE", "false")
    assert by_area("0.0.0.0")["login"].code == "BIND_REQUIRES_SECURE_COOKIE"
    assert by_area()["login"].code == "COOKIE_INSECURE_LOOPBACK"


def test_paths(monkeypatch, tmp_path):
    assert by_area()["db"].code == "DB_WILL_BE_CREATED"
    (tmp_path / "x.sqlite3").write_bytes(b"")
    assert by_area()["db"].code == "EXISTS"
    monkeypatch.setenv("JARVIS_MEMORY_VAULT_PATH", str(tmp_path / "nope"))
    assert (by_area()["vault"].status, by_area()["vault"].code) == ("INCOMPLETE", "PATH_MISSING")
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("JARVIS_MEMORY_VAULT_PATH", str(vault))
    assert by_area()["vault"].status == "OK"
    monkeypatch.setenv("JARVIS_PERSONALITY_PATH", str(tmp_path / "missing.json"))
    assert by_area()["personality"].code == "PATH_MISSING"


def test_doctor_does_not_write(monkeypatch, tmp_path):
    before = sorted(p.name for p in tmp_path.iterdir())
    doctor.main([])
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_json_output_and_no_secret_or_path_leak(monkeypatch, tmp_path, capsys):
    set_gemini(monkeypatch)
    vault = tmp_path / "private-vault-dir"
    vault.mkdir()
    monkeypatch.setenv("JARVIS_MEMORY_VAULT_PATH", str(vault))
    monkeypatch.setenv("JARVIS_RESEARCH_ENABLED", "1")
    monkeypatch.setenv("JARVIS_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("JARVIS_ROUTER", "rule")
    monkeypatch.setenv("JARVIS_AUTH_SIGNING_KEY", SECRETS["JARVIS_AUTH_SIGNING_KEY"])
    monkeypatch.setenv("OPENAI_API_KEY", SECRETS["OPENAI_API_KEY"])
    for with_key in (False, True):  # also covers the CONFIG_INVALID path
        if with_key:
            monkeypatch.setenv("JARVIS_SEARCH_API_KEY", SECRETS["JARVIS_SEARCH_API_KEY"])
        for args in ([], ["--json"], ["--host", "0.0.0.0"]):
            doctor.main(args)
            out = capsys.readouterr().out
            for secret in SECRETS.values():
                assert secret not in out
            assert "gemini-test-model" not in out
            assert str(tmp_path) not in out and "private-vault-dir" not in out
    doctor.main(["--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert {c["area"] for c in data["checks"]} >= {"chat", "research", "router", "login", "vault"}
