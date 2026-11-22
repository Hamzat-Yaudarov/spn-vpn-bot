"""Offline Voxa HTTP contract tests. No credentials, network or paid requests."""

import http.client
import io
import json
import ssl
import unittest
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from unittest.mock import patch

from voxa_fleet import provider


PRODUCT = "1e2d3c4b-5566-4788-9900-aabbccddeeff"
ORDER = "de605a35-bc23-4057-906c-3fe19662dd96"
SERVICE = "abf67f8a-79ec-422f-967d-e801a1e905f6"
PROJECT = "bf1280a0-06b0-41bf-9403-de2dc628c62e"
KEY = "5b5d91c0-498f-46b0-9aee-2e3d9ae00204"
SECRET = "fixture-private-api-secret"


def headers(**values):
    message = Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = str(value)
    return message


class Response:
    def __init__(self, data=None, *, raw=None, status=200, response_headers=None, read_error=None):
        self.raw = raw if raw is not None else json.dumps({"success": True, "data": data}).encode()
        self.status = status
        self.headers = response_headers if response_headers is not None else headers(Content_Type="application/json")
        self.read_error = read_error
        self.closed = False
        self.read_sizes = []

    def getcode(self):
        return self.status

    def read(self, size):
        self.read_sizes.append(size)
        if self.read_error:
            raise self.read_error
        return self.raw[:size]

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected repeat or extra HTTP call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def order_body(**changes):
    return {"productId": PRODUCT, "billingCycle": "hourly", "hostname": "fleet-operation-1.example.com",
            "acceptTerms": True, "location": "germany", **changes}


def create_result():
    return {"order": {"id": ORDER, "idempotency_key": KEY}, "services": [{"id": SERVICE}],
            "document": None, "settled": True}


def detail_vps(**changes):
    return {"id": SERVICE, "primary_ip": "8.8.8.8", "ip_addresses": [], "status": "active",
            "instance": {"state": "running"}, "project": {"id": PROJECT, "slug": "fleet"}, **changes}


