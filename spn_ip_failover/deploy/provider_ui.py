"""Read an owned Xorek VPS address inventory from its BILLmanager page."""

from __future__ import annotations

import argparse
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlencode, urlsplit


XOREK_ORIGIN = "https://my.xorek.cloud"
XOREK_PATH = "/billmgr"
COLUMNS = ("IP-адрес", "Маска", "Шлюз", "Домен", "Тип", "Статус")
PUBLIC_IPV4 = "Публичные IPv4-адреса"


class InventoryError(RuntimeError):
    """The browser did not confirm the requested VPS and its IP table."""


def service_url(server_id):
    if not isinstance(server_id, str) or not re.fullmatch(r"[1-9][0-9]{0,17}", server_id):
        raise ValueError("Нужен числовой ID услуги Xorek")
    return XOREK_ORIGIN + XOREK_PATH + "?" + urlencode({"startform": "service.ip", "elid": server_id})


def service_heading(server_id):
    service_url(server_id)
    return re.compile(r"^IP-адреса\s*[-–—]\s*[^\n]*#\s*" + re.escape(server_id) + r"(?:\s*\([^\n]*\))?\s*$")


def assert_xorek_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme, parsed.hostname, parsed.port, parsed.path) != ("https", "my.xorek.cloud", None, XOREK_PATH):
        raise InventoryError("Браузер открыл страницу вне кабинета Xorek")
    if parsed.username is not None or parsed.password is not None:
        raise InventoryError("Адрес страницы кабинета содержит учётные данные")


def parse_rows(rows):
    result = []
    seen = set()
    for row in rows:
        if len(row) != len(COLUMNS) or any(len(value) > 253 for value in row):
            raise InventoryError("Строка таблицы IP-адресов не совпадает с ожидаемыми колонками")
        try:
            address = str(ip_address(row[0]))
        except ValueError:
            raise InventoryError("В колонке IP-адрес найдено значение другого формата") from None
        if address in seen or not row[4] or not row[5]:
            raise InventoryError("Таблица IP-адресов содержит повтор или неполную строку")
        seen.add(address)
        result.append({"ip": address, "mask": row[1], "gateway": row[2], "type": row[4], "status": row[5]})
    if not result:
        raise InventoryError("Список адресов выбранной услуги пуст или ещё не загрузился")
    return result


def project_cells(cells):
    cells = [" ".join(value.split()) for value in cells]
    if len(cells) == len(COLUMNS) + 1 and not cells[0]:
        cells = cells[1:]
    if len(cells) != len(COLUMNS):
        raise InventoryError("Ячейки строки не совпадают с заголовком таблицы IP-адресов")
    return cells


def read_xorek_inventory(page, server_id):
    response = page.goto(service_url(server_id), wait_until="domcontentloaded", timeout=20_000)
    if response is None or not response.ok:
        raise InventoryError("Кабинет Xorek не вернул страницу услуги")
    assert_xorek_url(page.url)
    heading = page.get_by_text(service_heading(server_id))
    heading.wait_for(state="visible", timeout=12_000)

    first_column = page.get_by_role("columnheader", name=COLUMNS[0], exact=True)
    tables = page.get_by_role("table").or_(page.get_by_role("grid"))
    headers = tables.filter(has=first_column)
    headers.wait_for(state="visible", timeout=12_000)
    if headers.count() != 1:
        raise InventoryError("На странице найдено несколько заголовков таблицы IP-адресов")
    if tuple(project_cells(headers.get_by_role("columnheader").all_text_contents())) != COLUMNS:
        raise InventoryError("Заголовки таблицы IP-адресов изменились")

    # BILLmanager can render column headings and the scrolling data in separate tables.
    cell = page.get_by_role("cell").or_(page.get_by_role("gridcell"))
    body_tables = tables.filter(has=cell)
    body_tables.first.wait_for(state="visible", timeout=12_000)
    inventories = []
    for table in body_tables.all():
        rows = []
        for row in table.get_by_role("row").all():
            cells = row.get_by_role("cell").or_(row.get_by_role("gridcell")).all_text_contents()
            if cells:
                rows.append(cells)
        try:
            addresses = parse_rows(project_cells(row) for row in rows)
        except InventoryError:
            continue
        if any(entry["type"] == PUBLIC_IPV4 and ip_address(entry["ip"]).version == 4 for entry in addresses):
            inventories.append(addresses)
    assert_xorek_url(page.url)
    if heading.count() != 1 or not heading.is_visible():
        raise InventoryError("Номер услуги изменился во время чтения таблицы")
    if len(inventories) != 1:
        raise InventoryError("Браузер не подтвердил единственную таблицу с адресами услуги")
    return inventories[0]


def private_profile(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError("Укажи абсолютный путь к отдельному профилю браузера")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("Каталог профиля должен принадлежать текущему пользователю и иметь права 0700")
    return path


def run_browser(profile, operation, server_id=None):
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        raise InventoryError("Для адаптера Xorek нужен отдельный браузер Playwright Chromium") from None

    try:
        with sync_playwright() as browser:
            context = browser.chromium.launch_persistent_context(
                user_data_dir=profile, headless=operation != "login", accept_downloads=False,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if operation == "login":
                    page.goto(XOREK_ORIGIN + XOREK_PATH, wait_until="domcontentloaded", timeout=20_000)
                    print("Войди в Xorek в открытом браузере и нажми Enter здесь.", file=sys.stderr)
                    input()
                    assert_xorek_url(page.url)
                    return None
                return read_xorek_inventory(page, server_id)
            finally:
                context.close()
    except PlaywrightError:
        raise InventoryError("Браузер не подтвердил кабинет и таблицу услуги Xorek. Проверь вход и язык интерфейса") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="Отдельный приватный каталог Chromium")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login")
    commands.add_parser("check")
    listing = commands.add_parser("list")
    listing.add_argument("--server-id", required=True)
    args = parser.parse_args(argv)

    try:
        server_id = None
        if args.command == "check":
            request = json.loads(sys.stdin.buffer.read(4097))
            if not isinstance(request, dict) or set(request) != {"operation", "server_id"} or request["operation"] != "check":
                raise ValueError("Ожидается запрос проверки одной услуги Xorek")
            server_id = request["server_id"]
        elif args.command == "list":
            server_id = args.server_id
        if server_id is not None:
            service_url(server_id)
        addresses = run_browser(private_profile(args.profile), args.command, server_id)
        if args.command == "check":
            print(json.dumps({"ok": True}))
        elif args.command == "list":
            print(json.dumps({"server_id": server_id, "addresses": addresses}, ensure_ascii=False))
        return 0
    except (InventoryError, OSError, ValueError, EOFError) as exc:
        print(f"Ошибка проверки Xorek: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
