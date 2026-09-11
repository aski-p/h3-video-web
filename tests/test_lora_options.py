import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class LoraOptionsTests(unittest.TestCase):
    def test_windows_inventory_reports_legacy_camera_aliases_as_canonical(self):
        path = Path(__file__).resolve().parents[1] / "windows-worker" / "h3_worker.py"
        spec = importlib.util.spec_from_file_location("h3_worker_lora_alias_test", path)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "loras").mkdir()
            (root / "loras" / "cam_motion_1000.safetensors").write_bytes(b"x")
            (root / "loras" / "cam_motion_3000.safetensors").write_bytes(b"x")
            installed = worker.installed_optional_loras(root)
        self.assertIn("camera_motion_h3_lora_v1_1000_pruned.safetensors", installed)
        self.assertIn("camera_motion_h3_lora_v1_3000_pruned.safetensors", installed)

    def test_public_pgx_catalog_recognizes_legacy_camera_aliases(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "cam_motion_1000.safetensors").write_bytes(b"x")
            Path(td, "cam_motion_3000.safetensors").write_bytes(b"x")
            with patch.object(server, "PGX_LORA_DIRS", (td,)):
                items = {item["id"]: item for item in server.public_lora_catalog()["items"]}
        self.assertTrue(items["camera_motion"]["installed"]["pgx"]["1000"])
        self.assertTrue(items["camera_motion"]["installed"]["pgx"]["3000"])

    def test_catalog_uses_allowlisted_real_filenames_and_versions(self):
        self.assertEqual(server.LORA_CATALOG["camera_motion"]["versions"]["1000"], "camera_motion_h3_lora_v1_1000_pruned.safetensors")
        self.assertEqual(server.LORA_CATALOG["camera_motion"]["versions"]["3000"], "camera_motion_h3_lora_v1_3000_pruned.safetensors")
        self.assertEqual(server.LORA_CATALOG["spatial_physics"]["versions"]["3000"], "wushu_spatial_physics_clean_3000_pruned.safetensors")
        self.assertNotIn("url", str(server.LORA_CATALOG).lower())

    def test_normalization_rejects_unknown_ids_bad_types_ranges_and_versions(self):
        good = {"better_motion": {"enabled": True, "strength": 0.6}}
        self.assertTrue(server.normalize_lora_options(good)["better_motion"]["enabled"])
        for bad in (
            {"arbitrary": {"enabled": True, "strength": 1}},
            {"better_motion": {"enabled": "true", "strength": 1}},
            {"better_motion": {"enabled": True, "strength": float("nan")}},
            {"better_motion": {"enabled": True, "strength": 2.01}},
            {"camera_motion": {"enabled": True, "strength": 1, "version": "../../x"}},
            {"camera_motion": {"enabled": True, "strength": 1, "version": ["1000", "3000"]}},
        ):
            with self.assertRaises(ValueError):
                server.normalize_lora_options(bad)

    def test_workflow_chains_only_enabled_positive_loras_and_updates_both_model_consumers(self):
        options = server.normalize_lora_options({
            "realism": {"enabled": True, "strength": 0.8},
            "better_motion": {"enabled": True, "strength": 0.6},
            "insta_tiktok": {"enabled": False, "strength": 1.0},
            "motion_repair": {"enabled": True, "strength": 0},
            "camera_motion": {"enabled": True, "strength": 0.7, "version": "1000", "movement": "tracking"},
            "spatial_physics": {"enabled": True, "strength": 0.4, "version": "3000"},
        })
        with tempfile.TemporaryDirectory() as td:
            for item in server.selected_loras(options):
                Path(td, item["filename"]).write_bytes(b"safe")
            Path(td, server.H3_LORA).write_bytes(b"turbo")
            wf = server.build_workflow("A woman walks.", "", 768, 1344, 49, 8, 1,
                                       lora_options=options, lora_dirs=[td], strict_loras=True)
        loaders = [node for node in wf.values() if node["class_type"] == "LoraLoaderModelOnly"]
        self.assertEqual([x["inputs"]["lora_name"] for x in loaders], [
            server.H3_LORA,
            server.LORA_CATALOG["realism"]["filename"],
            server.LORA_CATALOG["better_motion"]["filename"],
            server.LORA_CATALOG["camera_motion"]["versions"]["1000"],
            server.LORA_CATALOG["spatial_physics"]["versions"]["3000"],
        ])
        final_ref = [list(wf)[-1], 0]
        self.assertEqual(wf["8"]["inputs"]["model"], final_ref)
        self.assertEqual(wf["9"]["inputs"]["model"], final_ref)
        prompt = wf["5"]["inputs"]["prompt"]
        self.assertTrue(prompt.startswith("camera motion,"))
        self.assertEqual(prompt.lower().count("camera motion"), 1)
        self.assertEqual(prompt.lower().count("r34l1sm"), 1)

    def test_off_and_zero_strength_create_no_optional_loader_or_trigger(self):
        options = server.normalize_lora_options({
            "camera_motion": {"enabled": False, "strength": 1, "version": "1000", "movement": "tracking"},
            "better_motion": {"enabled": True, "strength": 0},
        })
        with tempfile.TemporaryDirectory() as td:
            Path(td, server.H3_LORA).write_bytes(b"turbo")
            wf = server.build_workflow("camera motion should not be injected", "", 768, 1344, 49, 6, 1,
                                       lora_options=options, lora_dirs=[td], strict_loras=True)
        loaders = [node for node in wf.values() if node["class_type"] == "LoraLoaderModelOnly"]
        self.assertEqual(len(loaders), 1)
        self.assertFalse(wf["5"]["inputs"]["prompt"].lower().startswith("camera motion,"))

    def test_selected_lora_availability_is_target_specific(self):
        options = server.normalize_lora_options({"motion_repair": {"enabled": True, "strength": 0.9}})
        with tempfile.TemporaryDirectory() as td:
            with patch.object(server, "PGX_LORA_DIRS", (td,)):
                missing = server.missing_loras_for_target("pgx", options)
                self.assertEqual(missing[0]["id"], "motion_repair")
                Path(td, server.LORA_CATALOG["motion_repair"]["filename"]).write_bytes(b"x")
                self.assertEqual(server.missing_loras_for_target("pgx", options), [])

    def test_applied_snapshot_distinguishes_enabled_zero_and_keeps_exact_versions(self):
        requested = server.normalize_lora_options({
            "realism": {"enabled": True, "strength": 0.0},
            "camera_motion": {"enabled": True, "strength": 0.45, "version": "3000", "movement": "tracking"},
            "spatial_physics": {"enabled": True, "strength": 0.3, "version": "1000"},
        })
        applied = server.applied_lora_options(requested)
        self.assertTrue(requested["realism"]["enabled"])
        self.assertFalse(applied["realism"]["enabled"])
        self.assertEqual(applied["camera_motion"]["version"], "3000")
        self.assertEqual(applied["spatial_physics"]["version"], "1000")

    def test_steps_reject_bool_fraction_and_out_of_range(self):
        for value in (True, 1, 21, 6.5, "6"):
            with self.assertRaises(ValueError):
                server.normalize_steps(value)
        self.assertEqual(server.normalize_steps(2), 2)
        self.assertEqual(server.normalize_steps(20), 20)


if __name__ == "__main__":
    unittest.main()
