"""Read-only configuration readiness check: ``python -m backend.doctor [--host HOST] [--json]``.

Shows which features the current environment would activate for ``python -m backend.serve``.
It makes no network calls, writes nothing and never prints a value taken from the environment:
credentials, paths and models are reported only as fixed words and reason codes. The rules come
from the same code the server uses (``Settings``, ``check_bind``, the vault and personality
loaders), so the doctor follows real behavior instead of copying it.
"""

import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from backend.core.config import ConfigError, Settings
from backend.personality.settings import PersonalityError, load_personality
from backend.serve import StartupRefused, check_bind, is_loopback_host

OK = "OK"
OFF = "OFF"
INCOMPLETE = "INCOMPLETE"

# Fixed vocabulary: status line text per reason code. Nothing here is built from the environment.
REASONS: dict[str, str] = {
    "READY": "利用できます",
    "NOT_CONFIGURED": "設定されていないため無効です",
    "CONFIG_INVALID": "設定値が不正です(JARVIS が起動を拒否します)",
    "API_KEY_MISSING": "APIキーが未設定です",
    "MODEL_MISSING": "モデル名が未設定または不正です",
    "DEPENDENCY_MISSING": "必要なパッケージが未インストールです",
    "RESEARCH_DISABLED": "JARVIS_RESEARCH_ENABLED が無効です",
    "SEARCH_PROVIDER_MISSING": "検索プロバイダが未設定です",
    "CHAT_PROVIDER_MISSING": "チャットプロバイダが未設定です",
    "MODEL_CHOICE_UNAVAILABLE": "選択肢のうち、キーまたはパッケージが揃っていないものがあります",
    "ROUTER_OFF": "ルーターが off です",
    "RESEARCH_NOT_READY": "リサーチの準備ができていません",
    "ROUTER_NEEDS_CHAT_PROVIDER": "llm ルーターにはチャットプロバイダが必要です",
    "CASUAL_NEEDS_ROUTER": "雑談経路にはルーター(JARVIS_ROUTER)が必要です",
    "CASUAL_NEEDS_CHAT_PROVIDER": "雑談経路にはチャットプロバイダが必要です",
    "CHAT_RESEARCH_OFF": "チャットからのリサーチは無効です",
    "AUTH_DISABLED": "ログインは無効です(ループバック専用)",
    "BIND_REQUIRES_AUTH": "非ループバック待受にはログインが必要です",
    "BIND_REQUIRES_SECURE_COOKIE": "非ループバック待受には Secure Cookie が必要です",
    "SIGNING_KEY_EPHEMERAL": "有効です(署名鍵が未設定で、再起動ごとにセッションが切れます)",
    "COOKIE_INSECURE_LOOPBACK": "有効です(Secure Cookie が無効のためループバック専用)",
    "PATH_MISSING": "指定されたパスが存在しません",
    "PATH_INVALID": "指定された内容が不正です",
    "DB_WILL_BE_CREATED": "DB は未作成です(初回起動時に作成されます)",
    "EXISTS": "存在します",
    "AUTO_APPROVE_ON": (
        "有効です: 調査由来の記憶は人間のレビューなしで自動承認されます(オーナー判断の例外)"
    ),
    "AUTO_STAGE_ONLY": "有効です: 完了した調査の主張を承認待ちとして登録します(承認は人間)",
    "CHAT_AUTO_APPROVE_ON": (
        "有効です: 会話の発言から作られた記憶は人間のレビューなしで自動承認されます"
        "(オーナー判断の例外)。発言ごとに追加のモデル呼び出しが発生します"
    ),
    "CHAT_AUTO_STAGE_ONLY": (
        "有効です: 会話の発言から記憶の候補を自動作成します(承認は人間)。"
        "発言ごとに追加のモデル呼び出しが発生します"
    ),
    "SHELL_ROOT_MISSING": "JARVIS_SHELL_ROOT が未設定です",
    "SHELL_ROOT_INVALID": "シェルの実行ルートが使えません(存在しない、/ やホームを含む等)",
    "SHELL_COMMAND_UNKNOWN": "JARVIS_SHELL_COMMANDS に許可表にない名前があります",
    "SHELL_NO_COMMANDS": "許可表のコマンドが実行環境に見つかりません",
    "SHELL_UNSUPPORTED": "このOSではシェルツールを使えません",
    "SHELL_POLICY_INVALID": "シェル許可表の内容が不正です",
}