class VoxaProviderTests(unittest.TestCase):
    def client(self, *responses, project=PROJECT):
        opener = Opener(*responses)
        return provider.VoxaClient(SECRET, project, opener=opener), opener

    def test_identity_and_balance_unwrap_actual_envelope(self):
        identity = {"user": {"id": "fixture"}, "project": {"id": PROJECT, "bound": True}}
        balance = {"balance": "12.5000", "currency": "EUR"}
        client, opener = self.client(Response(identity), Response(balance))
        self.assertEqual(client.identity(), identity)
        self.assertEqual(client.balance(), balance)
        self.assertEqual([call[0].full_url for call in opener.calls],
                         [provider.API_BASE + "/", provider.API_BASE + "/account/balance"])
        for request, timeout in opener.calls:
            self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
            self.assertEqual(request.get_header("X-project-id"), PROJECT)
            self.assertEqual(timeout, 20)
            self.assertNotIn(SECRET, request.full_url)
            self.assertNotIn(SECRET, repr(request))

    def test_project_slug_explicitly_sent_and_unbound_diagnostic_allowed(self):
        client, opener = self.client(Response({}), project="fleet-test")
        client.identity()
        self.assertEqual(opener.calls[0][0].get_header("X-project-id"), "fleet-test")
        client, opener = self.client(Response({}), project=None)
        client.identity()
        self.assertIsNone(opener.calls[0][0].get_header("X-project-id"))

    def test_default_tls_verifies_certificate_and_hostname_and_blocks_redirects(self):
        with patch.object(provider.urllib.request, "build_opener", return_value=Opener()) as build:
            provider.VoxaClient(SECRET, PROJECT)
        https, redirect = build.call_args.args
        self.assertIsInstance(https, urllib.request.HTTPSHandler)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)
        self.assertIsInstance(redirect, provider._NoRedirect)
        for code in (301, 302, 303, 307, 308):
            request = urllib.request.Request(provider.API_BASE + "/orders", data=b"{}",
                                             headers={"Authorization": "Bearer " + SECRET})
            self.assertIsNone(redirect.redirect_request(request, None, code, "", {}, "https://evil.example"))

    def test_invalid_constructor_values_never_echo_secret(self):
        for token in ("", SECRET + "\n", SECRET + " ", None):
            with self.subTest(token_type=type(token).__name__), self.assertRaises(ValueError) as caught:
                provider.VoxaClient(token, opener=Opener())
            self.assertNotIn(SECRET, str(caught.exception))
        for project in ("../default", "fleet\r\nX: value", "https://evil.example", "A B", "0" * 41,
                        "00000000-0000-0000-0000-000000000000"):
            with self.subTest(project=project), self.assertRaises(ValueError):
                provider.VoxaClient(SECRET, project, opener=Opener())
        for timeout in (True, 0, -1, float("inf"), float("nan"), "20"):
            with self.assertRaises(ValueError):
                provider.VoxaClient(SECRET, timeout=timeout, opener=Opener())

    def test_product_detail_availability_and_vps_use_documented_paths(self):
        product = {"product": {"id": PRODUCT}, "billing_cycles": [{"cycle": "hourly"}]}
        available = {"available": False, "code": "no_capacity", "location": "Germany & Finland"}
        vps = detail_vps()
        client, opener = self.client(Response(product), Response(available), Response(vps))
        self.assertEqual(client.product(PRODUCT), product)
        self.assertEqual(client.availability(PRODUCT, "Germany & Finland"), available)
        self.assertEqual(client.vps(SERVICE), vps)
        paths = [urllib.parse.urlsplit(call[0].full_url).path for call in opener.calls]
        self.assertEqual(paths, ["/api/v1/products/" + PRODUCT,
                                 "/api/v1/products/" + PRODUCT + "/availability", "/api/v1/vps/" + SERVICE])
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(opener.calls[1][0].full_url).query),
                         {"location": ["Germany & Finland"]})

    def test_unpaginated_actual_products_data_products(self):
        products = [{"id": PRODUCT, "hourly_price": "0.0100"}]
        client, opener = self.client(Response({"products": products, "count": 1}))
        self.assertEqual(client.products(), products)
        self.assertEqual(len(opener.calls), 1)

    def test_products_collect_all_advertised_nested_pages(self):
        first = {"products": [{"id": PRODUCT}], "pagination": {"page": 1, "limit": 1, "total": 2, "pages": 2}}
        second = {"products": [{"id": ORDER}], "pagination": {"page": 2, "limit": 1, "total": 2, "pages": 2}}
        client, opener = self.client(Response(first), Response(second))
        self.assertEqual([row["id"] for row in client.products()], [PRODUCT, ORDER])
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(opener.calls[1][0].full_url).query)["page"], ["2"])

    def test_products_collect_outer_envelope_pagination(self):
        responses = []
        for page, identifier in ((1, PRODUCT), (2, ORDER)):
            responses.append(Response(raw=json.dumps({"success": True, "data": {"products": [{"id": identifier}]},
                "pagination": {"page": page, "limit": 1, "total": 2, "pages": 2}}).encode()))
        client, _ = self.client(*responses)
        self.assertEqual(len(client.products()), 2)

    def test_orders_collect_pagination_without_pages_and_preserve_filters(self):
        responses = [Response({"orders": [{"id": identifier}],
            "pagination": {"page": page, "limit": 1, "total": 2}})
            for page, identifier in ((1, ORDER), (2, PRODUCT))]
        client, opener = self.client(*responses)
        self.assertEqual(len(client.orders(hostname="srv.example.com", idempotencyKey=KEY)), 2)
        for request, _ in opener.calls:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            self.assertEqual(query["hostname"], ["srv.example.com"])
            self.assertEqual(query["idempotencyKey"], [KEY])
        with self.assertRaises(ValueError):
            client.orders(project="default")
        self.assertEqual(len(opener.calls), 2)

    def test_empty_orders_are_confirmed_only_with_complete_pagination(self):
        client, _ = self.client(Response({"orders": [], "pagination": {"page": 1, "limit": 200, "total": 0}}))
        self.assertEqual(client.orders(), [])
        client, _ = self.client(Response({"orders": []}))
        with self.assertRaises(provider.ProviderError):
            client.orders()

    def test_pagination_drift_or_ignored_page_never_returns_partial_inventory(self):
        invalids = [
            {"page": 2, "limit": 1, "total": 1},
            {"page": True, "limit": 1, "total": 1},
            {"page": 1, "limit": 0, "total": 1},
            {"page": 1, "limit": 1, "total": -1},
            {"page": 1, "limit": 1, "total": 2, "pages": 1},
            {"page": 1, "limit": 200, "total": 2},
        ]
        for pagination in invalids:
            client, _ = self.client(Response({"orders": [{"id": ORDER}], "pagination": pagination}))
            with self.subTest(pagination=pagination), self.assertRaises(provider.ProviderError):
                client.orders()
        data = {"orders": [{"id": ORDER}], "pagination": {"page": 1, "limit": 1, "total": 2}}
        client, _ = self.client(Response(data), Response(data))
        with self.assertRaises(provider.ProviderError):
            client.orders()

    def test_pagination_limit_is_bounded(self):
        client, opener = self.client(Response({"orders": [{"id": ORDER}],
            "pagination": {"page": 1, "limit": 1, "total": 2}}))
        with patch.object(provider, "MAX_PAGES", 1), self.assertRaises(provider.ProviderError) as caught:
            client.orders()
        self.assertEqual(caught.exception.code, "PAGINATION_LIMIT")
        self.assertEqual(len(opener.calls), 1)

    def test_pagination_total_cannot_change_mid_inventory(self):
        client, _ = self.client(
            Response({"orders": [{"id": ORDER}], "pagination": {"page": 1, "limit": 1, "total": 2}}),
            Response({"orders": [], "pagination": {"page": 2, "limit": 1, "total": 1}}))
        with self.assertRaises(provider.ProviderError):
            client.orders()

    def test_create_uses_hourly_body_unchanged_and_saved_key_header_once(self):
        result = create_result()
        body = order_body(configOptions={"os_image_id": PRODUCT, "user_data": "#cloud-config\npackages: []"})
        client, opener = self.client(Response(result, status=201))
        self.assertEqual(client.create_order(body, KEY), result)
        self.assertEqual(len(opener.calls), 1)
        request, _ = opener.calls[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, provider.API_BASE + "/orders")
        self.assertEqual(request.get_header("Idempotency-key"), KEY)
        self.assertEqual(json.loads(request.data), body)
        self.assertNotIn("withdrawalWaiverAccepted", json.loads(request.data))

    def test_idempotent_replay_is_returned_not_reposted(self):
        result = {**create_result(), "idempotent": True}
        client, opener = self.client(Response(result, status=200))
        self.assertTrue(client.create_order(order_body(), "external-handle-1")["idempotent"])
        self.assertEqual(len(opener.calls), 1)

    def test_explicit_optional_waiver_is_passed_without_changes(self):
        for waiver in (True, False):
            client, opener = self.client(Response(create_result(), status=201))
            client.create_order(order_body(withdrawalWaiverAccepted=waiver), KEY)
            self.assertIs(json.loads(opener.calls[0][0].data)["withdrawalWaiverAccepted"], waiver)

    def test_create_validation_refuses_before_any_dispatch(self):
        bodies = [order_body(productId="not-a-uuid"), order_body(billingCycle="monthly"),
                  order_body(hostname="not-fqdn"), order_body(hostname="8.8.8.8"),
                  order_body(hostname="bad_underscore.example.com"), order_body(acceptTerms=False),
                  order_body(acceptTerms=1), order_body(withdrawalWaiverAccepted="yes"),
                  order_body(items=[]), order_body(idempotencyKey="another-key"),
                  order_body(projectId="other-project"), order_body(configOptions={"x": float("nan")})]
        without_terms = order_body()
        without_terms.pop("acceptTerms")
        bodies.append(without_terms)
        for body in bodies:
            client, opener = self.client()
            with self.subTest(keys=list(body)), self.assertRaises(ValueError):
                client.create_order(body, KEY)
            self.assertEqual(opener.calls, [])
        for key in ("", "x" * 256, "with space", "bad\r\nAuthorization: leaked"):
            client, opener = self.client()
            with self.assertRaises(ValueError):
                client.create_order(order_body(), key)
            self.assertEqual(opener.calls, [])

    def test_mutation_timeouts_and_lost_reads_are_unknown_and_not_retried(self):
        failures = [TimeoutError(SECRET), urllib.error.URLError(SECRET),
                    OSError(SECRET), http.client.IncompleteRead(SECRET.encode())]
        for failure in failures:
            response = Response(create_result(), read_error=failure)
            client, opener = self.client(response if isinstance(failure, http.client.HTTPException) else failure)
            with self.subTest(failure=type(failure).__name__), self.assertRaises(provider.ProviderError) as caught:
                client.create_order(order_body(), KEY)
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertEqual(caught.exception.code, "TRANSPORT_ERROR")
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertEqual(len(opener.calls), 1)

    def test_5xx_mutations_unknown_but_4xx_are_definitively_rejected(self):
        for status in (400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504):
            raw = json.dumps({"success": False, "error": SECRET, "code": "PROVIDER_UNAVAILABLE"}).encode()
            response = Response(raw=raw, status=status)
            client, opener = self.client(response)
            with self.subTest(status=status), self.assertRaises(provider.ProviderError) as caught:
                client.create_order(order_body(), KEY)
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(caught.exception.outcome_unknown, status >= 500)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertEqual(len(opener.calls), 1)
            self.assertTrue(response.closed)

    def test_http_error_response_is_read_safely_without_error_message_leak(self):
        raw = json.dumps({"success": False, "code": "UNAUTHORIZED", "error": SECRET}).encode()
        error = urllib.error.HTTPError(provider.API_BASE + "/orders", 401, SECRET,
                                       headers(), io.BytesIO(raw))
        client, _ = self.client(error)
        with self.assertRaises(provider.ProviderError) as caught:
            client.create_order(order_body(), KEY)
        self.assertEqual(caught.exception.code, "UNAUTHORIZED")
        self.assertEqual(caught.exception.status, 401)
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_429_preserves_retry_after_header_or_body_without_retry(self):
        for response_headers, payload, seconds in (
                (headers(Retry_After="37"), {"retry_after_seconds": 90}, 37),
                (headers(), {"retry_after_seconds": 90}, 90),
                (headers(Retry_After="garbage"), {"retry_after_seconds": 11}, 11)):
            payload.update(success=False, code="ACTION_RATE_LIMITED", error=SECRET)
            client, opener = self.client(Response(raw=json.dumps(payload).encode(), status=429,
                                                 response_headers=response_headers))
            with self.assertRaises(provider.ProviderError) as caught:
                client.create_order(order_body(), KEY)
            self.assertEqual(caught.exception.retry_after, seconds)
            self.assertFalse(caught.exception.outcome_unknown)
            self.assertEqual(len(opener.calls), 1)

    def test_explicit_waiver_required_error_is_actionable_without_auto_consent(self):
        client, opener = self.client(Response(status=400, raw=json.dumps({"success": False,
            "code": "WITHDRAWAL_WAIVER_REQUIRED", "error": SECRET}).encode()))
        with self.assertRaises(provider.ProviderError) as caught:
            client.create_order(order_body(), KEY)
        self.assertEqual(caught.exception.code, "WITHDRAWAL_WAIVER_REQUIRED")
        self.assertIn("explicit owner consent", str(caught.exception))
        self.assertNotIn("withdrawalWaiverAccepted", json.loads(opener.calls[0][0].data))

    def test_unknown_error_codes_and_raw_error_details_are_not_echoed(self):
        client, _ = self.client(Response(status=400, raw=json.dumps({"success": False,
            "code": SECRET, "error": SECRET, "details": [{"field": SECRET, "message": SECRET}]}).encode()))
        with self.assertRaises(provider.ProviderError) as caught:
            client.create_order(order_body(), KEY)
        self.assertEqual(caught.exception.code, "PROVIDER_ERROR")
        self.assertNotIn(SECRET, repr(caught.exception))
        self.assertNotIn(SECRET, repr(vars(caught.exception)))

    def test_redirects_never_trigger_second_call_or_expose_location(self):
        for status in (301, 302, 303, 307, 308):
            client, opener = self.client(Response(status=status, raw=b"", response_headers=headers(
                Location="https://evil.example/?token=" + SECRET)))
            with self.subTest(status=status), self.assertRaises(provider.ProviderError) as caught:
                client.create_order(order_body(), KEY)
            self.assertEqual(caught.exception.code, "REDIRECT_BLOCKED")
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertEqual(len(opener.calls), 1)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_response_size_capped_and_mutation_remains_unknown(self):
        for response in (Response(raw=b"x" * (provider.MAX_RESPONSE_BYTES + 2)),
                         Response({}, response_headers=headers(Content_Length=provider.MAX_RESPONSE_BYTES + 1))):
            client, _ = self.client(response)
            with self.assertRaises(provider.ProviderError) as caught:
                client.create_order(order_body(), KEY)
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertTrue(response.closed)
            self.assertIn(response.read_sizes, ([], [provider.MAX_RESPONSE_BYTES + 1]))

    def test_oversized_429_still_preserves_definitive_rejection_and_retry_after(self):
        response = Response(status=429, response_headers=headers(
            Content_Length=provider.MAX_RESPONSE_BYTES + 1, Retry_After="42"))
        client, _ = self.client(response)
        with self.assertRaises(provider.ProviderError) as caught:
            client.create_order(order_body(), KEY)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 42)
        self.assertFalse(caught.exception.outcome_unknown)

    def test_invalid_json_and_wrong_envelopes_never_confirm_mutation(self):
        for raw in (b"not JSON", b'{"success":true}', b'{"success":true,"data":null}',
                    b'{"success":true,"data":{},"success":false}',
                    b'{"success":true,"data":{"value":NaN}}', b'{"success":false,"data":{}}'):
            client, _ = self.client(Response(raw=raw))
            with self.subTest(raw=raw), self.assertRaises(provider.ProviderError) as caught:
                client.create_order(order_body(), KEY)
            self.assertTrue(caught.exception.outcome_unknown)

    def test_read_failures_do_not_claim_unknown_mutation(self):
        client, _ = self.client(TimeoutError(SECRET))
        with self.assertRaises(provider.ProviderError) as caught:
            client.identity()
        self.assertFalse(caught.exception.outcome_unknown)

    def test_cancel_explicit_immediate_exact_id_and_body(self):
        for immediate in (True, False):
            client, opener = self.client(Response({"immediate": immediate}))
            client.cancel(SERVICE, "candidate rejected", immediate=immediate)
            request, _ = opener.calls[0]
            self.assertEqual(request.full_url, provider.API_BASE + "/services/" + SERVICE + "/cancel")
            self.assertEqual(request.method, "POST")
            self.assertEqual(json.loads(request.data), {"reason": "candidate rejected", "immediate": immediate})

    def test_unsafe_ids_and_cancel_reason_are_rejected_before_dispatch(self):
        client, opener = self.client()
        for method in (client.product, client.order, client.vps):
            with self.assertRaises(ValueError):
                method("../different-project")
        for args in ((SERVICE, "single"), (SERVICE, "valid reason", "true"), ("bad", "valid reason")):
            with self.assertRaises(ValueError):
                client.cancel(*args)
        self.assertEqual(opener.calls, [])

    def test_order_service_mapping_uses_only_documented_create_or_detail_services(self):
        self.assertEqual(provider.parse_order_service(create_result()), SERVICE)
        self.assertEqual(provider.parse_order_service({"order": {"id": ORDER, "services": [{"id": SERVICE}]}}), SERVICE)
        for data in ({"order": {"id": ORDER}}, {"order": {"id": ORDER}, "services": []},
                     {"order": {"id": ORDER}, "services": [{"id": SERVICE}, {"id": PRODUCT}]},
                     {"order": {"id": ORDER, "services": [{"id": PRODUCT}]}, "services": [{"id": SERVICE}]},
                     {"order": {"id": ORDER}, "services": [{"service_id": SERVICE}]},
                     {"order": {"id": ORDER, "items": [{"id": SERVICE}]}}):
            with self.subTest(data=data), self.assertRaises(provider.ProviderError):
                provider.parse_order_service(data)

    def test_vps_running_requires_machine_running_and_billing_active(self):
        normalized = provider.parse_vps(detail_vps())
        self.assertEqual(normalized, {"service_id": SERVICE, "ip": "8.8.8.8", "running": True,
            "state": "running", "status": "active", "project_id": PROJECT, "project_slug": "fleet"})
        for changes in ({"instance": None}, {"instance": {"state": "stopped"}}, {"status": "suspended"}):
            self.assertFalse(provider.parse_vps(detail_vps(**changes))["running"])

    def test_vps_ip_mapping_does_not_guess_other_provider_fields(self):
        missing = detail_vps(primary_ip=None, ip="8.8.4.4", ipv4="8.8.4.4")
        self.assertIsNone(provider.parse_vps(missing)["ip"])
        entries = [{"address": "8.8.4.4", "version": 4, "purpose": "public_ip"}]
        self.assertEqual(provider.parse_vps(detail_vps(primary_ip=None, ip_addresses=entries))["ip"], "8.8.4.4")
        self.assertEqual(provider.parse_vps(detail_vps(ip_addresses=entries))["ip"], "8.8.8.8")
        for data in (detail_vps(primary_ip="garbage"), detail_vps(id="bad"),
                     detail_vps(primary_ip=None, ip_addresses=entries + [{"address": "1.1.1.1", "version": 4, "purpose": "public_ip"}])):
            with self.assertRaises(provider.ProviderError):
                provider.parse_vps(data)


if __name__ == "__main__":
    unittest.main()
