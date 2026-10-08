"""Passphrase hashing, signed session tokens, the login limiter, config and startup refusal."""

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.auth import hash_password
from backend.auth.limiter import LoginLimiter
from backend.auth.passwords import (
    PasswordHashError,
    hash_passphrase,
    parse_hash,
    verify_passphrase,
)
from backend.auth.session import SessionSigner
from backend.core.config import ConfigError, Settings
from backend.serve import StartupRefused, check_bind, is_loopback_host, main

PASSPHRASE = "fake passphrase for tests only"
CHEAP = {"n": 16, "r": 1, "p": 1}  # fast parameters; the production defaults are used once below
SIGNING = "s" * 40


@pytest.fixture(scope="module")
def encoded() -> str:
    return hash_passphrase(PASSPHRASE, **CHEAP)


# --- scrypt hashing ----------------------------------------------------------------------------


def test_default_parameters_round_trip_and_format() -> None:
    value = hash_passphrase(PASSPHRASE)
    scheme, n, r, p, salt, digest = value.split("$")
    assert (scheme, int(n), int(r), int(p)) == ("scrypt", 2**15, 8, 3)
    assert len(salt) >= 16 and len(digest) >= 32
    assert verify_passphrase(PASSPHRASE, value)
    assert not verify_passphrase(PASSPHRASE + "x", value)


def test_hash_is_salted_and_never_contains_the_passphrase(encoded: str) -> None:
    other = hash_passphrase(PASSPHRASE, **CHEAP)
    assert other != encoded
    assert verify_passphrase(PASSPHRASE, encoded) and verify_passphrase(PASSPHRASE, other)
    assert PASSPHRASE not in encoded and base64.urlsafe_b64encode(PASSPHRASE.encode()) not in (
        encoded.encode()
    )


def test_wrong_passphrase_and_near_misses_fail(encoded: str) -> None:
    for attempt in ("", " ", PASSPHRASE.upper(), PASSPHRASE + " ", PASSPHRASE[:-1], "x" * 2000):
        assert not verify_passphrase(attempt, encoded)


def test_unicode_is_normalized_so_input_methods_agree() -> None:
    wide = "ｐａｓｓｐｈｒａｓｅ－ｔｅｓｔ－１２３"  # full-width forms
    value = hash_passphrase(wide, **CHEAP)
    assert verify_passphrase("passphrase-test-123", value)
    assert verify_passphrase(wide, value)
    japanese = "これはテスト用のパスフレーズです"
    value = hash_passphrase(japanese, **CHEAP)
    assert verify_passphrase(japanese, value) and not verify_passphrase(japanese[:-1], value)


@pytest.mark.parametrize(
    "bad", ["", "short", "a" * 11, " " * 11, "x" * 1025, "valid-passphrase\x00"]
)
def test_policy_rejects_weak_or_odd_passphrases(bad: str) -> None:
    with pytest.raises(PasswordHashError):
        hash_passphrase(bad, **CHEAP)


def test_tampered_or_malformed_hashes_never_verify_and_never_raise(encoded: str) -> None:
    scheme, n, r, p, salt, digest = encoded.split("$")
    flipped = ("A" if digest[0] != "A" else "B") + digest[1:]
    tampered = [
        "$".join([scheme, n, r, p, salt, flipped]),
        "$".join(["scrypt2", n, r, p, salt, digest]),
        "$".join([scheme, "17", r, p, salt, digest]),  # N not a power of two
        "$".join([scheme, str(2**30), r, p, salt, digest]),  # memory bomb
        "$".join([scheme, n, "0", p, salt, digest]),
        "$".join([scheme, n, "999", p, salt, digest]),
        "$".join([scheme, n, r, "999", salt, digest]),
        "$".join([scheme, n, r, p, "!!", digest]),
        "$".join([scheme, n, r, p, salt, ""]),
        "$".join([scheme, n, r, p, "AA", digest]),  # salt too short
        "$".join([scheme, n, r, p, salt]),
        "$".join([scheme, n, r, p, salt, digest, "extra"]),
        "$".join([scheme, "-16", r, p, salt, digest]),
        "$".join([scheme, "1" * 40, r, p, salt, digest]),
        "",
        "scrypt",
        "$" * 5,
        "\x00",
        "x" * 5000,
    ]
    for value in tampered:
        assert not verify_passphrase(PASSPHRASE, value), value[:30]
    assert verify_passphrase(PASSPHRASE, encoded)


