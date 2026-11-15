#!/usr/bin/env python3
"""把项目源码按流程节点注入 capgraft-flow.html，生成可离线浏览的代码视图。

用法：python build_code_view.py
产物仍是双击即开的单文件 HTML；源码改动后重跑本脚本即可刷新快照。
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
HTML_PATH = PROJECT_DIR / "capgraft-flow.html"
# 占位块：构建时只替换两个标记之间的 JSON，其余 HTML 保持原样。
PLACEHOLDER = re.compile(r'(<script id="code-data" type="application/json">)[\s\S]*?(</script>)')
# 只识别顶层 def/class，用于切出函数级行号区间（1 起、含端点）。
SYMBOL_PATTERN = re.compile(r"^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
# 流程页节点 id 的提取规则，与 HTML 中 nodes 数组的写法保持一致。
NODE_ID_PATTERN = re.compile(r'\{id:"([a-z]+)"')

# 流程节点 → 源码片段映射；symbol 为 None 表示展示整文件。
# 同一函数可挂在多个节点下（如 _run_verification 同时属于 publish 与 runs）。
NODE_SOURCES = {
    "friction": [
        ("examples/word_count/capability.json", None),
        ("capgraft.py", "validate_spec"),
    ],
    "candidate": [
        ("examples/word_count/adapter.py", None),
        ("capgraft.py", "load_spec"),
    ],
    "publish": [
        ("capgraft.py", "publish"),
        ("capgraft.py", "_run_verification"),
    ],
    "trust": [
        ("examples/word_count/trusted.json", None),
        ("capgraft.py", "install_trusted_gate"),
        ("capgraft.py", "_load_trusted_gate"),
    ],
    "activate": [
        ("capgraft.py", "activate"),
        ("capgraft.py", "_check_active_releases"),
    ],
    "good": [
        ("capgraft.py", "_check_known_good_digest"),
        ("capgraft.py", "_release_digest"),
        ("capgraft.py", "load_registry"),
    ],
    "invoke": [
        ("capgraft.py", "invoke"),
        ("capgraft.py", "_call_worker"),
        ("capgraft.py", "_worker_entry"),
    ],
    "verify": [("capgraft.py", "verify")],
    "rollback": [("capgraft.py", "rollback")],
    "runs": [
        ("capgraft.py", "_run_verification"),
        ("capgraft.py", "_execute_case"),
    ],
    "journal": [("capgraft.py", "append_event")],
    "registry": [
        ("capgraft.py", "_atomic_write_json"),
        ("capgraft.py", "init_root"),
        ("capgraft.py", "load_registry"),
    ],
}

# 项目总览展示的全部文件。
# ponytail: 不含 capgraft-flow.html 自身——注入内容会随每次重建递归膨胀。
OVERVIEW_FILES = [
    "capgraft.py",
    "test_capgraft.py",
    "examples/word_count/capability.json",
    "examples/word_count/adapter.py",
    "examples/word_count/trusted.json",
    "build_code_view.py",
]


def _require(condition: bool, message: str) -> None:
    """在信任边界拒绝不符合约定的输入，直接向用户报告。"""
    if not condition:
        raise SystemExit(f"error: {message}")


def parse_symbols(lines: list[str]) -> dict[str, list[int]]:
    """切出顶层 def/class 的行号区间，供页面按函数展示与跳转。"""
    starts = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := SYMBOL_PATTERN.match(line))
    ]
    symbols: dict[str, list[int]] = {}
    for position, (index, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        symbols[name] = [index + 1, end]
    return symbols


def load_file(relative: str) -> dict:
    """读取一个项目文件；Python 文件附带函数行号表，其余只有全文。"""
    path = PROJECT_DIR / relative
    _require(path.is_file(), f"缺少源文件: {relative}")
    lines = path.read_text(encoding="utf-8").splitlines()
    return {
        "lines": lines,
        "symbols": parse_symbols(lines) if path.suffix == ".py" else {},
    }


def build_payload(files: dict[str, dict]) -> dict:
    """校验节点映射后生成注入用 JSON：文件全文 + 节点片段 + 总览清单。"""
    for node_id, slices in NODE_SOURCES.items():
        for relative, symbol in slices:
            _require(relative in files, f"{node_id} 引用了未加载文件: {relative}")
            if symbol is not None:
                _require(
                    symbol in files[relative]["symbols"],
                    f"{node_id} 引用的函数不存在: {relative}:{symbol}",
                )
    for relative in OVERVIEW_FILES:
        _require(relative in files, f"总览引用了未加载文件: {relative}")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
        "nodes": {
            node_id: [
                {"file": relative, "symbol": symbol} for relative, symbol in slices
            ]
            for node_id, slices in NODE_SOURCES.items()
        },
        "overview": OVERVIEW_FILES,
    }


def inject(html: str, payload: dict) -> str:
    """把 JSON 写入占位块；转义 </ 防止提前闭合 script 标签。"""
    _require(len(PLACEHOLDER.findall(html)) == 1, "HTML 必须恰好包含一个 code-data 占位块")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("</", "<\\/")
    return PLACEHOLDER.sub(lambda m: m.group(1) + encoded + m.group(2), html, count=1)


def main() -> int:
    """读取源码、校验映射、注入 HTML，并打印一行摘要。"""
    html = HTML_PATH.read_text(encoding="utf-8")
    page_nodes = set(NODE_ID_PATTERN.findall(html))
    _require(page_nodes == set(NODE_SOURCES), "节点映射与流程页节点不一致")
    relatives = sorted(
        {relative for slices in NODE_SOURCES.values() for relative, _ in slices}
        | set(OVERVIEW_FILES)
    )
    files = {relative: load_file(relative) for relative in relatives}
    payload = build_payload(files)
    updated = inject(html, payload)
    HTML_PATH.write_text(updated, encoding="utf-8")
    total_lines = sum(len(entry["lines"]) for entry in files.values())
    print(
        f"已注入 {len(files)} 个文件 / {total_lines} 行 → {HTML_PATH.name}"
        f"（{len(updated.encode('utf-8')) // 1024} KB）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())