@dataclass(frozen=True)
class Check:
    area: str
    label: str
    status: str
    code: str
    detail: str = ""  # fixed words only, e.g. "set" / "not set"

    def as_dict(self) -> dict[str, str]:
        return {
            "area": self.area,
            "status": self.status,
            "code": self.code,
            "message": REASONS[self.code],
            "detail": self.detail,
        }


def _set(env: Mapping[str, str], name: str) -> bool:
    return bool(env.get(name, "").strip())


def _word(flag: bool) -> str:
    return "set" if flag else "not set"


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _model_valid(provider: str, model: str) -> bool:
    try:
        if provider == "groq":
            from backend.providers.groq import _MODEL_ID
        else:
            from backend.providers.gemini import _MODEL_ID
    except ImportError:
        return bool(model.strip())
    return bool(_MODEL_ID.fullmatch(model.strip()))


def _chat(settings: Settings, env: Mapping[str, str]) -> Check:
    provider = settings.llm_provider
    if provider == "none":
        return Check("chat", "チャット", OFF, "NOT_CONFIGURED", "provider=none")
    if provider == "gemini":
        key, model_name, module = "GEMINI_API_KEY", "JARVIS_GEMINI_MODEL", "httpx"
    elif provider == "groq":
        key, model_name, module = "GROQ_API_KEY", "JARVIS_GROQ_MODEL", "httpx"
    else:
        key, model_name, module = "OPENAI_API_KEY", "JARVIS_OPENAI_MODEL", "openai"
    detail = f"provider={provider}, {key}={_word(_set(env, key))}, " + (
        f"{model_name}={_word(_set(env, model_name))}"
    )
    if not _set(env, key):
        return Check("chat", "チャット", INCOMPLETE, "API_KEY_MISSING", detail)
    model = env.get(model_name, "")
    if not model.strip() or (provider in {"gemini", "groq"} and not _model_valid(provider, model)):
        return Check("chat", "チャット", INCOMPLETE, "MODEL_MISSING", detail)
    if not _installed(module):
        return Check("chat", "チャット", INCOMPLETE, "DEPENDENCY_MISSING", detail)
    return Check("chat", "チャット", OK, "READY", detail)


def _models(settings: Settings, env: Mapping[str, str], chat: Check) -> Check:
    """The selectable-model allowlist (``JARVIS_MODEL_CHOICES``). Counts only; no names, no keys."""
    from backend.providers.choices import provider_ready, split_choice

    total = len(settings.model_choices)
    if total == 0:
        return Check("models", "モデル選択", OFF, "NOT_CONFIGURED", "choices=0")
    if chat.status != OK:  # the default provider is what the app falls back to
        detail = f"choices={total}"
        return Check("models", "モデル選択", INCOMPLETE, "CHAT_PROVIDER_MISSING", detail)
    ready = sum(provider_ready(split_choice(entry)[0], env) for entry in settings.model_choices)
    detail = f"choices={total}, available={ready}"
    if ready < total:
        return Check("models", "モデル選択", INCOMPLETE, "MODEL_CHOICE_UNAVAILABLE", detail)
    return Check("models", "モデル選択", OK, "READY", detail)


def _research(settings: Settings, chat: Check) -> Check:
    # Same order as backend.api.app._build_research: switch, search provider, chat provider.
    detail = f"search={settings.search_provider}"
    if not settings.research_enabled:
        return Check("research", "リサーチ", OFF, "RESEARCH_DISABLED", detail)
    if settings.search_provider == "none":
        return Check("research", "リサーチ", INCOMPLETE, "SEARCH_PROVIDER_MISSING", detail)
    # Settings already guarantees a search key for a real provider (JARVIS_SEARCH_API_KEY).
    if chat.status != OK:
        return Check("research", "リサーチ", INCOMPLETE, "CHAT_PROVIDER_MISSING", detail)
    if not _installed("httpx"):
        return Check("research", "リサーチ", INCOMPLETE, "DEPENDENCY_MISSING", detail)
    return Check("research", "リサーチ", OK, "READY", detail + ", JARVIS_SEARCH_API_KEY=set")


def _research_memory(settings: Settings) -> Check:
    """Owner-approved exception (docs/memory.md): flags only, never any stored content."""
    auto_approve = settings.research_memory_auto_approve
    auto_stage = settings.research_memory_auto_stage
    detail = (
        "JARVIS_RESEARCH_MEMORY_AUTO_APPROVE=" + ("on" if auto_approve else "off") + ", "
        "JARVIS_RESEARCH_MEMORY_AUTO_STAGE=" + ("on" if auto_stage else "off") + ", "
        "JARVIS_MEMORY_VAULT_PATH=" + _word(settings.memory_vault_path is not None)
    )
    label = "調査記憶の自動承認"
    if auto_approve:
        return Check("research_memory", label, OK, "AUTO_APPROVE_ON", detail)
    if auto_stage:
        return Check("research_memory", label, OK, "AUTO_STAGE_ONLY", detail)
    return Check("research_memory", label, OFF, "NOT_CONFIGURED", detail)


