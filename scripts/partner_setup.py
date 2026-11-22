"""Create a private, DISABLED partner config from hidden local terminal input.

No .env discovery, network, SQL, credential changes, bot calls or service start.
Never overwrite an existing file: its encryption/link keys must be preserved.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from urllib.parse import quote
import warnings

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SetupError(ValueError):
    """Messages deliberately contain no input values or credential fragments."""


def build_config(*, owner_id, database_password, manager_token):
    from cryptography.fernet import Fernet

    from services.partner_runtime import PartnerRuntimeSettings
    from services.partner_security import validate_bot_token

    if type(owner_id) is not int or not 0 < owner_id < 2**63:
        raise SetupError("Нужен положительный Telegram ID владельца стенда.")
    if (
        not isinstance(database_password, str)
        or not 16 <= len(database_password) <= 256
        or any(ord(c) < 32 or ord(c) == 127 for c in database_password)
    ):
        raise SetupError("Нужен пароль отдельной роли БД длиной от 16 символов.")
    try:
        validate_bot_token(manager_token)
    except Exception:
        raise SetupError("Проверьте токен управляющего бота.") from None
    # This template contains only the explicitly selected project's public
    # connection coordinates, not an SPN pool or automatically discovered DSN.
    template = json.loads(
        (ROOT / "ops/partner-platform/runtime.supabase.example.json").read_text()
    )
    runtime = template["runtime"]
    ref = runtime["database_project_ref"]
    runtime.update(
        mode="disabled",
        staging_isolation_confirmed=False,
        database_isolation_confirmed=True,
        database_api_disabled_confirmed=True,
        allow_staging_network=False,
        allow_staging_writes=False,
        database_dsn=(
            f"postgresql://spn_partner_runtime.{ref}:"
            f"{quote(database_password, safe='')}"
            "@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres"
        ),
        manager_bot_token=manager_token,
        manager_allowed_user_ids=[owner_id],
        encryption_keys=[Fernet.generate_key().decode("ascii")],
        webhook_key_hex=secrets.token_hex(32),
        subscription_key_hex=secrets.token_hex(32),
    )
    try:
        PartnerRuntimeSettings.from_mapping(runtime)
    except Exception:
        raise SetupError(
            "Не удалось проверить выключенную конфигурацию стенда."
        ) from None
    return template


def destination(path):
    path = Path(path).expanduser().absolute()
    if (
        path.suffix != ".json"
        or ".." in path.parts
        or any(part == ".env" or part.startswith(".env.") for part in path.parts)
    ):
        raise SetupError("Нужен новый отдельный JSON, не файл .env.")
    if path.is_relative_to(ROOT) and not path.is_relative_to(ROOT / ".release-secrets"):
        raise SetupError(
            "Храните секреты вне Git либо в игнорируемой .release-secrets."
        )
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise SetupError("Символические ссылки для конфигурации запрещены.")
    if path.exists():
        raise SetupError(
            "Файл уже существует. Сохраняйте прежние ключи; перезапись запрещена."
        )
    return path


def write_private_config(path, config):
    """Exclusive owner-only creation; no overwrite, backup or secret stdout."""
    path = destination(path)
    fd = directory = None
    created = False
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        parent = os.fstat(directory)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o077
        ):
            raise SetupError(
                "Папка конфигурации должна принадлежать вам и иметь права 0700."
            )
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        created = True
        os.fchmod(fd, 0o600)
        data = json.dumps(config, ensure_ascii=False, indent=2).encode() + b"\n"
        if len(data) > 65_536:
            raise SetupError("Конфигурация слишком велика.")
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError()
            data = data[written:]
        os.fsync(fd)
    except BaseException:
        if created:
            # Only this function's newly created partial file, never an old one.
            os.unlink(path.name, dir_fd=directory)
        raise
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, help="New owner-only JSON path, outside Git"
    )
    args = parser.parse_args(argv)
    try:
        target = destination(args.config)
        if not sys.stdin.isatty():
            raise SetupError(
                "Запустите настройку в личном интерактивном терминале, без pipe."
            )
        print(
            "Только локальная настройка. БД, Telegram, платежи и службы не запускаются."
        )
        owner = input("Ваш числовой Telegram ID для закрытого теста: ").strip()
        if not owner.isascii() or not owner.isdecimal():
            raise SetupError("Нужен числовой Telegram ID владельца.")
        with warnings.catch_warnings():
            # Never fall back to echoed stdin if a terminal cannot hide input.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass(
                "Пароль отдельной роли spn_partner_runtime (не VPS/SPN): "
            )
            if password != getpass.getpass("Повторите пароль роли БД: "):
                raise SetupError("Пароли не совпали. Файл не создан.")
            token = getpass.getpass("Токен @spn_franchise_bot из BotFather: ").strip()
        config = build_config(
            owner_id=int(owner), database_password=password, manager_token=token
        )
        write_private_config(target, config)
        print(
            "Конфигурация сохранена с правами 0600. Режим disabled; сеть и продажи выключены."
        )
        print(
            "Сохраните защищённую копию файла: его ключи нельзя менять при повторном запуске."
        )
        return 0
    except (KeyboardInterrupt, EOFError):
        print(
            "Настройка отменена. Существующая конфигурация не изменена.",
            file=sys.stderr,
        )
        return 130
    except SetupError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print(
            "Не удалось сохранить конфигурацию. Проверьте путь и права без раскрытия секретов.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
