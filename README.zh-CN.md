# Codex Adaptive Agents 0.8.0

**让主 Agent 按当前子模型能力选择 Luna 角色，负责规划与验收。**

[English](README.md) · 简体中文 · [验证记录](docs/VALIDATION.md) · [操作指南](docs/PUBLIC_GUIDE.md)

Codex Adaptive Agents 是一个为 Codex 安装协作规则、原生 Agent 角色和每日选档脚本的开源项目。安装后，继续在 Codex 中提出需求：主 Agent 判断是否值得委派，需要时使用 0–5 个 Luna 子 Agent，最后检查结果并回复。它没有独立网页或聊天窗口。

安装器、选档策略和验收逻辑均由本仓库实现，无需安装其他 Agent 编排项目。具体模块与外部数据输入见[实现说明](docs/UPSTREAM.md)。

运行只需要 **Python 3.11+ 标准库和兼容的官方 Codex CLI**。不需要 Go、自编译二进制或 pip 依赖，也不会安装常驻服务、MCP 服务或 Hook。

## 最简单的用法：复制给 Codex

在能访问本机文件、运行命令的 Codex 中操作。新安装按两步完成：**让 Codex 安装 → 开一个新任务验证**；已有安装需要升级时再使用第三段提示词。也可以继续阅读下方的手动命令。

### 第一步：复制这段，让 Codex 安装

安装会将子模型设为 `gpt-6-luna`，默认思考强度为 `xhigh`，并安装五个角色和协作规则；主窗口的模型、思考强度和其他配置保留。

```text
请从 Codex Adaptive Agents 的项目仓库为我安装：
https://github.com/aweiht/codex-adaptive-agents
保留已有修改。阅读项目 README.zh-CN.md，使用当前 Codex 已在使用的 CODEX_HOME，通过源码目录中的准确 codex-adaptive-agents.py 入口运行；macOS/Linux 用 Python 3.11+ 的 `python3 codex-adaptive-agents.py`，Windows 用 `py -3 .\codex-adaptive-agents.py`。
安装 Codex Adaptive Agents 的受管运行时、五个 adaptive_luna_* 角色和规则，同时保留其他设置、角色、备份与文件。
先运行 `install --dry-run` 并检查计划，再严格用返回的 `<plan_id>` 执行 `install --yes --plan-id <plan_id>`。随后运行 `doctor`；只有能力快照或选档缓存过期时才运行 `refresh`，再检查一次。
请实际完成安装并报告源码路径、CODEX_HOME、计划结果和 doctor 状态。只有安装完成且 doctor 为 READY 或 READY_FALLBACK 才算成功。此步不要运行 verify --live 或任何模型任务；依赖、登录、权限或冲突需我处理时给出具体原因。
```

### 第二步：新建 Codex 任务，复制这段验证

这一步分别检查本地安装状态和子角色选档，不额外启动模型验收任务。`READY` 或 `READY_FALLBACK` 表示本地安装就绪；选档成功表示当前子模型能力和策略允许选择一个角色。

```text
请验证本机 Codex Adaptive Agents 是否安装成功：
https://github.com/aweiht/codex-adaptive-agents
使用当前 Codex 的 CODEX_HOME，找到 `CODEX_HOME/codex-adaptive-agents/runtime/codex-adaptive-agents.py`。用 Python 3.11+（macOS/Linux 用 `python3`，Windows 用 `py -3`）通过这个已安装入口运行 `python3 "$CODEX_HOME/codex-adaptive-agents/runtime/codex-adaptive-agents.py" --codex-home "$CODEX_HOME" doctor`（Windows 使用对应的 `py -3` 命令），再在独立临时目录运行 `select --project <临时目录> --explain`。不需要主模型参数或人工确认。
只有 doctor 报告子模型能力快照或选档缓存过期时才运行 `refresh`，然后再次检查 doctor；如果本机 Luna 能力和策略数据有效，核对选档返回已安装的 `adaptive_luna_*` 角色。
不要重新安装、修改业务文件、运行 verify --live、调用模型，也不要把选档当成已经调用子 Agent。根据 doctor 单独报告安装就绪状态，并展示实际状态和失败原因。
```