def _chat_memory(settings: Settings, chat: Check) -> Check:
    """Owner-approved exception (docs/chat-auto-memory.md): flags only, never any content."""
    detail = (
        "JARVIS_CHAT_MEMORY_AUTO=" + ("on" if settings.chat_memory_auto else "off") + ", "
        "JARVIS_CHAT_MEMORY_AUTO_APPROVE="
        + ("on" if settings.chat_memory_auto_approve else "off")
        + ", "
        f"JARVIS_CHAT_MEMORY_DAILY_LIMIT={settings.chat_memory_daily_limit}, "
        f"JARVIS_CHAT_MEMORY_MIN_CHARS={settings.chat_memory_min_chars}, "
        "JARVIS_CHAT_MEMORY_PER_CONVERSATION_LIMIT="
        f"{settings.chat_memory_per_conversation_limit}, "
        f"JARVIS_CHAT_MEMORY_PER_DAY_LIMIT={settings.chat_memory_per_day_limit}, "
        "JARVIS_MEMORY_VAULT_PATH=" + _word(settings.memory_vault_path is not None)
    )
    label = "会話記憶の自動作成"
    if not settings.chat_memory_auto:
        return Check("chat_memory", label, OFF, "NOT_CONFIGURED", detail)
    if chat.status != OK:
        return Check("chat_memory", label, INCOMPLETE, "CHAT_PROVIDER_MISSING", detail)
    if settings.chat_memory_auto_approve:
        return Check("chat_memory", label, OK, "CHAT_AUTO_APPROVE_ON", detail)
    return Check("chat_memory", label, OK, "CHAT_AUTO_STAGE_ONLY", detail)


def _router(settings: Settings, chat: Check) -> Check:
    detail = f"router={settings.router}"
    if settings.router == "off":
        return Check("router", "ルーター", OFF, "NOT_CONFIGURED", detail)
    if settings.router == "llm" and chat.status != OK:
        return Check("router", "ルーター", INCOMPLETE, "ROUTER_NEEDS_CHAT_PROVIDER", detail)
    return Check("router", "ルーター", OK, "READY", detail)


def _casual(settings: Settings, router: Check, chat: Check) -> Check:
    detail = "casual=" + ("on" if settings.casual else "off")
    if not settings.casual:
        return Check("casual", "雑談経路", OFF, "NOT_CONFIGURED", detail)
    if settings.router == "off" or router.status != OK:
        return Check("casual", "雑談経路", INCOMPLETE, "CASUAL_NEEDS_ROUTER", detail)
    if chat.status != OK:
        return Check("casual", "雑談経路", INCOMPLETE, "CASUAL_NEEDS_CHAT_PROVIDER", detail)
    return Check("casual", "雑談経路", OK, "READY", detail)


def _chat_research(settings: Settings, router: Check, research: Check) -> Check:
    answer = "on" if settings.chat_research_answer else "off"
    fallback_main = "on" if settings.chat_research_fallback_main else "off"
    detail = (
        f"level={settings.chat_research_level}, answer={answer}, "
        f"timeout={settings.chat_research_answer_timeout_seconds}s, fallback_main={fallback_main}"
    )
    if settings.router == "off":
        return Check("chat_research", "チャットからのリサーチ", OFF, "ROUTER_OFF", detail)
    if not settings.research_enabled:
        return Check("chat_research", "チャットからのリサーチ", OFF, "CHAT_RESEARCH_OFF", detail)
    if router.status != OK or research.status != OK:
        return Check(
            "chat_research", "チャットからのリサーチ", INCOMPLETE, "RESEARCH_NOT_READY", detail
        )
    return Check("chat_research", "チャットからのリサーチ", OK, "READY", detail)


