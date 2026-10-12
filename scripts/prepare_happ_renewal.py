"""Print free renewal-link headers for Remnawave; never write to a live panel."""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import PUBLIC_SITE_URL  # noqa: E402


ANNOUNCEMENTS = {
    "happ": "💳 Продлить подписку — нажмите ⓘ слева от трафика.",
    "incy": "💳 Продлить подписку — нажмите «🌐 Сайт».",
    "both": "💳 Продление: Happ — ⓘ слева от трафика; INCY — «🌐 Сайт».",
}
BYPASS_NOTICE = "Подключайтесь к «ОБХОД» только во время ограничений интернета."


def build_headers(site_url: str, support_url: str = "", *, client: str = "both") -> dict:
    site = urlsplit(site_url)
    if (site.scheme != "https" or not site.hostname or site.username or site.password
            or site.query or site.fragment or site.path not in {"", "/"}):
        raise ValueError("Укажите HTTPS-адрес сайта без пути, например https://wayspn.ru")
    if client not in ANNOUNCEMENTS:
        raise ValueError("Выберите happ, incy или both")
    link = f"{site_url.rstrip('/')}/renew#{{{{SUBSCRIPTION_URL}}}}"
    headers = {
        # Standard website link and announcement do not require paid banners.
        "profile-web-page-url": link,
        "announce": f"rwEncodeBase64:{ANNOUNCEMENTS[client]}\n{BYPASS_NOTICE}",
    }
    # INCY shows announce-url as a separate "Канал/Бот" button. Renewal uses
    # the website button, so do not add a second link to the same page.
    if support_url:
        support = urlsplit(support_url)
        if support.scheme != "https" or not support.hostname or support.username or support.password:
            raise ValueError("Ссылка поддержки должна начинаться с https://")
        headers["support-url"] = support_url
    return headers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-url", default=PUBLIC_SITE_URL)
    parser.add_argument("--client", choices=("happ", "incy", "both"), default="both")
    parser.add_argument("--support-url", default="", help="Не указывайте, чтобы сохранить текущую ссылку поддержки")
    args = parser.parse_args()
    try:
        headers = build_headers(args.site_url, args.support_url, client=args.client)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(headers, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
