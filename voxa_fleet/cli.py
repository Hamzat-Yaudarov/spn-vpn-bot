"""CLI entrypoint for the same fleet backend as the administration panel."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import sys

from .runtime import Runtime


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="~/.local/share/spn-voxa", help="Private persistent state and credential directory")
    sub = p.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Set the owner password with hidden local input")
    setup.add_argument("--replace-password", action="store_true")
    serve = sub.add_parser("serve", help="Run the loopback-only admin panel")
    serve.add_argument("--port", type=int, default=8970)
    sub.add_parser("status", help="Read local readiness, settings and operation counts; no network")
    sub.add_parser("preflight", help="Read-only API identity/project/currency check; no purchase")
    sub.add_parser("catalog", help="Refresh the read-only provider catalogue")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        runtime = Runtime(Path(args.data_dir).expanduser())
        if args.command == "setup":
            password = getpass.getpass("Новый пароль панели (от 16 символов): ")
            again = getpass.getpass("Повторите пароль: ")
            if password != again:
                raise ValueError("Пароли не совпали")
            runtime.auth.set_password(password, replace=args.replace_password)
            print("Пароль панели сохранён в виде scrypt-хеша. Покупки и автозамена не включались.")
        elif args.command == "serve":
            if not runtime.auth.configured():
                raise ValueError("Сначала выполните setup для задания пароля владельца")
            if not 1024 <= args.port <= 65535:
                raise ValueError("Недопустимый порт")
            import uvicorn
            from .web import create_app
            print(f"Панель: http://127.0.0.1:{args.port} — доступ только с этой машины или через SSH-туннель.", flush=True)
            uvicorn.run(create_app(runtime=runtime), host="127.0.0.1", port=args.port,
                        access_log=False, proxy_headers=False, log_level="warning")
        elif args.command == "status":
            print(json.dumps({"readiness": runtime.readiness(), "settings": runtime.store.get_settings(),
                              "servers": len(runtime.store.list_servers()), "operations": len(runtime.store.list_operations()),
                              "budget": runtime.store.budget_status()}, ensure_ascii=False, indent=2))
        elif args.command == "preflight":
            print(json.dumps(runtime.preflight(), ensure_ascii=False, indent=2))
        elif args.command == "catalog":
            print(json.dumps(runtime.catalog(refresh=True), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, EOFError) as exc:
        # OSError can include a private path, but never emit transport bodies/tokens.
        print("Остановлено: " + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "системная ошибка; подробности скрыты"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Остановлено пользователем; состояние сохранено.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
