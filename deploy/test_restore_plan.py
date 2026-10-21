from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from deploy.restore_plan import RestorePlanError, load_service_mapping, normalize_restore_plan

IMAGE = "sha256:" + "1" * 64
ALLOWED = {"bot", "wrapper", "legacy-wrapper", "lxserver", "sqmusic-server"}


def fixture(service="legacy-vm", project="old-downloads", identifier="main", path="/new/data/vm"):
    return {"projects": [{"id": identifier, "project_name": project}], "source_map": {"/old/vm": path},
            "images_included": True, "image_archive": "/new/data/_images/images.tar",
            "mounts": [{"project_id": identifier, "service": service, "target": "/data", "source": "/old/vm",
                        "restored_path": path, "included": True, "read_only": False}],
            "images": [{"project": project, "service": service, "image_id": IMAGE}]}


class RestoreServiceMappingTests(unittest.TestCase):
    def test_same_name_suite_requires_no_mapping_and_keeps_all_records(self):
        plan = fixture(service="bot")
        result = normalize_restore_plan(plan, allowed_services=ALLOWED)
        self.assertEqual(result["mounts"][0]["service"], "bot")
        self.assertEqual(result["images"][0]["service"], "bot")
        self.assertEqual(result["source_map"], plan["source_map"])
        self.assertNotIn("original_service", plan["mounts"][0])

    def test_explicit_legacy_vm_and_adapter_mapping_is_applied_to_mounts_and_images(self):
        plan = fixture()
        plan["mounts"].append({**plan["mounts"][0], "service": "wrapper-adapter", "target": "/session",
                               "source": "/old/session", "restored_path": "/new/data/session"})
        plan["images"].append({**plan["images"][0], "service": "wrapper-adapter"})
        result = normalize_restore_plan(plan, {"legacy-vm": "legacy-wrapper", "wrapper-adapter": "wrapper"}, allowed_services=ALLOWED)
        self.assertEqual({item["service"] for item in result["mounts"]}, {"legacy-wrapper", "wrapper"})
        self.assertEqual({item["service"] for item in result["images"]}, {"legacy-wrapper", "wrapper"})
        self.assertEqual(result["mounts"][0]["original_service"], "legacy-vm")

    def test_legacy_vm_alias_requires_full_image_archive_and_matching_original_image(self):
        mapping = {"legacy-vm": "legacy-wrapper"}
        cases = [
            {"images_included": False},
            {"images_included": "true"},
            {"image_archive": None},
            {"image_archive": ""},
            {"images": []},
            {"images": [{"project": "other-project", "service": "legacy-vm", "image_id": IMAGE}]},
            {"images": [{"project": "old-downloads", "service": "legacy-wrapper", "image_id": IMAGE}]},
        ]
        for changes in cases:
            plan = fixture()
            plan.update(changes)
            original = deepcopy(plan)
            with self.subTest(changes=changes), self.assertRaisesRegex(RestorePlanError, "original image archive"):
                normalize_restore_plan(plan, mapping, allowed_services=ALLOWED)
            self.assertEqual(plan, original)
        plan = fixture()
        plan["images"][0]["image_id"] = "invalid-image"
        with self.assertRaisesRegex(RestorePlanError, "image identity"):
            normalize_restore_plan(plan, mapping, allowed_services=ALLOWED)

    def test_canonical_managed_vm_without_images_remains_supported(self):
        for image_metadata in ([], [{"project": "old-downloads", "service": "legacy-wrapper", "image_id": IMAGE}]):
            plan = fixture(service="legacy-wrapper")
            plan.update({"images_included": False, "image_archive": None, "images": image_metadata})
            result = normalize_restore_plan(plan, allowed_services=ALLOWED)
            self.assertEqual(result["mounts"][0]["service"], "legacy-wrapper")
            self.assertEqual(len(result["images"]), len(image_metadata))
            self.assertFalse(result["images_included"])

    def test_legacy_vm_image_requirement_survives_normalized_resume(self):
        mapping = {"legacy-vm": "legacy-wrapper"}
        result = normalize_restore_plan(fixture(), mapping, allowed_services=ALLOWED)
        self.assertEqual(normalize_restore_plan(result, mapping, allowed_services=ALLOWED), result)
        result["images_included"] = False
        with self.assertRaisesRegex(RestorePlanError, "original image archive"):
            normalize_restore_plan(result, mapping, allowed_services=ALLOWED)

    def test_unknown_persistent_service_cannot_silently_disappear(self):
        with self.assertRaisesRegex(RestorePlanError, "explicit restore_services"):
            normalize_restore_plan(fixture(), allowed_services=ALLOWED)

    def test_unselected_optional_media_requires_the_explicit_install_flag(self):
        for included in (True, False):
            plan = fixture(service="navidrome")
            plan["mounts"][0]["included"] = included
            with self.assertRaisesRegex(RestorePlanError, "with-media-servers"):
                normalize_restore_plan(plan, allowed_services=ALLOWED)
        result = normalize_restore_plan(plan, allowed_services=ALLOWED | {"navidrome"})
        self.assertEqual(result["mounts"][0]["service"], "navidrome")

    def test_project_id_and_compose_name_qualifiers_are_supported(self):
        for key in ("main/legacy-vm", "old-downloads/legacy-vm"):
            result = normalize_restore_plan(fixture(), {key: "legacy-wrapper"}, allowed_services=ALLOWED)
            self.assertEqual(result["mounts"][0]["service"], "legacy-wrapper")

    def test_conflicting_qualifiers_are_rejected(self):
        mapping = {"main/legacy-vm": "legacy-wrapper", "old-downloads/legacy-vm": "wrapper"}
        with self.assertRaisesRegex(RestorePlanError, "Ambiguous"):
            normalize_restore_plan(fixture(), mapping, allowed_services=ALLOWED)

    def test_unknown_targets_and_unmatched_mapping_typos_are_rejected(self):
        for mapping in ({"legacy-vm": "unrelated-daemon"}, {"typo": "bot"}):
            with self.assertRaises(RestorePlanError):
                normalize_restore_plan(fixture(service="bot"), mapping, allowed_services=ALLOWED)

    def test_many_sources_cannot_overwrite_one_mount_or_disagree_about_image(self):
        plan = fixture()
        plan["mounts"].append({**plan["mounts"][0], "service": "second-vm", "restored_path": "/new/other-data"})
        mapping = {"legacy-vm": "legacy-wrapper", "second-vm": "legacy-wrapper"}
        with self.assertRaisesRegex(RestorePlanError, "same service mount"):
            normalize_restore_plan(plan, mapping, allowed_services=ALLOWED)
        plan = fixture()
        plan["images"].append({**plan["images"][0], "service": "second-vm", "image_id": "sha256:" + "2" * 64})
        with self.assertRaisesRegex(RestorePlanError, "Different old service images"):
            normalize_restore_plan(plan, mapping, allowed_services=ALLOWED)

    def test_identical_shared_records_deduplicate_and_remain_idempotent_on_resume(self):
        plan = fixture()
        plan["mounts"].append({**plan["mounts"][0], "service": "second-vm"})
        plan["images"].append({**plan["images"][0], "service": "second-vm"})
        mapping = {"legacy-vm": "legacy-wrapper", "second-vm": "legacy-wrapper"}
        first = normalize_restore_plan(plan, mapping, allowed_services=ALLOWED)
        second = normalize_restore_plan(first, mapping, allowed_services=ALLOWED)
        self.assertEqual(first, second)
        self.assertEqual(len(first["mounts"]), 1)
        self.assertEqual(len(first["images"]), 1)

    def test_private_configuration_returns_only_service_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            path.write_text(json.dumps({"env": {"TOKEN": "synthetic-private-value"},
                                        "restore_services": {"legacy-vm": "legacy-wrapper"}}))
            mapping = load_service_mapping(path)
        self.assertEqual(mapping, {"legacy-vm": "legacy-wrapper"})
        self.assertNotIn("synthetic-private-value", json.dumps(mapping))


if __name__ == "__main__":
    unittest.main()
