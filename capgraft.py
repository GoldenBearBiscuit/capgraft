#!/usr/bin/env python3
"""CapGraft：用标准库实现的能力发布、质量证据、调用与回滚 CLI。"""

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 2
GATE_POLICY_VERSION = 1
WORKER_TIMEOUT_SECONDS = 5
ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)*")
VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TRACE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
JSON_TYPES = {
    "str": str,
    "int": int,
    "number": (int, float),
    "bool": bool,
    "dict": dict,
    "list": list,
}


class CapgraftError(Exception):
    """表示可向 CLI 用户直接报告的预期错误。"""


def _require(condition: bool, message: str) -> None:
    """在信任边界拒绝不符合约定的数据。"""
    if not condition:
        raise CapgraftError(message)


def _utc_now() -> str:
    """生成统一的 UTC 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def _validate_schema_fields(schema: object, label: str) -> None:
    """校验简单的 JSON 对象字段类型声明。"""
    _require(isinstance(schema, dict), f"{label} 必须是对象")
    for field, type_name in schema.items():
        _require(isinstance(field, str) and bool(field), f"{label} 含无效字段名")
        _require(type_name in JSON_TYPES, f"{label}.{field} 使用了未知类型 {type_name}")


def _validate_payload(payload: object, schema: dict, label: str) -> None:
    """确保输入输出与公开能力契约完全一致。"""
    _require(isinstance(payload, dict), f"{label} 必须是 JSON 对象")
    missing = sorted(set(schema) - set(payload))
    extra = sorted(set(payload) - set(schema))
    _require(not missing, f"{label} 缺少字段: {', '.join(missing)}")
    _require(not extra, f"{label} 含未声明字段: {', '.join(extra)}")
    for field, type_name in schema.items():
        value = payload[field]
        if type_name == "number":
            valid = type(value) in (int, float) and math.isfinite(value)
        elif type_name == "int":
            valid = type(value) is int
        else:
            valid = isinstance(value, JSON_TYPES[type_name])
        _require(valid, f"{label}.{field} 应为 {type_name}")


def _validate_case_metadata(case: object, label: str) -> None:
    """校验用例名称、类型、来源及错误/期望结构。"""
    _require(isinstance(case, dict), f"{label} 必须是对象")
    _require(isinstance(case.get("name"), str) and case["name"], f"{label} 缺少名称")
    kind = case.get("kind")
    _require(kind in {"normal", "error", "counterexample"}, f"{label} 类型无效")
    _require(case.get("origin") in {"human", "agent"}, f"{label} 必须标记 human/agent 来源")
    if kind == "error":
        _require(isinstance(case.get("error"), str) and case["error"], f"{label} 缺少异常名")
        _require("expect" not in case, f"{label} 不能同时声明 error 和 expect")
    else:
        _require("error" not in case, f"{label} 不应声明 error")
        _require("expect" in case, f"{label} 缺少 expect")


def _validate_case(case: object, spec: dict, label: str) -> None:
    """按能力契约校验一个正常、错误或反例测试用例。"""
    _validate_case_metadata(case, label)
    _validate_payload(case.get("input"), spec["input"], f"{label}.input")
    if case["kind"] != "error":
        _validate_payload(case.get("expect"), spec["output"], f"{label}.expect")


def _require_all_kinds(cases: list, label: str) -> None:
    """确保测试集合同时覆盖正常、错误和反例。"""
    kinds = {case["kind"] for case in cases}
    _require({"normal", "error", "counterexample"} <= kinds, f"{label} 必须包含正常、错误和反例测试")


def validate_spec(spec: object) -> None:
    """校验能力提案格式，候选测试只作为自证材料。"""
    _require(isinstance(spec, dict), "capability.json 顶层必须是对象")
    required = {
        "schema_version", "id", "version", "source", "friction_id", "friction",
        "input", "output", "tests",
    }
    _require(set(spec) == required, f"能力字段必须严格为: {', '.join(sorted(required))}")
    _require(spec["schema_version"] == SCHEMA_VERSION, "不支持的 schema_version")
    _require(isinstance(spec["id"], str) and ID_PATTERN.fullmatch(spec["id"]), "能力 id 无效")
    _require(
        isinstance(spec["version"], str) and VERSION_PATTERN.fullmatch(spec["version"]),
        "能力 version 无效",
    )
    source = spec["source"]
    _require(isinstance(source, dict) and set(source) == {"name", "revision", "license"}, "source 格式无效")
    for key in ("name", "revision", "license"):
        _require(isinstance(source[key], str) and source[key], f"source.{key} 不能为空")
    _require(
        isinstance(spec["friction_id"], str) and TRACE_ID_PATTERN.fullmatch(spec["friction_id"]),
        "friction_id 无效",
    )
    _require(isinstance(spec["friction"], str) and spec["friction"], "必须记录真实能力缺口")
    _validate_schema_fields(spec["input"], "input")
    _validate_schema_fields(spec["output"], "output")
    tests = spec["tests"]
    _require(isinstance(tests, list) and tests, "tests 必须是非空数组")
    for index, case in enumerate(tests):
        _validate_case(case, spec, f"tests[{index}]")
    _require_all_kinds(tests, "tests")


def load_spec(directory: Path) -> dict:
    """读取并验证能力契约及固定入口 adapter.py。"""
    try:
        spec = json.loads((directory / "capability.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapgraftError(f"无法读取 {directory / 'capability.json'}: {exc}") from exc
    validate_spec(spec)
    _require((directory / "adapter.py").is_file(), f"{directory} 缺少 adapter.py")
    return spec


def _release_path(root: Path, capability_id: str, version: str) -> Path:
    """构造受命名约束的发布路径，阻止路径穿越。"""
    _require(ID_PATTERN.fullmatch(capability_id), "能力 id 无效")
    _require(VERSION_PATTERN.fullmatch(version), "能力 version 无效")
    return root / "releases" / capability_id / version


def _release_digest(directory: Path) -> str:
    """对契约与适配器字节计算摘要，证明被测对象身份。"""
    digest = hashlib.sha256()
    for name in ("capability.json", "adapter.py"):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((directory / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _check_known_good_digest(registry: dict, capability_id: str, version: str, digest: str) -> None:
    """拒绝执行或回退到已偏离验证证据的 known_good 字节。"""
    good = registry.get("known_good", {}).get(capability_id)
    if good and good["version"] == version:
        _require(digest == good["release_sha256"], "发布内容已偏离 known_good 证据")


def _atomic_write_json(path: Path, value: object) -> None:
    """通过同目录临时文件和原子替换避免半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()


