"""Create the value for JARVIS_AUTH_PASSPHRASE_HASH.

Usage: ``python -m backend.auth.hash_password``

The passphrase is read with getpass (never from arguments or the environment) and only the
encoded hash is printed to standard output.
"""

import sys
from collections.abc import Callable
from getpass import getpass

from backend.auth.passwords import PasswordHashError, check_passphrase_policy, hash_passphrase


def main(prompt: Callable[[str], str] = getpass) -> int:
    try:
        first = prompt("Passphrase: ")
        check_passphrase_policy(first)  # fail fast, before asking for the confirmation
        if first != prompt("Repeat passphrase: "):
            print("Passphrases do not match.", file=sys.stderr)
            return 1
        encoded = hash_passphrase(first)
    except PasswordHashError as error:
        print(f"Rejected: {error}.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Aborted.", file=sys.stderr)
        return 1
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
