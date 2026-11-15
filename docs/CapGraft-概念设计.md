# CapGraft 概念设计（MVP）

| 项目 | 内容 |
|---|---|
| 日期 | 2026-11-15 |
| 状态 | 已实现（schema 2，纯 Python 标准库） |
| 一句话 | 摩擦驱动、契约化的能力吸收：把外部项目逆向成候选能力，经可信门禁与证据验证后嫁接进系统，失败自动回滚。 |

---

## 1. 起源与定位

原始想法：结合 **REA 式逆向**（把外部项目拆解成可理解的结构）与 **schlarevolve 式吸收**（把有用的部分长进自己的能力边界），做一个可以不断扩展自身功能边界的开源项目。

CapGraft 是这个想法的 MVP 落点，只做「吸收闭环」本身：

- **摩擦驱动**：每次能力增长必须回溯到一个具体摩擦事件（重复 / 搬运 / 等待 / 返工 / 纠正），没有摩擦来源的提案不予吸收。
- **契约化**：能力以公开契约（输入 / 输出 / 测试）进入系统，外部类型留在适配器内部，核心概念不自动膨胀。
- **可信门禁**：候选自证与放行判据分离，AI 生成的材料不能自己决定自己通过。
- **证据不可变**：每次验证生成追加式证据对象，回滚不截断历史。

非定位：不是通用 agent 平台，不是测试执行引擎，不做 AI 自动修 bug。

## 2. 核心闭环

```
friction → candidate → publish → activate → known_good → invoke
              │           │         │ ↑         │           │
              │         trust ──────┘ │         │         verify
              │      （人审门禁）   fail → rollback ────────┘
              └────────────── 全部过程写入 runs / journal / registry
```

| 阶段 | 动作 | 关键约束 |
|---|---|---|
| friction | 记录真实缺口 `friction_id` | 提案必须引用摩擦来源 |
| candidate | 产出 capability.json + adapter.py + 自证测试 | 测试只是自证材料 |
| publish | 候选测试通过 → 不可变 release + SHA256 | 同版本禁止覆盖 |
| trust | 控制面安装受信任门禁，`--approver` 留痕 | 候选代码不可修改门禁 |
| activate | 原子切换注册表 → 重跑全部现役双层门禁 | 任一失败恢复完整旧注册表 |
| known_good | 记录 version + run_id + sha256 | 偏离摘要即拒绝执行 |
| invoke | 独立 Worker 子进程执行，契约校验输入输出 | 故障隔离，不是安全沙箱 |
| verify | 任何已发布版本一键复现 | 只追加新 run，不覆盖 |
| rollback | 自动（激活失败）/ 手动切回 known_good | 不删版本、不截断日志 |

## 3. 概念模型

MVP 的全部概念（概念守恒：不随吸收自动生长）：

- **能力契约 `capability.json`**（schema 2）：`id / version / source{name,revision,license} / friction_id / friction / input / output / tests`。
  - 测试必须覆盖三类：`normal / error / counterexample`；每条标 `origin: human | agent`。
- **适配器 `adapter.py`**：单文件入口 `run(payload: dict) -> dict`。外部项目的类型、依赖、副作用都关在适配器里，核心只认契约。
- **受信任门禁 `trusted/<id>.json`**：由控制面显式安装（`trust` 命令 + 审批者），与候选自证测试分层运行。
- **验证运行 `runs/run_<uuid>.json`**：证据对象，含 run_id、摩擦 ID、release SHA256、Python/平台环境、逐用例结果与耗时、attempt 恒为 1（不静默重试）、复现命令。
- **注册表 `registry.json`**：`active`（当前版本指针）+ `known_good`（`{version, verification_run_id, release_sha256}` 证据引用）。同目录临时文件 + 原子替换写入。
- **审计账本 `journal.jsonl`**：append-only，每条事件带 UTC 时间与 schema 版本，历史从不更新或删除。

## 4. 三大设计支柱与来源

三份外部 PRD 作为设计输入（原文归档于 `docs/references/`），各取一部分：