def init_root(root: Path) -> None:
    """初始化发布库、受信任门禁、证据目录和审计日志。"""
    (root / "releases").mkdir(parents=True, exist_ok=True)
    (root / "trusted").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    registry_path = root / "registry.json"
    if not registry_path.exists():
        _atomic_write_json(
            registry_path,
            {"schema_version": SCHEMA_VERSION, "revision": 0, "active": {}, "known_good": {}},
        )
    (root / "journal.jsonl").touch(exist_ok=True)


def load_registry(root: Path) -> dict:
    """读取当前激活版本及带证据引用的 known_good。"""
    try:
        registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapgraftError(f"无法读取注册表，请先运行 init: {exc}") from exc
    _require(isinstance(registry, dict), "注册表必须是对象")
    _require(registry.get("schema_version") == SCHEMA_VERSION, "注册表版本不受支持")
    _require(type(registry.get("revision")) is int and registry["revision"] >= 0, "注册表 revision 无效")
    _require(isinstance(registry.get("active"), dict), "注册表 active 无效")
    _require(isinstance(registry.get("known_good"), dict), "注册表 known_good 无效")
    return registry


def append_event(root: Path, kind: str, **fields: object) -> None:
    """追加审计事件并落盘，历史事件从不更新或删除。"""
    event = {
        "ts": _utc_now(),
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        **fields,
    }
    with (root / "journal.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def install_trusted_gate(root: Path, capability_id: str, gate_file: Path, approver: str) -> Path:
    """由控制面显式安装受信任门禁，并记录审批者与文件摘要。"""
    init_root(root)
    _require(ID_PATTERN.fullmatch(capability_id), "能力 id 无效")
    _require(isinstance(approver, str) and approver.strip(), "必须提供审批者")
    try:
        document = json.loads(gate_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapgraftError(f"无法读取受信任门禁: {exc}") from exc
    _require(isinstance(document, dict), "受信任门禁必须是对象")
    _require(set(document) == {"schema_version", "capability_id", "cases"}, "受信任门禁字段无效")
    _require(document["schema_version"] == SCHEMA_VERSION, "受信任门禁版本不受支持")
    _require(document["capability_id"] == capability_id, "受信任门禁能力 id 不一致")
    cases = document["cases"]
    _require(isinstance(cases, list) and cases, "受信任用例必须是非空数组")
    for index, case in enumerate(cases):
        _validate_case_metadata(case, f"trusted[{index}]")
    _require_all_kinds(cases, "受信任门禁")
    gate = {
        "schema_version": SCHEMA_VERSION,
        "capability_id": capability_id,
        "approved_by": approver.strip(),
        "approved_at": _utc_now(),
        "cases": cases,
    }
    path = root / "trusted" / f"{capability_id}.json"
    _atomic_write_json(path, gate)
    gate_sha256 = hashlib.sha256(
        json.dumps(cases, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    append_event(
        root,
        "trusted_gate_installed",
        capability_id=capability_id,
        approved_by=gate["approved_by"],
        gate_sha256=gate_sha256,
    )
    return path


def _load_trusted_gate(root: Path, capability_id: str, spec: dict) -> dict:
    """加载控制面门禁，并按当前公开契约重新校验。"""
    path = root / "trusted" / f"{capability_id}.json"
    try:
        gate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapgraftError(f"缺少受信任门禁，请先运行 trust: {exc}") from exc
    _require(isinstance(gate, dict), "受信任门禁必须是对象")
    _require(gate.get("schema_version") == SCHEMA_VERSION, "受信任门禁版本不受支持")
    _require(gate.get("capability_id") == capability_id, "受信任门禁能力 id 不一致")
    for key in ("approved_by", "approved_at"):
        _require(isinstance(gate.get(key), str) and gate[key], f"受信任门禁缺少 {key}")
    cases = gate.get("cases")
    _require(isinstance(cases, list) and cases, "受信任用例必须是非空数组")
    for index, case in enumerate(cases):
        _validate_case(case, spec, f"trusted[{index}]")
    _require_all_kinds(cases, "受信任门禁")
    return gate


def _worker_entry(release: Path) -> None:
    """在子进程中加载适配器，并通过 stdout 返回唯一 JSON 信封。"""
    try:
        payload = json.load(sys.stdin)
        adapter_path = release / "adapter.py"
        module_spec = importlib.util.spec_from_file_location("capgraft_adapter", adapter_path)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"无法加载 {adapter_path}")
        module = importlib.util.module_from_spec(module_spec)
        # 适配器的 print 不得污染机器可读 stdout 协议。
        with redirect_stdout(io.StringIO()):
            module_spec.loader.exec_module(module)
            value = module.run(payload)
        encoded = json.dumps({"ok": True, "value": value}, ensure_ascii=False, allow_nan=False)
    except Exception as exc:
        encoded = json.dumps(
            {"ok": False, "error_type": type(exc).__name__, "message": str(exc)},
            ensure_ascii=False,
        )
    sys.stdout.write(encoded)


def _call_worker(release: Path, payload: dict) -> dict:
    """调用故障隔离 Worker；该进程边界不是安全沙箱。"""
    command = [sys.executable, str(Path(__file__).resolve()), "_worker", str(release)]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
            text=True,
            capture_output=True,
            timeout=WORKER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CapgraftError(f"Worker 超过 {WORKER_TIMEOUT_SECONDS} 秒") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "无错误输出"
        raise CapgraftError(f"Worker 异常退出: {detail}")
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CapgraftError("Worker 未返回合法 JSON 信封") from exc
    _require(isinstance(envelope, dict) and type(envelope.get("ok")) is bool, "Worker 信封无效")
    return envelope


def _run_case(release: Path, spec: dict, case: dict, label: str) -> None:
    """执行一个行为用例并检查异常、输出类型和精确结果。"""
    envelope = _call_worker(release, case["input"])
    expected_error = case.get("error")
    if not envelope["ok"]:
        actual = f"{envelope.get('error_type')}: {envelope.get('message')}"
        if expected_error:
            _require(envelope.get("error_type") == expected_error, f"{label} 异常不符: {actual}")
            return
        raise CapgraftError(f"{label} 执行失败: {actual}")
    _require(not expected_error, f"{label} 应抛出 {expected_error}")
    value = envelope.get("value")
    _validate_payload(value, spec["output"], f"{label}.output")
    _require(value == case["expect"], f"{label} 期望 {case['expect']}，实际 {value}")


def _execute_case(release: Path, spec: dict, case: dict, layer: str) -> dict:
    """执行一次用例并生成证据；失败不重试、不覆盖。"""
    started = time.perf_counter()
    error = None
    try:
        _run_case(release, spec, case, f"{layer}:{case['name']}")
    except CapgraftError as exc:
        error = str(exc)
    return {
        "case_id": case["name"],
        "origin": case["origin"],
        "layer": layer,
        "result": "failed" if error else "pass",
        "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "attempt": 1,
        "error": error,
    }


def _run_verification(
    root: Path, directory: Path, spec: dict, phase: str, include_trusted: bool
) -> dict:
    """生成不可变 VerificationRun，并让审计事件引用它。"""
    trusted = _load_trusted_gate(root, spec["id"], spec) if include_trusted else None
    run_id = f"run_{uuid.uuid4().hex}"
    started_at = _utc_now()
    results = [
        _execute_case(directory, spec, case, "candidate")
        for case in spec["tests"]
    ]
    if trusted:
        results.extend(
            _execute_case(directory, spec, case, "trusted")
            for case in trusted["cases"]
        )
    result = "pass" if all(case["result"] == "pass" for case in results) else "failed"
    if phase == "publish":
        reproduce = [sys.executable, str(Path(__file__).resolve()), "--root", str(root.resolve()), "publish", str(directory.resolve())]
    else:
        reproduce = [sys.executable, str(Path(__file__).resolve()), "--root", str(root.resolve()), "verify", spec["id"], spec["version"]]
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "capability_id": spec["id"],
        "capability_version": spec["version"],
        "friction_id": spec["friction_id"],
        "source": spec["source"],
        "release_sha256": _release_digest(directory),
        "phase": phase,
        "policy_version": GATE_POLICY_VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "result": result,
        "tests": results,
        "trusted_gate": {
            "approved_by": trusted["approved_by"],
            "approved_at": trusted["approved_at"],
        } if trusted else None,
        "reproduce": reproduce,
    }
    run_path = root / "runs" / f"{run_id}.json"
    _atomic_write_json(run_path, report)
    append_event(
        root,
        "verification_completed",
        run_id=run_id,
        capability_id=spec["id"],
        capability_version=spec["version"],
        phase=phase,
        result=result,
        release_sha256=report["release_sha256"],
        run_path=str(run_path),
    )
    return report


def _check_active_releases(root: Path, active: dict) -> list[dict]:
    """重跑所有现役能力的候选测试和受信任门禁。"""
    registry = load_registry(root)
    reports = []
    for capability_id, version in active.items():
        release = _release_path(root, capability_id, version)
        spec = load_spec(release)
        _require(spec["id"] == capability_id and spec["version"] == version, "发布路径与契约不一致")
        _check_known_good_digest(registry, capability_id, version, _release_digest(release))
        reports.append(_run_verification(root, release, spec, "activation", True))
    return reports


def publish(root: Path, candidate: Path) -> Path:
    """验证候选自证材料并发布为不可覆盖的版本目录。"""
    init_root(root)
    spec = load_spec(candidate)
    report = _run_verification(root, candidate, spec, "publish", False)
    _require(report["result"] == "pass", f"候选测试失败，证据: {report['run_id']}")
    release = _release_path(root, spec["id"], spec["version"])
    _require(not release.exists(), f"版本已存在，禁止覆盖: {spec['id']}@{spec['version']}")
    release.parent.mkdir(parents=True, exist_ok=True)
    try:
        release.mkdir()
        # MVP 只接收单文件适配器，避免隐式复制无关文件。
        shutil.copy2(candidate / "capability.json", release / "capability.json")
        shutil.copy2(candidate / "adapter.py", release / "adapter.py")
    except Exception:
        if release.exists():
            shutil.rmtree(release)
        raise
    append_event(
        root,
        "capability_published",
        capability_id=spec["id"],
        capability_version=spec["version"],
        friction_id=spec["friction_id"],
        source=spec["source"],
        release_sha256=report["release_sha256"],
        verification_run_id=report["run_id"],
    )
    return release


def activate(root: Path, capability_id: str, version: str) -> dict:
    """原子激活候选版本，受信任门禁失败时恢复完整旧注册表。"""
    registry = load_registry(root)
    release = _release_path(root, capability_id, version)
    spec = load_spec(release)
    _require(spec["id"] == capability_id and spec["version"] == version, "发布路径与契约不一致")
    _load_trusted_gate(root, capability_id, spec)
    previous = copy.deepcopy(registry)
    candidate = copy.deepcopy(registry)
    candidate["active"][capability_id] = version
    candidate["revision"] += 1
    # ponytail: 单写者 MVP，并发激活需要引入文件锁。
    _atomic_write_json(root / "registry.json", candidate)
    append_event(
        root, "capability_activation_started", capability_id=capability_id, capability_version=version
    )
    reports = []
    try:
        reports = _check_active_releases(root, candidate["active"])
        failed = [report for report in reports if report["result"] != "pass"]
        if failed:
            raise CapgraftError("受信任门禁失败: " + ", ".join(report["run_id"] for report in failed))
    except CapgraftError as exc:
        current = load_registry(root)
        restored = {
            "schema_version": SCHEMA_VERSION,
            "revision": current["revision"] + 1,
            "active": previous["active"],
            "known_good": previous["known_good"],
        }
        _atomic_write_json(root / "registry.json", restored)
        previous_good = previous["known_good"].get(capability_id)
        append_event(
            root,
            "capability_rolled_back",
            capability_id=capability_id,
            from_version=version,
            to_version=previous["active"].get(capability_id),
            to_verification_run_id=previous_good["verification_run_id"] if previous_good else None,
            failed_run_ids=[report["run_id"] for report in reports if report["result"] != "pass"],
            trigger="activation_healthcheck",
            reason=str(exc),
        )
        raise CapgraftError(f"激活失败，已自动回退: {exc}") from exc
    confirmed = load_registry(root)
    candidate_report = next(report for report in reports if report["capability_id"] == capability_id)
    confirmed["known_good"][capability_id] = {
        "version": version,
        "verification_run_id": candidate_report["run_id"],
        "release_sha256": candidate_report["release_sha256"],
    }
    confirmed["revision"] += 1
    _atomic_write_json(root / "registry.json", confirmed)
    append_event(
        root,
        "capability_activated",
        capability_id=capability_id,
        capability_version=version,
        friction_id=spec["friction_id"],
        release_sha256=candidate_report["release_sha256"],
        verification_run_ids=[report["run_id"] for report in reports],
    )
    return confirmed


def verify(root: Path, capability_id: str, version: str) -> dict:
    """复现发布版本的候选测试与受信任门禁，生成新证据。"""
    release = _release_path(root, capability_id, version)
    spec = load_spec(release)
    _require(spec["id"] == capability_id and spec["version"] == version, "发布路径与契约不一致")
    _check_known_good_digest(load_registry(root), capability_id, version, _release_digest(release))
    report = _run_verification(root, release, spec, "verify", True)
    _require(report["result"] == "pass", f"验证失败，证据: {report['run_id']}")
    return report


def invoke(root: Path, capability_id: str, payload: dict) -> dict:
    """按注册表选择不可变版本，校验输入输出并记录调用结果。"""
    registry = load_registry(root)
    _require(capability_id in registry["active"], f"能力未激活: {capability_id}")
    version = registry["active"][capability_id]
    release = _release_path(root, capability_id, version)
    spec = load_spec(release)
    digest = _release_digest(release)
    _check_known_good_digest(registry, capability_id, version, digest)
    _validate_payload(payload, spec["input"], "input")
    try:
        envelope = _call_worker(release, payload)
        if not envelope["ok"]:
            raise CapgraftError(f"{envelope.get('error_type')}: {envelope.get('message')}")
        value = envelope.get("value")
        _validate_payload(value, spec["output"], "output")
    except CapgraftError as exc:
        append_event(
            root,
            "capability_invoked",
            capability_id=capability_id,
            capability_version=version,
            release_sha256=digest,
            status="error",
            message=str(exc),
        )
        raise
    append_event(
        root,
        "capability_invoked",
        capability_id=capability_id,
        capability_version=version,
        release_sha256=digest,
        status="ok",
    )
    return value


def rollback(root: Path, capability_id: str) -> dict:
    """手动切回该能力带证据引用的最近 known_good。"""
    registry = load_registry(root)
    _require(capability_id in registry["active"], f"能力未激活: {capability_id}")
    _require(capability_id in registry["known_good"], f"能力没有 known_good: {capability_id}")
    current = registry["active"][capability_id]
    target = registry["known_good"][capability_id]
    _require(current != target["version"], f"能力已经位于 known_good: {target['version']}")
    target_release = _release_path(root, capability_id, target["version"])
    _check_known_good_digest(registry, capability_id, target["version"], _release_digest(target_release))
    registry["active"][capability_id] = target["version"]
    registry["revision"] += 1
    _atomic_write_json(root / "registry.json", registry)
    append_event(
        root,
        "capability_rolled_back",
        capability_id=capability_id,
        from_version=current,
        to_version=target["version"],
        to_verification_run_id=target["verification_run_id"],
        trigger="manual",
    )
    return registry


def _print_json(value: object) -> None:
    """以统一 UTF-8 友好格式输出 CLI 结果。"""
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def build_parser() -> argparse.ArgumentParser:
    """声明最小 CLI，不加入尚未实现的自动逆向功能。"""
    parser = argparse.ArgumentParser(description="CapGraft 能力发布、质量证据、调用与回滚 MVP")
    parser.add_argument("--root", type=Path, default=Path(".capgraft"), help="状态目录，默认 .capgraft")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化状态目录")
    trust_parser = commands.add_parser("trust", help="安装受信任门禁并记录审批者")
    trust_parser.add_argument("capability_id")
    trust_parser.add_argument("gate_file", type=Path)
    trust_parser.add_argument("--approver", required=True)
    publish_parser = commands.add_parser("publish", help="验证候选自证并发布版本")
    publish_parser.add_argument("candidate", type=Path, help="含 capability.json 与 adapter.py 的目录")
    activate_parser = commands.add_parser("activate", help="按受信任门禁激活，失败自动回退")
    activate_parser.add_argument("capability_id")
    activate_parser.add_argument("version")
    verify_parser = commands.add_parser("verify", help="复现验证并生成新证据")
    verify_parser.add_argument("capability_id")
    verify_parser.add_argument("version")
    invoke_parser = commands.add_parser("invoke", help="调用现役能力")
    invoke_parser.add_argument("capability_id")
    invoke_parser.add_argument("--input", dest="input_json", required=True, help="JSON 输入对象")
    rollback_parser = commands.add_parser("rollback", help="回退到最近 known_good")
    rollback_parser.add_argument("capability_id")
    commands.add_parser("status", help="显示当前注册表")
    worker_parser = commands.add_parser("_worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("release", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """分发 CLI 命令，并把预期错误转换为非零退出码。"""
    args = build_parser().parse_args(argv)
    if args.command == "_worker":
        _worker_entry(args.release)
        return 0
    try:
        if args.command == "init":
            init_root(args.root)
            result = {"root": str(args.root)}
        elif args.command == "trust":
            path = install_trusted_gate(args.root, args.capability_id, args.gate_file, args.approver)
            result = {"trusted_gate": str(path), "approved_by": args.approver}
        elif args.command == "publish":
            release = publish(args.root, args.candidate)
            result = {"release": str(release)}
        elif args.command == "activate":
            result = activate(args.root, args.capability_id, args.version)
        elif args.command == "verify":
            result = verify(args.root, args.capability_id, args.version)
        elif args.command == "invoke":
            try:
                payload = json.loads(args.input_json)
            except json.JSONDecodeError as exc:
                raise CapgraftError(f"--input 不是合法 JSON: {exc}") from exc
            result = invoke(args.root, args.capability_id, payload)
        elif args.command == "rollback":
            result = rollback(args.root, args.capability_id)
        else:
            result = load_registry(args.root)
        _print_json(result)
        return 0
    except (CapgraftError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
