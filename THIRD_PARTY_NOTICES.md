# 第三方来源与许可

本项目使用 AstrBot、KubeJS 和 Baritone，并参考以下项目的设计与源码。仓库许可证见 [LICENSE](LICENSE)。

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

以下部分与 Numen 有具体对应，单独记录；实现复用边界及实际采用版本尚待确认。

| 本地部分 | 上游来源 | 对应内容 |
|---|---|---|
| permission.py | Rule、Gate、RuleSet 等权限类型 | 规则解析、裁决顺序、部分默认规则及 RuleSet 说明文字 |
| events.py：Joins、KindSpec | EventTypes | 事件投递声明结构 |
| ripeness.py | EventQueue | 熟度阈值、等待公式与触发顺序 |
| 客户端桥：mcbPickHit、mcbFacePoint、mcbSlotRole | Look、GuiOps | 命中候选点与槽位分类 |

## 许可文本

| 来源 | 许可 | 全文 |
|---|---|---|
| Numen | LGPL-3.0-only | [LGPLv3](licenses/Numen-LGPL-3.0.txt)及其引用的[GPLv3](licenses/GPL-3.0.txt) |
| mcpfabric | MIT，Copyright (c) 2026 dabinayo | [MIT](licenses/mcpfabric-MIT.txt) |

Numen 对照版本：`2df6979ec33ae45edd623c512ee6644dd94ef11a`，用于源码比对，不表示已确认实际采用版本。其他参考项目的具体版本、许可及表达复用范围仍待核实。

第三方内容保留其适用许可。收录许可文本不改变本项目或上游的授权范围，也不表示来源及分发义务核查已经完成。