### 第三步：复制这段，让 Codex 升级已有安装

适用于运行时从旧源码目录或 ZIP 复制安装的情况。受管运行时路径保持不变；只更新源码目录不会更新已经复制到 CODEX_HOME 的运行时。

```text
请从 Codex Adaptive Agents 的项目仓库升级我已有的安装：
https://github.com/aweiht/codex-adaptive-agents
使用当前 Codex 的 CODEX_HOME 和已有安装。从项目默认分支获取最新源码并读取 VERSION。若当前源码目录有未提交修改，保留它们，必要时使用新的独立源码目录完成升级。将 origin 指向 `https://github.com/aweiht/codex-adaptive-agents.git`，或直接使用新目录。若源码来自 ZIP，请从该项目仓库重新获取并解压，不要依赖旧下载目录。

用 Python 3.11+ 通过源码目录的准确 `codex-adaptive-agents.py` 入口并指定现有 CODEX_HOME（例如 `python3 codex-adaptive-agents.py --codex-home "$CODEX_HOME" ...`）。先取得并审阅 `install --dry-run` 计划，再严格用返回的 `<plan_id>` 执行 `install --yes --plan-id <plan_id>`。受管冲突需要我处理时停止并报告。建立正常备份，保留其他设置、角色、规则和文件；不要先卸载，也不要另建第二套安装目录，用这个计划更新现有安装。

