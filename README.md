# Minecraft 身体 · AstrBot 插件

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.27%2C%3C5-4B8BBE.svg)
![Windows](https://img.shields.io/badge/Windows-AstrBot_host-0078D4.svg)
![Minecraft 1.21.1](https://img.shields.io/badge/Minecraft-1.21.1-62B47A.svg)

通过 AstrBot、RCON、KubeJS 和 Baritone，让智能体以玩家身份参与 Minecraft，支持游戏聊天、移动、感知、物品交互、合成和程序任务。

需要额外运行一个角色客户端，并将其加入服务器。仅安装本插件和 Minecraft 服务端无法使用。

当前版本：0.40.0。实验性项目，需要手动配置游戏环境与两端桥脚本。

## 部署文档

完整部署步骤不在本 README 展开，先读这两份：

| 文档 | 用途 |
|---|---|
| [部署教程](docs/用户使用指南.md) | 亲自从零部署：服务端、AI 客户端、RCON、两端桥、AstrBot 配置 |
| [AI 部署操作单](docs/AI操作指南.md) | 交给自己的 AI 助手执行部署、排错和验收 |

尽可能整理了步骤 理论上应该是可以自己或者丢给agent尝试的

## 写在前面 
- 这套东西的最终效果是可以让ai假装自己是真实的玩家而不是另外一个酒狐 最大的特征就是交互完全基于原版对话框 但是因为部署需要额外的服务端和专门的服务器所以说目前门槛上会有很大的问题 部署步骤已经整理到 docs，本机部署本机ai单人游玩还没有做干净环境验收 目前达成的效果就是在有指令和交流下可以自然行动 但是主动性问题尚未测试 目前我自己测试的话效果上还可以 主动太烧钱了我尚且没测 接下来的计划是新增本地客户端模式 也就是传统上的ai玩用户电脑上的我的世界 相对会简单好部署很多

## 功能

- 游戏聊天接入 AstrBot 会话，可配置是否需要唤醒词，回复发送到游戏公屏。
- 查询身体状态、背包、菜单、附近实体、地形、设施及事件。
- 寻路、跟随、控制视线、装备与使用物品、交互方块和操作菜单。
- 读取服务端配方，规划背包及工作台合成链，通过配方书摆料并核对产物。
- 执行冶炼、沿途照明和农事任务。
- 通过程序反射处理受伤、进食、逃跑、溺水及卡住状态。
- 协调保命、用户指令、战斗、任务和闲逛，提供地点簿、空闲扫视及可选截图识图。

## 环境要求

| 组件 | 要求 |
|---|---|
| AstrBot | 4.27 及以上、低于 5.0，已配置支持工具调用的模型 |
| Minecraft | 1.21.1，NeoForge 环境 |
| 服务端 | 兼容的 KubeJS，已启用且安全隔离的 RCON |
| 角色客户端 | 独立运行，安装兼容的 KubeJS 与 Baritone，能够正常入服 |
| 桥脚本 | 客户端、服务端与 Python 插件使用配套版本 |
| 截图识图 | 可选；当前实现需要 SSH、Docker、虚拟显示、截图环境及视觉模型 |

AstrBot 可运行于 Windows 主机；仓库附带的客户端容器参考使用 Linux 与 Xvfb。客户端和服务端可以部署在同一台机器或不同机器上。

## 安装与配置

以下是配置项摘要。完整安装顺序、环境准备和验收标准见[部署教程](docs/用户使用指南.md)。

### 1. 准备角色客户端

先确认角色客户端能够正常加入服务器，并与服务端的模组和协议兼容。

服务桥默认控制玩家 `Nanako`。使用其他名字时，同步调整服务桥目标、客户端登录身份和插件配置 `character_name`。

仓库附带的启动器和容器文件仅作为部署参考，不包含完整游戏实例或通用正版账号登录流程。

### 2. 部署桥脚本

| 仓库文件 | 目标位置 |
|---|---|
| `bridge/server/mcbridge_server.js` | 服务端实例的 `kubejs/server_scripts/` |
| `bridge/client/mcbridge.js` | 角色客户端实例的 `kubejs/client_scripts/` |

服务端执行 `/reload` 加载脚本；客户端需要重启。升级时同步更新两端桥与 Python 插件。

启动器和容器参考见[桥接部署说明](bridge/DEPLOY.md)。

### 3. 配置 AstrBot

将仓库放入 AstrBot 的 `data/plugins/astrbot_plugin_mc_body/`，在插件面板填写配置并重载。

| 配置项 | 用途 |
|---|---|
| `rcon_host`、`rcon_port`、`rcon_password` | Minecraft 服务端 RCON 连接信息 |
| `allowed_sender_ids` | 允许调用游戏工具的发送者 ID；留空全部拒绝 |
| `character_name` | 角色客户端的实际玩家名 |
| `owner_player_name` | 主要交互玩家的游戏名 |
| `enable_chat_uplink` | 启用游戏聊天接入 |
| `white_session` | 游戏聊天使用的 AstrBot 会话 UMO；留空不启动上行 |
| `require_wake_keyword` | 是否要求游戏聊天包含唤醒词；默认开启 |
| `wake_keywords` | 唤醒词列表；要求唤醒词时，空列表表示不唤醒 |

会话 UMO 格式为 `平台实例名:消息类型:会话ID`，可从 AstrBot 会话管理中获取。使用人格工具白名单时，将需要的 `mc_*` 工具加入可用工具列表。

跨机器访问 RCON 建议使用 SSH 隧道；插件不会自动建立或维护隧道。

### 4. 检查连接

在 AstrBot 中执行 `/mcbody`，再调用 `mc_state` 与 `mc_inventory`。确认角色在线、客户端状态新鲜后，尝试移动和交互。

## 使用

可通过 AstrBot 对话调用工具；游戏内聊天默认需要包含配置的唤醒词。关闭 `require_wake_keyword` 后，除角色自身外的所有玩家聊天均可触发回应，不限制玩家身份或距离，会增加模型调用。修改配置后重载插件生效。

| 请求示例 | 操作 |
|---|---|
| 查看坐标和血量 | 查询身体状态 |
| 跟着我 | 跟随玩家 |
| 附近有什么箱子 | 查询周边设施 |
| 用现有材料做一把木镐 | 规划并执行合成 |
| 向北照明，尝试两处，间隔五格，不挖地 | 启动照明任务 |
| 停止任务 | 取消程序任务 |

### 模型工具

| 类别 | 工具 |
|---|---|
| 感知与记录 | `mc_state`、`mc_inventory`、`mc_menu`、`mc_threats`、`mc_around`、`mc_where`、`mc_lookat`、`mc_events`、`mc_journal`、`mc_chat_log`、`mc_place`、`mc_screenshot` |
| 移动 | `mc_goto`、`mc_follow`、`mc_stop`、`mc_baritone_raw` |
| 交互 | `mc_say`、`mc_hold`、`mc_equip`、`mc_use`、`mc_use_on`、`mc_click`、`mc_attack`、`mc_aim` |
| 生产与任务 | `mc_craft`、`mc_smelt`、`mc_task`、`mc_task_stop` |
| 战斗控制 | `mc_stance`、`mc_retreat` |
| 技能说明 | `mc_skill` |

### 使用限制

- 合成支持背包与工作台配方，不自动采集缺少的材料；模组配方尚未全面验证。
- 合成失败会锁定当前事件的后续游戏动作；状态、背包查询及停止工具仍可用，新事件不继承该锁。
- 照明的 `no_break` 仅限制本次任务路线，不是全局破坏保护。`rounds` 表示位置尝试次数，不是物品消耗上限。
- `mc_click` 的 `mode=4` 用于丢弃自己的库存物品，`button=0` 丢一个、`button=1` 丢整叠。使用当前菜单槽号，不支持直接丢弃容器内容；下发不等于已掉落或交付，需另行核对库存与地面物品。
- `mc_click` 需要配套客户端桥提供菜单实例及采样标识；缺失时拒绝执行。丢物执行前重新核对菜单、库存槽归属、物品注册名、数量和空光标。点击后的槽位变化不等于操作完成，未确认时不要自动重发。
- 移动、点击和放置尚未统一提供完成回执，需要核对游戏状态。保命反射不能保证角色不会受伤或死亡。
- 聊天队列状态保存在 `data/uplink_state.json`，更新代码时应保留。异常掉线或高流量下仍可能遗漏消息，失败项不会自动重跑。
- 尚未完成干净环境部署及长期无人看护验收。

## 安全与隐私

RCON 使用明文协议，不要暴露到公网或不可信网络。不要向模型开放任意 RCON 或系统 Shell 权限。

配置和日志可能包含凭证及聊天内容。可选截图识图会将游戏图像发送给配置的视觉提供商。

## 常见问题

| 问题 | 检查项 |
|---|---|
| 工具全部失败 | RCON 地址、密码、隧道及两端桥版本 |
| 角色离线或状态陈旧 | 客户端进程、入服状态及客户端脚本 |
| 模型没有游戏工具 | 人格工具白名单、插件开关及发送者授权 |
| 游戏聊天没有回应 | 会话 UMO、唤醒词及聊天上行开关 |
| 找不到工作台 | 工作台位置、扫描范围及当前菜单 |
| 更新没有生效 | 实际部署目录，以及对应的重载或客户端重启 |

## 致谢

感谢 [Numen（言出法随）](https://github.com/Dwinovo/minecraft-numen) 及其贡献者的设计与源码灵感架构设计参考。具体来源及复用性质见[第三方来源说明](THIRD_PARTY_NOTICES.md)。

感谢 AstrBot、KubeJS 和 Baritone 制作的框架与工具，以及 mineflayer、mcpfabric 等项目的灵感设计参考。

## 许可

`mcb/permission.py`、`mcb/events.py`、`mcb/ripeness.py` 和 `bridge/client/mcbridge.js` 按 GPL-3.0-only 分发，其余原创部分维持现有 [GNU AGPLv3](LICENSE)。Numen 上游为 LGPL-3.0-only；本副本依 GPLv3 第7节移除相关内容的 LGPL 附加许可，并依 GPLv3 第13节与 AGPLv3 第13节组合。来源分类、许可全文及范围见[第三方来源说明](THIRD_PARTY_NOTICES.md)。其他第三方内容适用各自许可。
