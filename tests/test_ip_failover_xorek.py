import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs
from unittest.mock import MagicMock, call, patch
from xml.etree.ElementTree import fromstring

from spn_ip_failover.provider import Allocation, ProviderError
from spn_ip_failover.xorek import ENDPOINT, OrderSettings, SessionExpired, XorekBilling, execute


def xml_inventory(*entries):
    rows = "".join(f"<elem><id>{row_id}</id><name>{address}</name>"
                   "<is_main>off</is_main><no_delete>off</no_delete></elem>" for row_id, address in entries)
    return fromstring(f'<doc func="service.ip">{rows}</doc>')


def order_settings(directory):
    return OrderSettings.from_config({
        "function": "service.ip.order",
        "button_name": "order",
        "server_parameter": "plid",
        "quantity_parameter": "count",
        "fixed_fields": {"family": "ipv4"},
        "quote_server_field": "server",
        "quote_quantity_field": "count",
        "quote_price_field": "cost",
        "quote_currency_field": "currency",
        "maximum_rub": "80.00",
        "journal_path": str(Path(directory) / "order-state"),
    })


def xml_toolbar():
    return fromstring("""<doc><metadata name="service.ip"><toolbar>
      <toolbtn func="service.ip.order" name="order" type="new" />
    </toolbar></metadata></doc>""")


def xml_quote(price="70.25", currency="RUB", count="1", server="21"):
    return fromstring(f"""<doc func="service.ip.order"><server>{server}</server><count>{count}</count>
      <cost>{price}</cost><currency>{currency}</currency></doc>""")


