# 第三方来源与许可

本项目使用 AstrBot、KubeJS 和 Baritone，并参考以下项目的设计与源码。许可范围见下文，根 [LICENSE](LICENSE) 保持为 AGPLv3。

## 设计与接口参考

| 项目 | 参考内容 | 本地涉及部分 |
|---|---|---|
| [Numen（言出法随）](https://github.com/Dwinovo/minecraft-numen) | 协议净化、能力名册、任务调度、错误反馈及历史存储边界 | protocol、reflexes、arbiter、render、tasks、uplink |
| [mcpfabric](https://github.com/denfry/mcpfabric) | 事件缓冲、增量游标、地点指纹 | events、places、journal、服务端桥 |
| mineflayer、mineflayer-crafting-util | 配方依赖规划与容器交互 | craft、containers、客户端桥 |
| 原版 Minecraft、AltoClef | 行为通道与任务分层 | arbiter、客户端桥 |
| 车万女仆（TLM）、Kindred | 进食、战斗与威胁判断 | reflex、tasks、服务端桥 |
| MC_baritone_AI_Custer_Auto | 配方书 API 使用 | 客户端桥 |
| astrbot_plugin_dafeiyu_pet | 状态注入方式 | main.py |

## 具体实现与文字对应

以下部分根据 Numen 源码改写或具有具体实现、文字对应，不仅是设计参考。上游作者信息为 dwinovo（项目元数据），贡献者见[上游历史](https://github.com/Dwinovo/minecraft-numen/commits/)。

| 本地部分 | 上游源码路径 | 对应内容及本地修改 |
|---|---|---|
| mcb/permission.py | numen-api/common/src/main/java/com/dwinovo/numen/api/permission/{Rule,Gate,RuleSet}.java | 规则解析、裁决顺序、部分默认规则及 RuleSet 说明文字；改写为 Python，缩减动作与标签支持，调整默认拿取规则 |
| mcb/events.py：Joins、KindSpec | agent/src/main/java/com/dwinovo/numen/api/agent/inbox/EventTypes.java | 事件投递声明结构；改为 Python 声明表，部分字段尚未接线 |
| mcb/ripeness.py | agent/src/main/java/com/dwinovo/numen/api/agent/inbox/EventQueue.java | 熟度阈值、等待公式与触发顺序；改为 Python，增加在线及冷却门控 |
| bridge/client/mcbridge.js：mcbPickHit、mcbFacePoint、mcbSlotRole | numen-api/common/src/main/java/com/dwinovo/numen/api/entity/Look.java；numen/common/src/main/java/com/dwinovo/numen/tools/GuiOps.java | 命中候选点与槽位分类；适配 KubeJS/Rhino，并调整面排序 |

本地 Git 首次记录上述改写的日期（北京时间）：事件声明为 2026-10-10；权限规则、命中候选、槽位分类及熟度策略为 2026-10-11。权限规则在 2026-10-11 继续调整，熟度策略同日调整事件判断与优先级；2026-10-15 至 2026-10-16 整理来源说明，2026-10-16 补充本通知、文件级修改日期及下述 GPL-3.0-only 分发声明。这些日期不代表上游创作时间或实际取用时刻。

## 许可文本

| 来源 | 许可 | 全文 |
|---|---|---|
| Numen | LGPL-3.0-only | [LGPLv3](licenses/Numen-LGPL-3.0.txt)及其引用的[GPLv3](licenses/GPL-3.0.txt) |
| mcpfabric | MIT，Copyright (c) 2026 dabinayo | [MIT](licenses/mcpfabric-MIT.txt) |

Numen 对照版本：[`2df6979ec33ae45edd623c512ee6644dd94ef11a`](https://github.com/Dwinovo/minecraft-numen/tree/2df6979ec33ae45edd623c512ee6644dd94ef11a)，仅用于源码定位，不是已确认的采用版本。其他参考项目的具体版本、许可及表达复用范围仍待核实。

## 本副本的许可范围

以下四个文件整体按 GPL-3.0-only 分发，包括作者有权授权并已按此许可提供的原创贡献：

- `mcb/permission.py`
- `mcb/events.py`
- `mcb/ripeness.py`
- `bridge/client/mcbridge.js`

Numen 上游许可为 LGPL-3.0-only，包含 GPLv3 条款及 LGPLv3 附加许可。本分发副本依 GPLv3 第7节移除上述文件中 Numen 来源内容的 LGPL 附加许可；上游许可本身不变，原有来源通知及许可全文保留。

其余原创部分维持现有 AGPLv3 许可。GPLv3 第13节与 AGPLv3 第13节允许这两类作品组合；GPL 部分仍适用 GPLv3，AGPLv3 第13节的网络交互要求适用于组合体。根 LICENSE 不替代上述文件的 GPL-3.0-only 声明，其他第三方内容仍适用各自许可。