升级后，用 `codex-adaptive-agents.py --version` 检查源码，再用 `CODEX_HOME/codex-adaptive-agents/runtime/codex-adaptive-agents.py --version` 检查已安装入口，并运行已安装入口的 `doctor`。只有能力快照或选档缓存过期时才运行 `refresh`。源码和已安装版本应为预期版本，且 doctor 为 READY 或 READY_FALLBACK 才算成功。不要运行 verify --live 或任何模型调用；报告源码路径、运行时路径、备份、更新结果、doctor 状态和冲突。
```

想确认 Luna 确实能执行任务，再使用下方[真实原生委派验证](#可选验证真实原生委派)；它会使用真实 Codex 额度。

![从安装到日常使用，再到验证的三个步骤](docs/images/workflow-zh.svg)

*使用流程示意。子 Agent 数量取决于任务，不会每次都启动五个。*

## 安装前准备

- Python **3.11 或更新版本**。
- 可运行的官方 Codex CLI **0.147.0 或更新版本**，以及支持原生自定义 Agent 的 Codex 客户端。CLI 的公开模型目录需要包含子模型 `gpt-6-luna` 的 `low / medium / high / xhigh / max` 五档。
- 委派不按主模型身份筛选；安装和选档只检查 Luna 子模型能力与策略。
- 日常使用和真实验收需要正常的 Codex 登录、所需模型访问权限及可用额度。安装检查模型目录，不代表账号一定能完成模型任务。
- 网络能读取公开模型目录和选档数据源；用 Git 下载时还需 Git。也可以通过 GitHub 的 **Code → Download ZIP** 下载源码。

先检查终端命令：

```sh
python3 --version
codex --version
```

Windows PowerShell 用 `py -3 --version` 检查 Python，确认版本不低于 3.11。如果 CLI 尚未就绪，先按 [Codex 官方文档](https://developers.openai.com/codex/cli/) 完成安装和登录。

## 1. 下载并安装

macOS / Linux：

```sh
git clone https://github.com/aweiht/codex-adaptive-agents.git
cd codex-adaptive-agents
python3 codex-adaptive-agents.py install --dry-run
python3 codex-adaptive-agents.py install --yes
python3 codex-adaptive-agents.py doctor
```

Windows PowerShell：

```powershell
git clone https://github.com/aweiht/codex-adaptive-agents.git
cd codex-adaptive-agents
py -3 .\codex-adaptive-agents.py install --dry-run
py -3 .\codex-adaptive-agents.py install --yes
py -3 .\codex-adaptive-agents.py doctor
```

下载 ZIP 的用户，在解压目录里运行后三条命令即可。Windows 的 `py -3` 应指向 Python 3.11+；没有 Python launcher 时，可以换成对应的 `python` 命令。

`--dry-run` 仅用于 `install` 和 `uninstall`，展示改动计划；其他命令会在执行前拒绝该参数，包括 `verify --live` 和 `refresh`。`--yes` 应用当前计划并备份。默认安装目标是 `CODEX_HOME`，未设置时为用户目录下的 `.codex`。安装到已有 Codex 使用的同一个目录，避免把空白目录当成已登录环境。

**安装会修改哪些内容？** 它设置 `gpt-6-luna` 子模型、默认 `xhigh` 和最多五个并发子 Agent，安装五个 Luna 角色、协作规则与 Python 运行脚本。其他配置、角色和规则保留；受管文件发生冲突时会拒绝覆盖。修改前先建立备份。

使用不同 Codex home 或 CLI 路径时，把下面的参数加到命令中：

```sh
python3 codex-adaptive-agents.py --codex-home /path/to/codex-home --codex /path/to/codex install --dry-run
```

## 2. 在 Codex 中正常使用

安装完成后，**重新打开 Codex 并开始一个新任务**，让客户端加载新增规则和角色。打开要开发的项目，像平时一样描述需求，例如：

> 检查这个项目的输入校验和测试覆盖。如果有值得拆开的独立工作，请委派给 Luna；修复后由主 Agent 复核，并告诉我验证结果。

主 Agent 负责需求、关键决策、公共接口、协调和最终验收。实质工作开工前只做足够确定边界的少量探索，使用者不需要手动启动 worker，也不必先运行选档命令。

委派前脚本按**系统本地自然日**选择一个支持的 Luna 档位；项目缓存保存在 `.codex-adaptive-agents/state`。请将 `.codex-adaptive-agents/` 加入业务项目的 `.gitignore`。网站查不到 GPT-6 Luna 评分、请求失败或评分无法可靠比较时，默认回退到 `xhigh`；仍须通过本机模型能力检查，不保证降档、省钱或提速。

### 主模型与实时验收参数

`select` 不读取、不要求也不按主模型限制委派。主模型缺失、unknown、当前型号或未来型号都走相同的 Luna 子能力与策略检查；`--root-model` 在 `select` 中仅为兼容参数，会被忽略。安装、选档和验收均不改写用户的主模型或思考强度。`model_policy.json` 保留在运行时文件布局中以兼容已有安装回执，其内容是 unrestricted 元数据，没有主模型名单，也不用于识别当前身份。

主模型思考强度没有插件白名单或强制 `max`。`verify --live` 默认遵从官方客户端、不覆盖主模型或 effort，也不按主模型门禁；显式提供的 `--root-model` / `--root-effort` 只是请求覆盖，必须与公开 effective 值精确匹配。effective 字段缺失或 null 时保留为不可观测，不能伪造身份元数据。该 CLI 不自动读取另一个桌面任务的窗口身份。

### 委派门槛与例子

只要安全工作包具备明确目标、边界、产物或问题归属和可查证验收，用户未禁止且宿主允许，就必须先实际派发给 Luna，再开始该工作包。这个路由规则是试用策略，不承诺 token 更低或完成更快，不设总成本、耗时或预期节省门槛，不设 2:8 等固定比例，也不增加 usage 采集系统。不能把整批实质工作拆成多个“一次小改”来规避评估。

完整功能或 UI 诊断由同一叶子完成实现、相关辅助验证脚本、构建/测试/打包检查和普通失败修复；进入验证阶段不改变执行 owner，普通失败返给原叶子。只有公共接口变更、重大方案问题、合理修复后仍未解决，或能力/权限限制才升级主 Agent。主 Agent 定义验收标准和风险边界，在阶段交付时集中核查真实差异、关键证据及边界，并作最终验收判断；这不要求主 Agent 亲自执行每条验证命令或脚本。需要独立执行复核时，可另派无写冲突的 Luna 受限复核包，判断仍由主 Agent 负责，不要求每个任务增加复核者。主 Agent 在既有授权边界内独立核查高风险权限、真实数据与进程清理，并确认所需授权已经具备；不得推断或扩大授权，也不以叶子自述或退出码代替核查。简单问答、确实一次完成的小改、纯主控决策、没有安全独立边界的紧耦合依赖链，以及用户明确要求不委派时，可以直接完成。

长测试、轮询、采样和常规失败处理都属于同一个 Luna 工作包，由脚本或宿主原生等待持续监测。阶段交付集中审查交接摘要、真实差异和定点产物，不因最终验收重复同一份完整探索或测试；主 Agent 不因进入验证或最终验收而接管执行。仅在证据矛盾、关键覆盖缺失或风险信号出现时扩大检查，必要的独立执行复核另设无写冲突的受限工作包。

独立工作包可以并行，有依赖时按顺序执行；共享 GUI、数据库、端口或构建目录只设一个 owner，需要时串行。单个任务最多五个直接 Luna 子 Agent，不为满足比例或制造重复任务而开 Agent。用户指定的数量或并行方式无法安全满足时，说明具体限制并请求调整，不能擅自改要求。

| 情况 | 做法 |
| --- | --- |
| 简单问答、一次小改或纯主控决策 | 直接完成 |
| 目标、边界、归属和验收明确的完整功能或 UI 诊断 | 同一叶子完成实现、相关验证脚本、构建/测试/打包检查和普通修复 |
| 有独立资源的多个工作包 | 委派；安全时并行 |
| 有依赖的工作包或共享 GUI、端口、构建目录 | 委派；按顺序执行并保持单一 owner |
| 跨模块集成或高风险权限、真实数据、进程行为 | 叶子完成工作包；主 Agent 负责验收判断，并在既有授权边界内独立核查；独立执行复核按需另派受限工作包 |
| 公共接口变更、重大方案，或有界恢复后仍阻塞/需要新增授权 | 升级主 Agent；继续独立工作 |
| 用户明确说不要委派 | 直接完成 |

工作范围扩大、原方案失效、反复排错，或进入新的实施/验证阶段时，重新评估尚未完成的工作。每次实际发生选档、角色或原生派发失败，都在现有任务状态入口加一条精简记录：脱敏后的实际错误原文、类别、受影响工作包、时间、工具给出的 `retry-after`/配额重置点或可查证恢复条件、累计尝试次数、恢复事件是否已消费、当前 owner 和交接点。旧失败不能证明新批次当前失败；同一未解决故障的次数则跨回合、跨工作包连续累计。区分配额、并发、参数/角色、权限、临时网络/服务和宿主能力；类别不明标 `unknown`，不能统称 Luna 配额。宿主明确不支持原生能力属于能力限制，不按临时故障退避；只有宿主支持状态可查证地变化后才重试。若返回含糊且可能已启动，重试前先查是否已有该活动 owner。选档成功不等于派发成功或恢复成功，这些指令也不能强制宿主调度。

先分类再处理。并发已满时，复用已停止写入且角色/模型边界合适的 Luna，或用原生等待等现有工作完成；不能为腾名额中断他人。新建子 Agent 失败不代表已有叶子不能继续；没有关闭操作就不宣称名额已释放。临时限流、网络或服务错误遵守明确的 `retry-after`；没有提示时依次等待 30 秒、2 分钟、5 分钟。不论是否有提示，同一故障额外最多重试 3 次；提示只改变等待时长，不增加次数。用原生等待，避免频繁探测。预算用尽后不再自行开启退避；只有新的、尚未消费的明确恢复事件或可查证状态变化才允许真实重试一次，并将该事件标记为已消费。若失败，过去的同一重置时间或状态变化不能在新回合再次触发；保留累计记录并等待新的恢复事件。

明确配额耗尽且有重置时间时，等到该时间后复查一次、不提前探测，并将该重置事件标记为已消费；没有重置时间时不猜时间、不盲目重试，只在有新的可查证状态或用户处理后恢复尝试。同一过去的重置时间或已消费状态变化不能再次触发。到了重置时间不代表已经恢复。认证、权限、参数或角色错误先定位并真实修正原因，再有界重试；未经授权不改账户或权限设置。恢复未明时，主 Agent 可在当前授权内临时完成一个事先写清范围和收口点的小工作包，到点即结束。不能把每个后续包都叫“临时接管”而无限交给主 Agent：若后续仍依赖 Luna 且没有新的恢复证据，保留待交接项并写明恢复条件；要扩大主 Agent 执行须由用户决定。其他不依赖该故障的工作可继续。取得本轮原生派发已恢复的真实证据后，在安全边界交回剩余工作；主 Agent 停止写同一范围，记录已有证据、剩余工作和新 owner。普通测试失败仍由原工作包 owner 修复。

回复末尾的执行摘要形如：

```text
主 Agent/Luna: direct · luna ×0
主 Agent/Luna: delegated · luna_max ×2 · parallel
```

这里只展示任务实际发生的委派：子 Agent 的档位来自派发角色或调用参数，数量按实际运行的直接子 Agent 统计。主控只显示模型名，不读取或展示当前窗口的思考强度；示例不是本次运行结果。
每个工作包写清完整目标、边界、归属、验收条件和必要的局部上下文；默认采用有界、无历史交接，不附无关历史。执行 owner 启动时说明一次、完成时交付一次，异常、阈值或待决事项及时报告。需要中途向用户简述时依据已知状态，不为状态更新重读无变化证据；原生等待超时只表示该等待区间结束，也不消除宿主的独立回合。叶子返回五点精简结论：完成、文件位置、验证证据、未决问题、待决事项。详细日志放在约定本地路径按需读取，不能丢失败和风险。主 Agent 等待时使用同一原生任务等待，只处理不同且不重叠的工作。计划或进度中用一句话说明委派或直接完成的原因，沿用这个紧凑尾注，不另建委派报告文件。

### 临时进程与长命令登记

普通短命令不需要建立进程账本。若工作包需要临时服务或长命令，主 Agent 先建立独立批次，再把 `run_id`、项目、入口、owner、用途和资源或端口边界传给叶子。长测试使用 `--summary --timeout <秒数>`；完成摘要提供状态、退出结果、登记批次/条目标识与 PID、捕获字节数和私有日志位置，不回传 stdout/stderr 正文。不加 `--summary` 时仍保留原有的全文完成 JSON。stdout 和 stderr 合计捕获上限为 2 MiB；超过上限会失败，日志可能不完整，`output_complete=false` 只能按部分证据处理。由命令或原生等待持续监测；无变化时不重复轮询 `process-check`，但异常、身份疑点或监督失联时可定点检查。执行 owner 报告一次 `entry_id` 和 PID，完成时交付验证结果；主 Agent 负责最终检查、清理和清理后的二次检查。叶子要主动停止自己的命令或执行会话，并把明确保留的工作标为 `RETAINED`。清理后批次拒绝新增命令，保留项仍可在同一 run 中再次检查或清理。可复制的 POSIX/PowerShell 命令、输出时序和限制见[公共进程操作说明](docs/PUBLIC_GUIDE.md#temporary-process-and-long-command-runs)。

## 3. 快速确认是否正常

### 检查安装和选档

```sh
python3 codex-adaptive-agents.py doctor
python3 codex-adaptive-agents.py select --project .
```

Windows 把 `python3` 换成 `py -3`。`doctor` 检查本地安装，不调用模型、不联网。`select` 只选择角色，必要时刷新公开数据；输出 `adaptive_luna_xhigh` 等角色名，**不会启动 Luna**。

![主模型无关的角色选档输出示例](docs/images/cli-check.svg)

*这是主模型无关的本地选择器实际输出；使用临时安装和本地合成 fallback 缓存，不调用模型。*

| 输出 | 含义与下一步 |
| --- | --- |
| `READY` | 本地安装、能力快照和选档状态通过，可以开始新 Codex 任务。 |
| `READY_FALLBACK` | 本地检查通过，评分不可用，当前使用本机支持的 `xhigh` 回退档位；可继续使用。 |
| `NOT_READY` | 检查未通过，查看 `errors`；缓存过期时运行 `refresh`，再运行 `doctor`。 |
| `INSTALLED_NEEDS_REFRESH` | 文件已安装，但选档未准备好；运行 `refresh` 后重新检查。 |

### 可选：验证真实原生委派

需要真实任务证据时，可以再复制这段给 Codex：

```text
请为本机 Codex Adaptive Agents 运行一次真实原生委派验收：
https://github.com/aweiht/codex-adaptive-agents
我同意本次 verify --live 使用真实 Codex 额度，最多两个直接 Luna 子 Agent，600 秒上限，不自动重试。
使用当前 CODEX_HOME 下 `codex-adaptive-agents/runtime/codex-adaptive-agents.py` 的已安装入口，并用 Python 3.11+ 运行；先检查 doctor，通过后运行 verify --live。默认不要覆盖主模型或 effort；只有我明确提供 `--root-model` / `--root-effort` 时才传入，并精确核对公开有效值。
使用命令默认创建的独立临时验收目录，保留结果，不使用已有业务目录。
检查命令结果和产物；只有返回 LIVE_VERIFIED 才报告真实委派通过，否则说明失败原因。
最后给出本地检查结果、真实委派结果及验收文件位置，不用模型自述或选档结果代替证据。
```

也可以在终端手动运行：

```sh
python3 codex-adaptive-agents.py verify --live
```

默认 live 命令不覆盖 root model 或 effort，也不按 effective 主模型门禁。只有显式传入的 `--root-model` / `--root-effort` 才会覆盖，并要求请求值与有效值完全一致。它不会自动读取桌面任务当前窗口身份。

进程清理存在平台边界：macOS 极短命中间进程连续 fork 后可能隐藏后代关系，不能把辅助脚本当成安全沙箱。详见[清理边界](docs/PUBLIC_GUIDE.md#process-cleanup-boundary)。

该命令使用真实模型，最多启动两个直接 Luna 子 Agent，整个检查有 600 秒上限。它检查真实输出、角色、文件范围和验收契约，完整通过才返回 `LIVE_VERIFIED`。失败时按输出排查，不能把 `doctor` 通过当成这一步通过。

验证时会临时打开官方 stdio app-server 通道，结束后关闭；不会安装常驻服务。可通过 `--project PATH --output PATH` 指定新的独立验收目录和结果目录，避免使用已有业务目录。

## 常见问题

| 问题 | 处理方式 |
| --- | --- |
| 找不到 `python3` / `py` | 安装 Python 3.11+，或使用已安装 Python 的完整路径。 |
| 找不到 `codex` | 确认同一个终端能运行 `codex --version`；必要时通过 `--codex PATH` 指定路径。 |
| 安装报模型目录不兼容，或空白 home 报 `input or file operation failed` | 检查使用的 Codex home、CLI 版本和模型目录；在同一环境运行 `codex debug models`。`gpt-6-luna` 五档缺失时，安装不会继续。 |
| `doctor` 提示快照或缓存不可用 | 先查看诊断，只运行一次 `python3 codex-adaptive-agents.py refresh`，再运行 `doctor`。仍失败时保留实际错误并停止重复刷新。`refresh` 读取公开数据，不调用模型。 |
| 某个项目同一天选档失败后仍被阻塞 | 查看诊断，只运行一次 `python3 codex-adaptive-agents.py refresh --project /path/to/project`，再对同一项目运行 `select --explain`；不带 `--project` 的 `refresh` 只更新 home 级策略。此命令更新选档输入，不重试原生派发，也不能证明配额已恢复。 |
| 修改过受管角色后升级失败 | 保留改动，检查冲突并决定如何合并；不要直接覆盖文件或安装凭据。 |
| 安装后没有使用 Luna | 先开始新 Codex 任务；简单任务直接完成是正常情况。需要独立验收时使用显式的 `verify --live`。 |
| Windows / Linux 的真实模型任务是否已验证？ | 本轮没有请求新的 live 模型验收；历史 CI 只对其具体提交有效，不代表当前 0.8.0。详见[验证记录](docs/VALIDATION.md)。 |

## 更新、卸载与恢复

已有安装请使用上面的升级提示词，按计划原地更新。Git 源码目录必须先指向项目仓库：

```sh
git remote set-url origin https://github.com/aweiht/codex-adaptive-agents.git
git pull --ff-only
python3 codex-adaptive-agents.py --version
python3 codex-adaptive-agents.py install --dry-run
python3 codex-adaptive-agents.py install --yes --plan-id <plan_id>
python3 "$CODEX_HOME/codex-adaptive-agents/runtime/codex-adaptive-agents.py" --codex-home "$CODEX_HOME" --version
python3 "$CODEX_HOME/codex-adaptive-agents/runtime/codex-adaptive-agents.py" --codex-home "$CODEX_HOME" doctor
```

`git pull` 只更新源码目录，不会更新已经复制到 CODEX_HOME 的运行时。升级沿用现有 CODEX_HOME 和运行时路径，创建备份并保留其他设置和角色；受管冲突需要人工处理。无需卸载或重新安装，也没有自动或 self-update 命令。ZIP 用户应从项目仓库重新获取并解压新源码，不要依赖旧下载目录。新模型支持随维护者发布版本提供，不保证任意未来模型自动可用。

卸载前同样可以查看计划：

```sh
python3 codex-adaptive-agents.py uninstall --dry-run
python3 codex-adaptive-agents.py uninstall --yes
```

原下载目录可以移动；安装后的独立入口位于 `<CODEX_HOME>/codex-adaptive-agents/runtime/codex-adaptive-agents.py`。仍需保留安装时使用的 Python 路径可用。卸载移除受管安装内容并恢复受管配置；若检测到后续冲突则拒绝覆盖。中断事务恢复及按备份回滚见[操作指南](docs/PUBLIC_GUIDE.md#recovery-and-uninstall)。

本次安装新建的文件和空配置表，只在没有后续用户内容时移除。`recover --yes` 仅恢复状态为 `applying` 的中断事务，不会撤销已完成安装。要撤销已完成事务，须明确执行 `rollback --backup PATH --yes`，其中 `PATH` 是该事务记录的备份路径。`recover` 不接受 `--backup`，`rollback` 必须指定它。

## 开发与验证范围

无需调用模型即可运行源码测试并构建源码包：

```sh
python3 -m unittest discover -s tests_python -p 'test_*.py' -v
python3 scripts/release_python.py --output-dir dist/0.8.0-python
```

打包目录须是新的，脚本不会覆盖已有发行包。源码包包含中英文文档、运行图片、测试和校验和。本轮 0.8.0 的证据受具体源码修订和环境限制；没有请求新的 live 模型验收，历史 CI 也不代表当前源码。详见[验证记录](docs/VALIDATION.md)与[贡献指南](CONTRIBUTING.md)。

## 许可与实现

采用 [Apache-2.0](LICENSE) 许可。实现与运行依赖见[实现说明](docs/UPSTREAM.md)，随包声明见 [NOTICE](NOTICE)。这是独立社区项目，并非 OpenAI 官方产品。安全问题请按 [SECURITY](SECURITY.md) 反馈。