def test_verify_survives_non_string_inputs(encoded: str) -> None:
    assert not verify_passphrase(None, encoded)  # type: ignore[arg-type]
    assert not verify_passphrase(PASSPHRASE, None)  # type: ignore[arg-type]
    assert not verify_passphrase(b"bytes", encoded)  # type: ignore[arg-type]


def test_parse_hash_does_not_echo_its_input() -> None:
    with pytest.raises(PasswordHashError) as info:
        parse_hash("scrypt$16$1$1$secretmarker$secretmarker")
    assert "secretmarker" not in str(info.value)


# --- hash_password CLI -------------------------------------------------------------------------


def _prompts(*answers: str):
    queue = iter(answers)
    return lambda _label: next(queue)


def test_cli_prints_only_the_hash(capsys: pytest.CaptureFixture[str]) -> None:
    assert hash_password.main(_prompts(PASSPHRASE, PASSPHRASE)) == 0
    out, err = capsys.readouterr()
    assert err == "" and PASSPHRASE not in out
    assert verify_passphrase(PASSPHRASE, out.strip()) and out.count("\n") == 1


@pytest.mark.parametrize(
    "answers", [("short", "short"), (PASSPHRASE, PASSPHRASE + "!"), ("a" * 5000, "a" * 5000)]
)
def test_cli_rejects_short_mismatched_or_huge_input(
    capsys: pytest.CaptureFixture[str], answers: tuple[str, str]
) -> None:
    assert hash_password.main(_prompts(*answers)) == 1
    out, err = capsys.readouterr()
    assert out == "" and answers[0] not in err


def test_cli_does_not_read_argv_or_env() -> None:
    source = Path(hash_password.__file__).read_text(encoding="utf-8")
    assert (
        "sys.argv" not in source
        and "os.environ" not in source
        and "import os" not in source
        and "getpass" in source
    )


def test_cli_module_runs_and_prompts_through_getpass(tmp_path: Path) -> None:
    # No tty is available under pytest; getpass falls back to stdin, which is enough to prove the
    # module entry point works and prints only the hash.
    result = subprocess.run(
        [sys.executable, "-m", "backend.auth.hash_password"],
        input=f"{PASSPHRASE}\n{PASSPHRASE}\n",
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0
    assert verify_passphrase(PASSPHRASE, result.stdout.strip())
    assert PASSPHRASE not in result.stdout


# --- session tokens ----------------------------------------------------------------------------


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def signer(
    clock: Clock, hours: int = 1, key: str = SIGNING, bound: str = "hash-a"
) -> SessionSigner:
    return SessionSigner(key, bound, hours * 3600, clock)


def test_valid_token_expires_exactly_at_its_absolute_lifetime() -> None:
    clock = Clock()
    s = signer(clock)
    token = s.issue()
    assert s.verify(token)
    clock.now += 3599
    assert s.verify(token)
    clock.now += 1
    assert not s.verify(token)
    clock.now += 10**6
    assert not s.verify(token)


def test_tokens_are_unique_and_do_not_slide() -> None:
    clock = Clock()
    s = signer(clock)
    assert s.issue() != s.issue()
    token = s.issue()
    for _ in range(6):  # verifying never extends the lifetime
        clock.now += 700
        s.verify(token)
    assert not s.verify(token)


def test_signature_and_payload_tampering_is_rejected() -> None:
    clock = Clock()
    s = signer(clock)
    payload, signature = s.issue().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["exp"] += 10**6
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    for token in (f"{forged}.{signature}", f"{payload}.{flipped}", f"{payload}.", f".{signature}"):
        assert not s.verify(token)


def test_token_from_another_key_or_passphrase_hash_is_rejected() -> None:
    clock = Clock()
    token = signer(clock).issue()
    assert not signer(clock, key="t" * 40).verify(token)
    assert not signer(clock, bound="hash-b").verify(token)  # changing the passphrase logs out


def test_future_dated_and_overlong_tokens_are_rejected() -> None:
    clock = Clock()
    s = signer(clock)
    now = int(clock.now)
    assert s.verify(s.sign({"iat": now, "exp": now + 100, "nonce": "n"}))
    assert not s.verify(s.sign({"iat": now + 3600, "exp": now + 7200, "nonce": "n"}))
    assert not s.verify(s.sign({"iat": now - 5, "exp": now - 5, "nonce": "n"}))
    assert not s.verify(s.sign({"iat": now, "exp": now + 3601, "nonce": "n"}))  # beyond lifetime
    assert not s.verify(s.sign({"iat": now, "exp": now - 1, "nonce": "n"}))
    # A shortened configured lifetime also retires longer-lived tokens.
    long_lived = signer(clock, hours=24).issue()
    assert not signer(clock, hours=1).verify(long_lived)


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"iat": 1, "exp": 2},
        {"iat": "1700000000", "exp": 1800000000, "nonce": "n"},
        {"iat": True, "exp": True, "nonce": "n"},
        {"iat": 1.5, "exp": 1800000000, "nonce": "n"},
        {"iat": 1700000000, "exp": 1700000100, "nonce": ""},
        {"iat": 1700000000, "exp": 1700000100, "nonce": 5},
        [1, 2, 3],
        "text",
        None,
    ],
)
def test_validly_signed_but_malformed_claims_are_rejected(claims: object) -> None:
    assert not signer(Clock()).verify(signer(Clock()).sign(claims))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        ".",
        "..",
        "a.b.c",
        "!!.!!",
        "a" * 5000,
        "\x00",
        "é.é",
        "null",
        "{}",
        "e30.e30",
        None,
        5,
        b"x",
    ],
)
def test_garbage_tokens_are_rejected_without_raising(garbage: object) -> None:
    assert signer(Clock()).verify(garbage) is False


