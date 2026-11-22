"""Optional 1Cent sign-in for a dedicated, operator-configured browser profile."""

import math
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit

from .state import Journal


ORIGIN = "https://bill.1cent.host"
LOGIN_PATH = "/login"
COOLDOWN_SECONDS = 300
CHALLENGE_FIELDS = (
    'input[autocomplete="one-time-code"], '
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
    'iframe[src*="challenges.cloudflare.com"], iframe[title*="challenge" i]'
)
CHALLENGE_TEXT = re.compile(
    r"captcha|капч[аиу]|двухфакторн|проверка безопасности|"
    r"(?:введите|укажите)\s+(?:одноразовый\s+|проверочный\s+)?код|"
    r"код\s+(?:подтверждения|из письма|из приложения)",
    re.IGNORECASE,
)


class OneCentLoginError(RuntimeError):
    """The 1Cent browser session needs operator attention."""


def _billing_url(url):
    try:
        location = urlsplit(url)
        if (location.scheme == "https" and location.hostname == "bill.1cent.host"
                and location.port in (None, 443) and location.username is None and location.password is None):
            return location
    except ValueError:
        pass
    return None


def _visible(locator):
    return locator.count() == 1 and locator.is_visible()


class OneCentLogin:
    def __init__(self, page, email, password, journal_dir):
        if (not isinstance(email, str) or not 1 <= len(email) <= 254
                or not re.fullmatch(r"[^@\s]+@[^@\s]+", email)
                or not isinstance(password, str) or not 1 <= len(password) <= 4096
                or any(char in password for char in ("\x00", "\r", "\n"))):
            raise OneCentLoginError("Проверь параметры входа в 1Cent")
        self.page = page
        self._email = email
        self._password = password
        self._journal_dir = Path(journal_dir)
        self.phase = "not_started"

    def __call__(self):
        location = _billing_url(self.page.url)
        if location is None or location.path != LOGIN_PATH:
            return False
        try:
            return self._login(location)
        except OneCentLoginError:
            raise
        except Exception:
            # A browser exception can contain its call arguments, including a field value.
            raise OneCentLoginError("Вход 1Cent не подтверждён. Проверь отдельный профиль браузера") from None

    def _assert_login(self):
        location = _billing_url(self.page.url)
        if location is None or location.path != LOGIN_PATH or location.query or location.fragment:
            raise OneCentLoginError("Кабинет 1Cent изменил страницу входа. Требуется проверка вручную")
        self._assert_no_challenge()

    def _assert_no_challenge(self):
        for found in (self.page.locator(CHALLENGE_FIELDS), self.page.get_by_text(CHALLENGE_TEXT)):
            count = found.count()
            if count > 20 or any(found.nth(index).is_visible() for index in range(count)):
                raise OneCentLoginError("1Cent запросил дополнительное подтверждение. Заверши вход вручную")

    def _assert_form_destination(self, field):
        form = field.locator("xpath=ancestor::form[1]")
        if form.count() == 1:
            action = form.get_attribute("action", timeout=1500)
            if action and _billing_url(urljoin(ORIGIN + LOGIN_PATH, action)) is None:
                raise OneCentLoginError("Форма 1Cent указывает на другой сайт. Требуется проверка вручную")

    def _login(self, location):
        self.phase = "wait_login_form"
        if location.query or location.fragment:
            response = self.page.goto(ORIGIN + LOGIN_PATH, wait_until="domcontentloaded", timeout=12_000)
            if response is None or not response.ok:
                raise OneCentLoginError("Кабинет 1Cent не открыл форму входа")
        self._assert_login()
        self.phase = "identify_form"
        heading = self.page.get_by_role("heading", name="Вход в кабинет", exact=True)
        heading.wait_for(state="visible", timeout=7_000)
        email = self.page.get_by_label("Почта", exact=True)
        password = self.page.get_by_label("Пароль", exact=True)
        submit = self.page.get_by_role("button", name="Войти", exact=True)
        if not all(_visible(item) for item in (heading, email, password, submit)):
            raise OneCentLoginError("Форма входа 1Cent изменилась. Требуется проверка вручную")
        if password.get_attribute("type", timeout=1500) != "password":
            raise OneCentLoginError("Поле пароля 1Cent не подтверждено")
        self._assert_form_destination(email)
        self._assert_form_destination(password)
        self.phase = "check_login_cooldown"

        # Authentication is nested under the account order journal. A first
        # installation has neither directory; create/validate the private
        # parent before Journal creates its own child, never a public fallback.
        self._journal_dir.parent.mkdir(mode=0o700, exist_ok=True)
        Journal(self._journal_dir.parent).validate_directory()
        with Journal(self._journal_dir) as journal:
            row = journal.data["servers"].setdefault("1cent", {})
            last_attempt = row.get("last_attempt", 0)
            if type(last_attempt) not in (int, float) or not math.isfinite(last_attempt):
                raise OneCentLoginError("Не удалось проверить журнал входа 1Cent")
            if time.time() < last_attempt + COOLDOWN_SECONDS:
                raise OneCentLoginError("Недавно уже была попытка входа в 1Cent. Повтори позже или войди вручную")

            self._assert_login()
            self.phase = "fill_email"
            email.fill(self._email, timeout=3000)
            self._assert_login()
            self.phase = "fill_password"
            password.fill(self._password, timeout=3000)
            self._assert_login()
            if not _visible(submit) or not submit.is_enabled(timeout=1500):
                raise OneCentLoginError("Кнопка входа 1Cent недоступна")
            row["last_attempt"] = time.time()
            journal.save()
            self._assert_login()
            self.phase = "submit_login"
            submit.click(timeout=5000)
            self.phase = "wait_dashboard"
            return self._wait_for_dashboard()

    def _wait_for_dashboard(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            location = _billing_url(self.page.url)
            if location is None:
                break
            self._assert_no_challenge()
            if location.path == "/dashboard" or location.path.startswith("/dashboard/"):
                return True
            if location.path != LOGIN_PATH:
                break
            time.sleep(0.2)
        raise OneCentLoginError("Вход 1Cent не подтверждён. Проверь отдельный профиль браузера")
