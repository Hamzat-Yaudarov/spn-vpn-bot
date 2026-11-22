import copy
import json
from pathlib import Path
import tempfile
import unittest

import panel_plan as panel
import prepare


def snapshot():
    return {
        "format": 1,
        "profiles": [{"uuid": panel.SOURCE_PROFILE, "name": "source",
                      "inbounds": [{"uuid": "source-inbound", "tag": "source-xhttp"}]}],
        "nodes": [],
        "hosts": [{"uuid": panel.SOURCE_HOST, "remark": "source", "address": "source.example",
                   "port": 443, "securityLayer": "TLS", "alpn": "h2", "fingerprint": "chrome",
                   "xhttpExtraParamsEncoding": "json-string",
                   "inbound": {"configProfileUuid": panel.SOURCE_PROFILE,
                               "configProfileInboundUuid": "source-inbound"}}],
        "squads": [{"uuid": "test-de", "name": "DE", "inbounds": [{"uuid": "old-inbound"}]}],
    }


class PanelPlanGuards(unittest.TestCase):
    def test_only_new_disabled_de_restricted_objects_and_explicit_uuid_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = prepare.prepare(Path(directory) / "stage")
            plan = panel.build_plan(snapshot(), stage, "test-de1.chislo.site")
            host = plan["host.create.template.json"]
            self.assertTrue(host["isDisabled"])
            self.assertEqual(host["internalSquads"], {"mode": "ALLOW_ONLY", "squads": ["test-de"]})
            self.assertIsNone(host["xrayJsonTemplateUuid"])
            self.assertEqual(plan["dedicated-selector.template.json"]["selector"],
                             {"type": "uuids", "values": [panel.NEW_HOST]})
            self.assertEqual(plan["de-squad.later-plan.json"]["body"]["inbounds"],
                             ["old-inbound", panel.NEW_INBOUND])
            self.assertFalse(plan["plan.json"]["existingObjectWritesAllowed"])
            self.assertFalse(plan["plan.json"]["applyImplemented"])
            self.assertIsInstance(host["xhttpExtraParams"], str)
            object_source = snapshot()
            object_source["hosts"][0]["xhttpExtraParamsEncoding"] = "object"
            object_plan = panel.build_plan(object_source, stage, "test-de1.chislo.site")
            self.assertIsInstance(object_plan["host.create.template.json"]["xhttpExtraParams"], dict)

    def test_collisions_and_origin_or_unapproved_delivery_domains_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = prepare.prepare(Path(directory) / "stage")
            for domain in (prepare.DOMAIN, "other.example", "chislo.site", "bad.chislo.site/path"):
                with self.subTest(domain=domain), self.assertRaises(ValueError):
                    panel.build_plan(snapshot(), stage, domain)
            collisions = (("profiles", {"name": panel.PROFILE}),
                          ("nodes", {"name": "other", "address": prepare.ORIGIN_IP}),
                          ("hosts", {"remark": panel.HOST}))
            for kind, item in collisions:
                state = snapshot()
                state[kind].append(item)
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    panel.build_plan(state, stage, "test-de1.chislo.site")

    def test_metadata_projection_never_exports_raw_configs_keys_or_members(self):
        raw = copy.deepcopy(snapshot())
        raw["profiles"][0]["config"] = {"privateKey": "synthetic-server-secret",
                                         "clients": [{"id": "synthetic-subscriber-key"}]}
        raw["hosts"][0]["unexpectedSecret"] = "synthetic-host-secret"
        raw["squads"][0]["members"] = [{"username": "synthetic-user"}]
        saved = json.dumps(panel.metadata_snapshot(raw))
        for text in ("synthetic-server-secret", "synthetic-subscriber-key", "synthetic-host-secret", "synthetic-user", "privateKey"):
            self.assertNotIn(text, saved)


if __name__ == "__main__":
    unittest.main()