def test_non_json_payload_with_a_valid_signature_is_rejected() -> None:
    s = signer(Clock())
    payload = base64.urlsafe_b64encode(b"\xff\xfenot json").rstrip(b"=").decode()
    mac = s._mac(payload)
    token = f"{payload}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    assert not s.verify(token)


# --- limiter -----------------------------------------------------------------------------------


def limiter(clock: Clock, **options: float) -> LoginLimiter:
    return LoginLimiter(clock=clock, **options)  # type: ignore[arg-type]


def test_five_failures_then_exponentially_growing_lockouts_up_to_the_cap() -> None:
    clock = Clock(1000.0)
    lim = limiter(clock, global_limit=10_000)
    for _ in range(5):
        assert lim.begin("a").allowed
    expected = [30, 60, 120, 240, 480, 900, 900, 900]
    for seconds in expected:
        blocked = lim.begin("a")
        assert not blocked.allowed and blocked.retry_after == seconds
        clock.now += seconds - 1
        assert not lim.begin("a").allowed  # still locked one second early
        clock.now += 1
        assert lim.begin("a").allowed  # the lock expired; this attempt fails and lengthens it


def test_clients_are_independent_and_success_resets_only_that_client() -> None:
    clock = Clock()
    lim = limiter(clock)
    for _ in range(5):
        lim.begin("a")
    assert not lim.begin("a").allowed
    assert lim.begin("b").allowed
    clock.now += 31
    assert lim.begin("a").allowed
    lim.succeeded("a")
    for _ in range(5):
        assert lim.begin("a").allowed  # counting starts over after a success


def test_burst_of_guesses_cannot_exceed_the_limit() -> None:
    lim = limiter(Clock())
    allowed = sum(lim.begin("a").allowed for _ in range(200))
    assert allowed == 5


def test_old_failures_fade() -> None:
    clock = Clock()
    lim = limiter(clock)
    for _ in range(4):
        lim.begin("a")
    clock.now += 3601
    for _ in range(5):
        assert lim.begin("a").allowed


def test_global_budget_caps_distributed_guessing_and_recovers() -> None:
    clock = Clock()
    lim = limiter(clock, global_limit=10, global_window=900)
    for i in range(10):
        assert lim.begin(f"client-{i}").allowed
    denied = lim.begin("fresh-client")
    assert not denied.allowed and 0 < denied.retry_after <= 900
    clock.now += 900
    assert lim.begin("fresh-client").allowed


def test_state_is_bounded() -> None:
    lim = limiter(Clock(), max_clients=50, global_limit=10**9)
    for i in range(5000):
        lim.begin(f"client-{i}")
    assert len(lim._clients) <= 50


# --- configuration -----------------------------------------------------------------------------


def settings(**options: object) -> Settings:
    return Settings(db_path=Path("x.sqlite3"), **options)  # type: ignore[arg-type]


