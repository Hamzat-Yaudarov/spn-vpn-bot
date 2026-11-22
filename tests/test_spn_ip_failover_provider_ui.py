import unittest

from spn_ip_failover.deploy.provider_ui import (
    COLUMNS, PUBLIC_IPV4, InventoryError, assert_xorek_url, parse_rows, project_cells, service_heading, service_url,
)


class XorekInventoryTests(unittest.TestCase):
    def test_service_navigation_uses_exact_origin_and_numeric_id(self):
        url = service_url("12345")
        self.assertEqual(url, "https://my.xorek.cloud/billmgr?startform=service.ip&elid=12345")
        assert_xorek_url(url)
        for invalid in ("12345&elid=321", "https://somewhere.example", "0"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                service_url(invalid)
        for invalid in ("https://my.xorek.cloud.evil.example/billmgr", "https://my.xorek.cloud/payments"):
            with self.subTest(invalid=invalid), self.assertRaises(InventoryError):
                assert_xorek_url(invalid)

    def test_heading_requires_selected_service_id_after_name(self):
        heading = service_heading("12345")
        self.assertRegex("IP-адреса - VPS #12345 (Активен)", heading)
        self.assertNotRegex("IP-адреса - VPS #123450 (Активен)", heading)
        self.assertNotRegex("IP-адреса - VPS #12345 example #56789 (Активен)", heading)

    def test_parse_address_rows_suppresses_domain_and_rejects_duplicate_or_partial_rows(self):
        row = ["192.0.2.1", "255.255.255.0", "192.0.2.254", "customer.example", PUBLIC_IPV4, "Активен"]
        self.assertEqual(parse_rows([row]), [{
            "ip": "192.0.2.1", "mask": "255.255.255.0", "gateway": "192.0.2.254",
            "type": PUBLIC_IPV4, "status": "Активен",
        }])
        for invalid in ([], [row, row], [row[:-1]], [["не IP", *row[1:]]]):
            with self.subTest(invalid=invalid), self.assertRaises(InventoryError):
                parse_rows(invalid)

    def test_split_table_body_can_have_a_leading_selector_cell(self):
        self.assertEqual(project_cells(COLUMNS), list(COLUMNS))
        self.assertEqual(project_cells([" ", *COLUMNS]), list(COLUMNS))
        with self.assertRaises(InventoryError):
            project_cells(["unrecognized column", *COLUMNS])


if __name__ == "__main__":
    unittest.main()