def _login(settings: Settings, env: Mapping[str, str], host: str) -> Check:
    detail = (
        f"JARVIS_AUTH_PASSPHRASE_HASH={_word(settings.auth_enabled)}, "
        f"JARVIS_AUTH_SIGNING_KEY={_word(settings.auth_signing_key is not None)}"
    )
    try:
        check_bind(host, settings)
    except StartupRefused:
        code = "BIND_REQUIRES_AUTH" if not settings.auth_enabled else "BIND_REQUIRES_SECURE_COOKIE"
        return Check("login", "ログイン", INCOMPLETE, code, detail)
    if not settings.auth_enabled:
        return Check("login", "ログイン", OFF, "AUTH_DISABLED", detail)
    if settings.auth_signing_key is None:
        return Check("login", "ログイン", OK, "SIGNING_KEY_EPHEMERAL", detail)
    if not settings.auth_cookie_secure:
        return Check("login", "ログイン", OK, "COOKIE_INSECURE_LOOPBACK", detail)
    return Check("login", "ログイン", OK, "READY", detail)


def _shell(settings: Settings) -> Check:
    # Same rules as backend.tools.shell_policy.build_shell_wiring; only fixed words are printed.
    from backend.tools.shell_policy import inspect_shell

    result = inspect_shell(settings)
    detail = f"JARVIS_SHELL_ENABLED={_word(settings.shell_enabled)}, " + (
        f"JARVIS_SHELL_ROOT={_word(settings.shell_root is not None)}"
    )
    if result.commands:
        detail += ", commands=" + ",".join(result.commands)
    return Check("shell", "シェルツール", result.status.value, result.code, detail)


def _paths(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    db = settings.db_path
    exists = db.is_file()
    checks.append(
        Check(
            "db",
            "DB",
            OK,
            "EXISTS" if exists else "DB_WILL_BE_CREATED",
            "exists" if exists else "missing",
        )
    )
    vault = settings.memory_vault_path
    if vault is None:
        checks.append(Check("vault", "メモリ vault", OFF, "NOT_CONFIGURED", "not set"))
    elif vault.is_symlink() or not vault.is_dir():  # the rule backend.api.app applies
        word = "missing" if not vault.exists() else "invalid"
        code = "PATH_MISSING" if word == "missing" else "PATH_INVALID"
        checks.append(Check("vault", "メモリ vault", INCOMPLETE, code, word))
    else:
        checks.append(Check("vault", "メモリ vault", OK, "EXISTS", "exists"))
    if settings.personality_path is None:
        checks.append(Check("personality", "人格設定", OFF, "NOT_CONFIGURED", "not set"))
    elif not settings.personality_path.exists():
        checks.append(Check("personality", "人格設定", INCOMPLETE, "PATH_MISSING", "missing"))
    else:
        try:
            load_personality(settings.personality_path)
        except PersonalityError:
            checks.append(Check("personality", "人格設定", INCOMPLETE, "PATH_INVALID", "invalid"))
        else:
            checks.append(Check("personality", "人格設定", OK, "EXISTS", "exists"))
    return checks


def run_checks(host: str = "127.0.0.1") -> list[Check]:
    """All checks for the process environment. Never raises."""
    environment = os.environ
    try:
        settings = Settings.from_env()
    except ConfigError:
        # The message only names a variable and the rule; it never contains a value.
        return [Check("config", "設定", INCOMPLETE, "CONFIG_INVALID", "see docs/doctor.md")]
    chat = _chat(settings, environment)
    research = _research(settings, chat)
    router = _router(settings, chat)
    return [
        chat,
        _models(settings, environment, chat),
        research,
        _research_memory(settings),
        _chat_memory(settings, chat),
        router,
        _casual(settings, router, chat),
        _chat_research(settings, router, research),
        _login(settings, environment, host),
        _shell(settings),
        *_paths(settings),
    ]


def render_text(checks: Sequence[Check], host: str) -> str:
    scope = "loopback" if is_loopback_host(host) else "non-loopback"
    lines = [f"JARVIS 設定診断 (待受ホスト指定: {scope})"]
    for check in checks:
        lines.append(
            f"[{check.status}] {check.label}: {REASONS[check.code]} ({check.code})"
            + (f" - {check.detail}" if check.detail else "")
        )
    bad = sum(c.status == INCOMPLETE for c in checks)
    lines.append(
        "結果: "
        + ("起動前に直すべき項目はありません" if not bad else f"INCOMPLETE が {bad} 件あります")
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, _out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(
        description="Check JARVIS configuration readiness (read-only)."
    )
    parser.add_argument("--host", default="127.0.0.1", help="the --host you plan to pass to serve")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    checks = run_checks(host=args.host)
    failed = any(c.status == INCOMPLETE for c in checks)
    if args.json:
        _out(
            json.dumps(
                {"ok": not failed, "checks": [c.as_dict() for c in checks]},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _out(render_text(checks, args.host))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
