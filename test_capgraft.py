"""CapGraft 的端到端检查，只使用 unittest 与真实 Worker 进程。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import capgraft


PROJECT_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = PROJECT_DIR / "examples" / "word_count"
TRUSTED_FILE = EXAMPLE_DIR / "trusted.json"


class CapgraftEndToEndTest(unittest.TestCase):
    """覆盖质量证据、独立门禁、调用和自动回滚。"""

    def setUp(self) -> None:
        """每个用例使用独立状态目录，避免审计事件互相污染。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / ".capgraft"
        capgraft.init_root(self.root)

    def tearDown(self) -> None:
        """清理测试自己创建的临时文件。"""
        self.temporary.cleanup()

    def _install_trust(self) -> None:
        """模拟控制面显式审批受信任门禁。"""
        capgraft.install_trusted_gate(
            self.root, "text.word_count", TRUSTED_FILE, "test-reviewer"
        )

    def _activate_v1(self) -> None:
        """建立文本统计能力的 known_good v1。"""
        self._install_trust()
        capgraft.publish(self.root, EXAMPLE_DIR)
        capgraft.activate(self.root, "text.word_count", "v1")

    def _make_self_certified_candidate(self, version: str) -> Path:
        """生成候选自证通过但受信任门禁必然失败的版本。"""
        candidate = self.base / f"candidate_{version}"
        shutil.copytree(EXAMPLE_DIR, candidate)
        spec_path = candidate / "capability.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["version"] = version
        for case in spec["tests"]:
            if "expect" in case:
                case["expect"]["characters"] += 1
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        adapter_path = candidate / "adapter.py"
        adapter = adapter_path.read_text(encoding="utf-8")
        adapter = adapter.replace('"characters": len(text)', '"characters": len(text) + 1')
        adapter_path.write_text(adapter, encoding="utf-8")
        return candidate

    def _events(self) -> list[dict]:
        """读取真实 JSONL 审计日志。"""
        lines = (self.root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def _run(self, run_id: str) -> dict:
        """读取不可变验证证据。"""
        return json.loads((self.root / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))

    def test_known_good_links_to_complete_evidence(self) -> None:
        """known_good 必须能追溯到代码摘要、摩擦 ID 和双层测试。"""
        self._activate_v1()
        result = capgraft.invoke(self.root, "text.word_count", {"text": "hello world"})
        self.assertEqual(result, {"words": 2, "characters": 11})
        registry = capgraft.load_registry(self.root)
        good = registry["known_good"]["text.word_count"]
        self.assertEqual(good["version"], "v1")
        self.assertEqual(len(good["release_sha256"]), 64)
        report = self._run(good["verification_run_id"])
        self.assertEqual(report["result"], "pass")
        self.assertEqual(report["friction_id"], "FR-0007")
        self.assertEqual(report["release_sha256"], good["release_sha256"])
        self.assertEqual({case["layer"] for case in report["tests"]}, {"candidate", "trusted"})
        self.assertTrue(all(case["attempt"] == 1 for case in report["tests"]))
        activated = [e for e in self._events() if e["kind"] == "capability_activated"][-1]
        self.assertIn(good["verification_run_id"], activated["verification_run_ids"])
        with self.assertRaises(capgraft.CapgraftError):
            capgraft.invoke(self.root, "text.word_count", {"text": 123})
        with self.assertRaises(capgraft.CapgraftError):
            capgraft.publish(self.root, EXAMPLE_DIR)
        adapter = self.root / "releases" / "text.word_count" / "v1" / "adapter.py"
        adapter.write_text(adapter.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(capgraft.CapgraftError, "偏离 known_good"):
            capgraft.invoke(self.root, "text.word_count", {"text": "hello world"})
        with self.assertRaisesRegex(capgraft.CapgraftError, "偏离 known_good"):
            capgraft.verify(self.root, "text.word_count", "v1")

    def test_verify_appends_new_evidence_without_overwrite(self) -> None:
        """复现验证必须生成新 run，而不是改写历史结果。"""
        self._activate_v1()
        first = capgraft.load_registry(self.root)["known_good"]["text.word_count"]["verification_run_id"]
        report = capgraft.verify(self.root, "text.word_count", "v1")
        self.assertNotEqual(report["run_id"], first)
        self.assertEqual(report["phase"], "verify")
        self.assertEqual(report["result"], "pass")
        self.assertTrue((self.root / "runs" / f"{first}.json").is_file())
        self.assertTrue((self.root / "runs" / f"{report['run_id']}.json").is_file())
        self.assertIn("verify", report["reproduce"])

    def test_candidate_self_proof_cannot_bypass_trusted_gate(self) -> None:
        """候选修改自身测试仍必须在受信任门禁处失败并回滚。"""
        self._activate_v1()
        candidate = self._make_self_certified_candidate("v2")
        capgraft.publish(self.root, candidate)
        with self.assertRaisesRegex(capgraft.CapgraftError, "已自动回退"):
            capgraft.activate(self.root, "text.word_count", "v2")
        registry = capgraft.load_registry(self.root)
        good = registry["known_good"]["text.word_count"]
        self.assertEqual(registry["active"]["text.word_count"], "v1")
        self.assertEqual(good["version"], "v1")
        self.assertEqual(registry["revision"], 4)
        rollback = [e for e in self._events() if e["kind"] == "capability_rolled_back"][-1]
        self.assertEqual(rollback["to_verification_run_id"], good["verification_run_id"])
        self.assertEqual(len(rollback["failed_run_ids"]), 1)
        failed = self._run(rollback["failed_run_ids"][0])
        self.assertEqual(failed["result"], "failed")
        self.assertTrue(any(c["layer"] == "trusted" and c["result"] == "failed" for c in failed["tests"]))
        published = [e for e in self._events() if e["kind"] == "capability_published" and e["capability_version"] == "v2"][-1]
        self.assertEqual(self._run(published["verification_run_id"])["result"], "pass")
        result = capgraft.invoke(self.root, "text.word_count", {"text": "still works"})
        self.assertEqual(result["words"], 2)

    def test_failed_first_activation_leaves_capability_inactive(self) -> None:
        """没有旧版可退时，首个坏版本不得残留为现役能力。"""
        self._install_trust()
        candidate = self._make_self_certified_candidate("v1")
        capgraft.publish(self.root, candidate)
        with self.assertRaisesRegex(capgraft.CapgraftError, "已自动回退"):
            capgraft.activate(self.root, "text.word_count", "v1")
        registry = capgraft.load_registry(self.root)
        self.assertNotIn("text.word_count", registry["active"])
        self.assertNotIn("text.word_count", registry["known_good"])
        rollback = [e for e in self._events() if e["kind"] == "capability_rolled_back"][-1]
        self.assertIsNone(rollback["to_version"])
        self.assertTrue(rollback["failed_run_ids"])

    def test_activation_requires_trusted_gate(self) -> None:
        """只有候选自证时不得激活，也不得产生半截现役状态。"""
        capgraft.publish(self.root, EXAMPLE_DIR)
        with self.assertRaisesRegex(capgraft.CapgraftError, "缺少受信任门禁"):
            capgraft.activate(self.root, "text.word_count", "v1")
        registry = capgraft.load_registry(self.root)
        self.assertEqual(registry["active"], {})
        self.assertEqual(registry["known_good"], {})


if __name__ == "__main__":
    unittest.main()