class XorekBillingTests(unittest.TestCase):
    def test_authinfo_uses_post_credentials_without_creating_or_refreshing_a_session(self):
        billing = XorekBilling(username="account", password="fake-secret", auth_mode="authinfo")
        with patch.object(billing.opener, "open") as opener:
            opener.return_value.__enter__.return_value.read.return_value = b'<doc><elem><id>1</id><name>8.8.8.8</name></elem></doc>'
            self.assertEqual(billing.inventory("21"), (Allocation("1", "8.8.8.8"),))
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(parse_qs(request.data.decode())["authinfo"], ["account:fake-secret"])
        self.assertNotIn("auth", parse_qs(request.data.decode()))
        with patch.object(billing, "_request", side_effect=ProviderError("rejected")) as request:
            with self.assertRaises(ProviderError):
                billing.inventory("21")
            request.assert_called_once_with("service.ip", authinfo="account:fake-secret", elid="21")

    def test_primary_or_unconfirmed_removal_flags_never_submit_delete(self):
        for field, value in (("is_main", "on"), ("no_delete", "on"), ("no_delete", None)):
            before = xml_inventory(("1", "8.8.8.8"), ("2", "8.8.4.4"))
            row = before.findall("elem")[1]
            if value is None:
                row.remove(row.find(field))
            else:
                row.find(field).text = value
            billing = XorekBilling(session="local-test")
            with patch.object(billing, "_request", return_value=before) as request:
                with self.assertRaises(ProviderError):
                    billing.release("21", Allocation("2", "8.8.4.4"))
                request.assert_called_once_with("service.ip", auth="local-test", elid="21")

    def test_inventory_uses_server_id_and_reads_only_public_ipv4(self):
        billing = XorekBilling(session="local-test")
        response = xml_inventory(("1", "8.8.8.8"), ("2", "2001:4860::1"), ("3", "10.0.0.1"))
        with patch.object(billing, "_request", return_value=response) as request:
            inventory = execute(billing, {"operation": "inventory", "server_id": "21"})
        self.assertEqual(inventory, {"allocations": [{"id": "1", "ip": "8.8.8.8"}]})
        request.assert_called_once_with("service.ip", auth="local-test", elid="21")

    def test_inventory_refreshes_an_expired_session_with_login_credentials(self):
        billing = XorekBilling(session="old-session", username="account", password="private")
        with patch.object(billing.opener, "open") as opener:
            opener.return_value.__enter__.return_value.read.side_effect = [
                b'<doc><error type="auth" object="badsession"/></doc>',
                b'<doc><auth id="new-session"/></doc>',
                b'<doc func="service.ip"><elem><id>1</id><name>8.8.8.8</name></elem></doc>',
            ]
            self.assertEqual(billing.inventory("21"), (Allocation("1", "8.8.8.8"),))
        requests = [parse_qs(entry.args[0].data.decode()) for entry in opener.call_args_list]
        self.assertEqual([request["func"] for request in requests], [["service.ip"], ["auth"], ["service.ip"]])
        self.assertEqual(requests[0]["auth"], ["old-session"])
        self.assertEqual(requests[2]["auth"], ["new-session"])

    def test_refreshes_http_401_once_and_refuses_to_follow_unrelated_redirects(self):
        for status, headers in (
            (401, {}),
            (302, {"Location": "/billmgr?startform=logon"}),
        ):
            with self.subTest(status=status):
                billing = XorekBilling(session="old-session", username="account", password="private")
                refused = HTTPError(ENDPOINT, status, "", headers, None)
                auth = MagicMock()
                auth.__enter__.return_value.read.return_value = b'<doc><auth id="new-session"/></doc>'
                with patch.object(billing.opener, "open", side_effect=[refused, auth, refused]) as opener:
                    with self.assertRaises(SessionExpired):
                        billing.inventory("21")
                    self.assertEqual(opener.call_count, 3)
        for status, headers in (
            (403, {}),
            (302, {"Location": "https://elsewhere.example/billmgr?startform=logon"}),
        ):
            with self.subTest(status=status):
                billing = XorekBilling(session="old-session", username="account", password="private")
                with patch.object(billing.opener, "open", side_effect=HTTPError(ENDPOINT, status, "", headers, None)) as opener:
                    with self.assertRaisesRegex(ProviderError, "did not confirm"):
                        billing.inventory("21")
                    opener.assert_called_once()

    def test_session_only_fails_without_attempting_a_login(self):
        billing = XorekBilling(session="old-session")
        with patch.object(billing, "_request", side_effect=SessionExpired("session expired")) as request:
            with self.assertRaises(SessionExpired):
                billing.inventory("21")
            request.assert_called_once_with("service.ip", auth="old-session", elid="21")

    def test_discovers_action_names_without_releasing_inventory_or_authentication(self):
        billing = XorekBilling(session="local-test")
        response = fromstring("""<doc>
          <auth>secret</auth>
          <metadata name="service.ip"><toolbar>
            <toolbtn func="form.open" name="order" type="new" secret="secret" />
            <toolbtn form="form.edit" name="edit" />
          </toolbar></metadata>
          <elem><id>123</id><name>8.8.8.8</name></elem>
        </doc>""")
        with patch.object(billing, "_request", return_value=response) as request:
            result = execute(billing, {"operation": "discover", "server_id": "21"})
        self.assertEqual(result, {"buttons": [
            {"func": "form.open", "name": "order", "type": "new"},
            {"form": "form.edit", "name": "edit"},
        ]})
        request.assert_called_once_with("service.ip", auth="local-test", elid="21", out="devel")

    def test_release_checks_exact_row_and_verifies_removal(self):
        billing = XorekBilling(session="local-test")
        before = xml_inventory(("1", "8.8.8.8"), ("2", "8.8.4.4"))
        after = xml_inventory(("1", "8.8.8.8"))
        request_data = {"operation": "release", "server_id": "21", "id": "2", "ip": "8.8.4.4"}
        with patch.object(billing, "_request", side_effect=[before, fromstring("<doc><ok/></doc>"), after]) as request:
            self.assertEqual(execute(billing, request_data), {"ok": True})
        self.assertEqual(request.call_args_list, [
            call("service.ip", auth="local-test", elid="21"),
            call("service.ip.delete", auth="local-test", elid="2", sok="ok"),
            call("service.ip", auth="local-test", elid="21"),
        ])
        with patch.object(billing, "_request", return_value=after) as request:
            self.assertEqual(execute(billing, request_data), {"ok": True})
            request.assert_called_once()
            with self.assertRaisesRegex(ProviderError, "exact row"):
                billing.release("21", Allocation("2", "8.8.8.8"))
            with self.assertRaisesRegex(ProviderError, "another assigned IPv4"):
                billing.release("21", Allocation("1", "8.8.8.8"))
            self.assertTrue(all(item.args == ("service.ip",) for item in request.call_args_list))

    def test_release_refreshes_preflight_but_does_not_replay_the_delete(self):
        billing = XorekBilling(session="old-session", username="account", password="private")
        before = xml_inventory(("1", "8.8.8.8"), ("2", "8.8.4.4"))
        expiry = SessionExpired("session expired")
        replies = [expiry, fromstring('<doc><auth id="new-session"/></doc>'), before, expiry]
        with patch.object(billing, "_request", side_effect=replies) as request:
            with self.assertRaises(SessionExpired):
                billing.release("21", Allocation("2", "8.8.4.4"))
        self.assertEqual(request.call_args_list, [
            call("service.ip", auth="old-session", elid="21"),
            call("auth", username="account", password="private"),
            call("service.ip", auth="new-session", elid="21"),
            call("service.ip.delete", auth="new-session", elid="2", sok="ok"),
        ])

    def test_order_requires_a_verified_contract(self):
        billing = XorekBilling(session="local-test")
        with patch.object(billing, "_request") as request:
            with self.assertRaisesRegex(ProviderError, "paid order form"):
                execute(billing, {"operation": "allocate", "server_id": "21", "request_id": "order-1"})
            with self.assertRaisesRegex(ProviderError, "paid order form"):
                execute(billing, {"operation": "check", "server_id": "21"})
            request.assert_not_called()

    def test_quoted_order_submits_exactly_once_and_verifies_inventory(self):
        before = xml_inventory(("1", "8.8.8.8"))
        after = xml_inventory(("1", "8.8.8.8"), ("2", "8.8.4.4"))
        replies = [xml_toolbar(), xml_quote(), before, fromstring("<doc><ok/></doc>"), after, after]
        with tempfile.TemporaryDirectory() as directory:
            settings = order_settings(directory)
            billing = XorekBilling(session="local-test", order=settings)
            with patch.object(billing, "_request", side_effect=replies) as request, patch("spn_ip_failover.xorek.time.sleep"):
                self.assertEqual(billing.allocate("21", "order-1"), Allocation("2", "8.8.4.4"))
            submissions = [c for c in request.call_args_list if c.kwargs.get("sok")]
            self.assertEqual(submissions, [
                call("service.ip.order", auth="local-test", sok="ok", family="ipv4", plid="21", count="1"),
            ])
            self.assertIn(call("service.ip.order", auth="local-test", family="ipv4", plid="21", count="1"),
                          request.call_args_list)
            restarted = XorekBilling(session="local-test", order=settings)
            with patch.object(restarted, "_request") as request:
                self.assertEqual(restarted.allocate("21", "order-1"), Allocation("2", "8.8.4.4"))
                request.assert_not_called()

    def test_session_expiry_after_a_purchase_request_does_not_replay_it(self):
        before = xml_inventory(("1", "8.8.8.8"))
        replies = [xml_toolbar(), xml_quote(), before, SessionExpired("session expired")]
        with tempfile.TemporaryDirectory() as directory:
            billing = XorekBilling(session="old-session", username="account", password="private",
                                   order=order_settings(directory))
            with patch.object(billing, "_request", side_effect=replies) as request:
                with self.assertRaises(SessionExpired):
                    billing.allocate("21", "order-1")
                with self.assertRaisesRegex(ProviderError, "was not repeated"):
                    billing.allocate("21", "order-1")
            self.assertEqual([entry for entry in request.call_args_list if entry.kwargs.get("sok")], [
                call("service.ip.order", auth="old-session", sok="ok", family="ipv4", plid="21", count="1"),
            ])
            self.assertNotIn("auth", [entry.args[0] for entry in request.call_args_list])

    def test_rejects_invalid_quotes_and_full_inventory_before_payment(self):
        full = xml_inventory(*[(str(n), f"8.8.4.{n}") for n in range(1, 11)])
        cases = [
            ([xml_toolbar(), xml_quote(price="80.01")], "RUB ceiling"),
            ([xml_toolbar(), xml_quote(currency="EUR")], "not in RUB"),
            ([xml_toolbar(), xml_quote(count="2")], "exactly one"),
            ([xml_toolbar(), xml_quote(server="22")], "requested VPS"),
            ([xml_toolbar(), xml_quote(), full], "maximum of ten"),
        ]
        for replies, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                billing = XorekBilling(session="local-test", order=order_settings(directory))
                with patch.object(billing, "_request", side_effect=replies) as request:
                    with self.assertRaisesRegex(ProviderError, error):
                        billing.allocate("21", "order-1")
                self.assertTrue(all("sok" not in c.kwargs for c in request.call_args_list))

    def test_cart_and_concurrent_inventory_changes_stop_the_order(self):
        before = xml_inventory(("1", "8.8.8.8"))
        two_added = xml_inventory(("1", "8.8.8.8"), ("2", "8.8.4.4"), ("3", "1.1.1.1"))
        cases = [
            ([fromstring("<doc><basket/></doc>")], "confirmation was unexpected"),
            ([fromstring("<doc><ok/></doc>"), two_added], "inventory changed ambiguously"),
        ]
        for suffix, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                billing = XorekBilling(session="local-test", order=order_settings(directory))
                replies = [xml_toolbar(), xml_quote(), before, *suffix]
                with patch.object(billing, "_request", side_effect=replies):
                    with self.assertRaisesRegex(ProviderError, error):
                        billing.allocate("21", "order-1")
                with patch.object(billing, "_request") as request:
                    with self.assertRaisesRegex(ProviderError, "was not repeated"):
                        billing.allocate("21", "order-1")
                    with self.assertRaisesRegex(ProviderError, "still needs reconciliation"):
                        billing.allocate("21", "order-2")
                    request.assert_not_called()
                with patch.object(billing, "_request", return_value=before) as request:
                    billing.reconcile("21", "order-1", None)
                    request.assert_called_once_with("service.ip", auth="local-test", elid="21")

    def test_password_goes_in_post_body_and_response_errors_are_sanitized(self):
        billing = XorekBilling(username="local-test", password="fake-secret")
        with patch.object(billing.opener, "open") as opener:
            opener.return_value.__enter__.return_value.read.return_value = b"<doc><error>fake-secret</error></doc>"
            with self.assertRaisesRegex(ProviderError, "^XorekCloud rejected auth$"):
                billing.inventory("21")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(parse_qs(request.data.decode())["password"], ["fake-secret"])