def test_defaults_leave_auth_off_and_secure_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HASH", "SIGNING_KEY", "SESSION_HOURS", "COOKIE_SECURE"):
        monkeypatch.delenv(f"JARVIS_AUTH_{name}", raising=False)
    monkeypatch.delenv("JARVIS_TRUSTED_PROXY", raising=False)
    monkeypatch.delenv("JARVIS_AUTH_PASSPHRASE_HASH", raising=False)
    current = Settings.from_env()
    assert not current.auth_enabled
    assert (current.auth_session_hours, current.auth_cookie_secure, current.trusted_proxy) == (
        168,
        True,
        False,
    )


def test_env_enables_auth(monkeypatch: pytest.MonkeyPatch, encoded: str) -> None:
    monkeypatch.setenv("JARVIS_AUTH_PASSPHRASE_HASH", encoded)
    monkeypatch.setenv("JARVIS_AUTH_SIGNING_KEY", SIGNING)
    monkeypatch.setenv("JARVIS_AUTH_SESSION_HOURS", "12")
    monkeypatch.setenv("JARVIS_AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("JARVIS_TRUSTED_PROXY", "1")
    current = Settings.from_env()
    assert current.auth_enabled and current.auth_session_hours == 12
    assert current.auth_cookie_secure is False and current.trusted_proxy is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("JARVIS_AUTH_PASSPHRASE_HASH", ""),
        ("JARVIS_AUTH_PASSPHRASE_HASH", "   "),
        ("JARVIS_AUTH_PASSPHRASE_HASH", "not-a-hash"),
        ("JARVIS_AUTH_SIGNING_KEY", ""),
        ("JARVIS_AUTH_SIGNING_KEY", "short"),
        ("JARVIS_AUTH_SESSION_HOURS", "0"),
        ("JARVIS_AUTH_SESSION_HOURS", "-4"),
        ("JARVIS_AUTH_SESSION_HOURS", "99999"),
        ("JARVIS_AUTH_SESSION_HOURS", "soon"),
        ("JARVIS_AUTH_COOKIE_SECURE", "maybe"),
        ("JARVIS_TRUSTED_PROXY", "2"),
    ],
)
def test_bad_auth_env_stops_startup_without_echoing_the_value(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as info:
        Settings.from_env()
    assert len(value.strip()) < 4 or value not in str(info.value)


def test_settings_repr_hides_credentials(encoded: str) -> None:
    text = repr(settings(auth_passphrase_hash=encoded, auth_signing_key=SIGNING))
    assert encoded not in text and SIGNING not in text


# --- startup refusal ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.5.5.5", True),
        ("::1", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("::ffff:127.0.0.1", True),
        ("0.0.0.0", False),
        ("::", False),
        ("", False),
        ("192.168.1.10", False),
        ("10.0.0.1", False),
        ("example.com", False),
        ("localhost.example.com", False),
        ("127.0.0.1.example.com", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_loopback_detection(host: str, loopback: bool) -> None:
    assert is_loopback_host(host) is loopback


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.0.5", "example.com", ""])
def test_non_loopback_bind_is_refused_without_auth(host: str) -> None:
    with pytest.raises(StartupRefused):
        check_bind(host, settings())


def test_non_loopback_bind_needs_secure_cookies(encoded: str) -> None:
    with pytest.raises(StartupRefused):
        check_bind("0.0.0.0", settings(auth_passphrase_hash=encoded, auth_cookie_secure=False))
    check_bind("0.0.0.0", settings(auth_passphrase_hash=encoded))
    check_bind("127.0.0.1", settings())
    check_bind("127.0.0.1", settings(auth_passphrase_hash=encoded, auth_cookie_secure=False))


def test_main_refuses_before_starting_a_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("JARVIS_AUTH_PASSPHRASE_HASH", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # importing it would fail: it must not run
    assert main(["--host", "0.0.0.0", "--port", "1"]) == 2
    assert "non-loopback" in capsys.readouterr().err


def test_main_reports_bad_config_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JARVIS_AUTH_PASSPHRASE_HASH", "garbage-value")
    assert main(["--host", "127.0.0.1"]) == 2
    err = capsys.readouterr().err
    assert "garbage-value" not in err and "Traceback" not in err


def test_main_starts_loopback_with_forwarded_headers_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("JARVIS_AUTH_PASSPHRASE_HASH", raising=False)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "serve.sqlite3"))
    calls: list[dict[str, object]] = []

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append(options))
    assert main(["--port", "18999"]) == 0
    assert calls == [
        {"host": "127.0.0.1", "port": 18999, "proxy_headers": False, "server_header": False}
    ]