| 来源 | 吸收的机制 | 在 CapGraft 的落点 |
|---|---|---|
| PRD-coevo | 概念守恒；摩擦门禁；外部类型隔离在适配器内 | `validate_spec` 严格字段集；`friction_id` 必填；单文件 adapter |
| PRD-agent-hot-standby | 不可变发布；原子切换；known_good；追加式审计；Worker 隔离 | `publish` 拒覆盖；`_atomic_write_json`；`activate` 失败恢复旧注册表；`journal.jsonl`；`_call_worker` |
| PRD-QTrail | 判断基于证据对象而非状态字符串；候选自证与可信门禁分离；不静默重试；可复现；human/AI 来源标记 | `runs/` 证据；`trust --approver`；attempt=1；复现命令；`origin` 字段 |

未吸收的部分（规模或阶段不匹配）：coevo 的完整概念演化代数、热备的多机切换与流量调度、QTrail 的追溯索引 / TIA / flaky 生命周期 / MCP 工具面。

## 5. 关键决策

1. **候选自证不能决定放行。** Agent 可以同时改代码和改期望值，自证测试因此不构成放行判据。受信任门禁由控制面安装、人审留痕。端到端测试证明：候选篡改自身期望后 publish 仍可通过，但 activate 必然失败并回滚。
2. **known_good 是证据引用，不是状态字符串。** 发布字节与 known_good 摘要不一致时，invoke / verify / rollback 全部拒绝，防止「证据指向的代码」与「实际执行的代码」脱节。
3. **回滚恢复完整旧注册表。** 不删除候选版本、不截断审计日志，回滚事件引用失败 run 与恢复目标 run，任何决策可追溯。
4. **Worker 是故障隔离边界，不是安全沙箱。** 子进程 + 超时只防崩溃与挂死，不防恶意代码；只允许可信候选代码进入。
5. **单写者 MVP。** 并发激活需要文件锁，当前刻意不做（`# ponytail:` 标注上限）。

## 6. MVP 范围与非目标

已实现：

- CLI：`init / trust / publish / activate / verify / invoke / rollback / status`（+ 隐藏 `_worker`）。
- schema 2 契约校验、双层门禁、证据链、两类自动回退。
- 5 个端到端测试（真实 Worker 子进程，无 mock）。
- 流程可视化 `capgraft-flow.html` + 源码注入 `build_code_view.py`：点节点看对应源码，点空白看项目总览。

明确不做（推迟，需另行立项）：

- 自动逆向 / LLM 分析层（当前候选由人或 agent 手工产出）
- 真正的安全沙箱
- 副作用 intent / result / idem_key 对账
- flaky 自动检测与隔离
- 可配置门禁规则引擎
- MCP 工具封装
- 流程图的 drill-down 编辑 / 节点创建

## 7. CLI 与状态目录

```bash
python capgraft.py init
python capgraft.py trust text.word_count gate.json --approver alice
python capgraft.py publish examples/word_count
python capgraft.py activate text.word_count v1
python capgraft.py invoke text.word_count --input '{"text":"hello world"}'
python capgraft.py verify text.word_count v1
python capgraft.py rollback text.word_count
python capgraft.py status
```

```
.capgraft/
├── releases/<id>/<version>/   # 不可变发布：capability.json + adapter.py
├── trusted/<id>.json          # 控制面安装的受信任门禁
├── runs/run_<uuid>.json       # 追加式验证证据
├── registry.json              # active + known_good（原子写入）
└── journal.jsonl              # append-only 审计账本
```

## 8. 验证

`test_capgraft.py` 五个端到端用例：

| 用例 | 证明 |
|---|---|
| known_good_links_to_complete_evidence | 正常闭环 + 篡改发布字节后 invoke/verify 被摘要校验拒绝 |
| verify_appends_new_evidence_without_overwrite | 复现只追加新 run，历史不改写 |
| candidate_self_proof_cannot_bypass_trusted_gate | 候选自改期望仍激活失败，回滚引用证据 |
| failed_first_activation_leaves_capability_inactive | 无旧版可退时不残留现役能力 |
| activation_requires_trusted_gate | 未安装受信任门禁不得激活 |

## 9. 可视化

`capgraft-flow.html`：12 节点流程图（主链 + 人审 / 失败分支 + 证据存储道），滚轮缩放、拖拽重排；点节点看说明与对应源码（行号 / 函数跳转 / 整文件），点空白看项目总览。代码快照由 `python build_code_view.py` 注入，构建时校验每个节点映射的函数与行号真实存在。