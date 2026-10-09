# astrbot_plugin_mc_body

给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的智能体接上一具 Minecraft 里的身体。

智能体通过 RCON 桥接查询角色状态、说话、移动。桥接跑在 Minecraft 服务端一侧（KubeJS），
本插件只负责把 RCON 命令包成 `@filter.llm_tool` 工具，让智能体在**自己的 agent 循环里**
调用 —— 不另起一条裸 LLM 通路，人格与记忆保持连续。

## 当前版本（v0.3.0）

### 工具（智能体主动调）

| 工具 | 作用 |
|---|---|
| `mc_state` | 查询角色是否在线、坐标、血量、维度、正在做什么。读服务端即时数据，不依赖游戏客户端 |
| `mc_say` | 用角色的身体在游戏里说话 |
| `mc_goto` | 走到指定坐标（Baritone 寻路）|
| `mc_follow` | 跟随某个玩家 |
| `mc_stop` | 急停：取消一切寻路与跟随 |
| `mc_baritone_raw` | 应急口，透传一条 Baritone 命令 |

> ⚠️ **动作类工具只能报告「已下发」，不能报告「完成了」。**
> 服务端只知道动作发给了玩家的客户端；客户端执行到哪一步，要靠 `mc_state` 复核坐标。

### 游戏内聊天上行（她"听得见"）

开启后会轮询服务端的聊天，把游戏里的事接进智能体的**主会话**（人格/记忆/工具都在），
不是另开一条裸 LLM 通路。规则：

- **被点名才唤醒** —— 消息里含唤醒词才算。其余**攒着当环境上下文**，下次唤醒时一并附上
- **按来源路由** —— 从游戏来的回复用 `mc_say` 打回**游戏公屏**，不回 QQ（确定性代码，不靠模型判断）
- **她自己的发言永不触发唤醒**（防自激），只作 `你刚才在游戏里说过` 前缀

> 需要填 `white_session`（形如 `平台实例名:FriendMessage:会话ID`），留空则上行不启动。

### 调试指令

| 指令 | 作用 |
|---|---|
| `/mcbody` | 直接打一次 RCON 自检，报告隧道与桥的状态。排查用 |

## 依赖

- AstrBot `>=4.27,<5`
- `aio-mc-rcon`（见 `requirements.txt`）
- Minecraft 服务端一侧需部署配套的 KubeJS 桥接脚本，提供 `/mcb` 命令。见下方"服务端侧"

## 安装与配置

1. 把本目录放进 AstrBot 的 `data/plugins/`
2. 在 WebUI 插件配置里填：

| 配置项 | 说明 |
|---|---|
| `rcon_host` / `rcon_port` | RCON 地址与端口；走 SSH 隧道时填 `127.0.0.1` / `25575` |
| `rcon_password` | 与服务端 `server.properties` 的 `rcon.password` 一致 |
| `rcon_timeout_seconds` | 单条命令超时，默认 10 秒 |
| `allowed_sender_ids` | **允许调用 MC 工具的发送者 ID 白名单** |

> ⚠️ **`allowed_sender_ids` 默认是空的，此时所有调用都会被拒绝。** 这是刻意设计：
> 这组工具能让游戏里的角色真实移动，不该因为"忘了配"就默认放行。

## 安全说明

- **凭证只从配置读**，不写进源码
- **授权默认拒绝**，在工具边界逐次校验发送者 ID
- 与 RCON 的连接**不暴露到公网或局域网**，建议一律走 SSH 隧道
- 工具参数在边界做校验（玩家名、坐标、文本都会过滤），不提供任意 RCON 或 shell 权限

## 服务端侧

本插件依赖 Minecraft 服务端上的 KubeJS 桥接脚本，命令契约（RCON 里发的是**不带斜杠**的裸命令）：

| 命令 | 返回 |
|---|---|
| `mcb ping` | `PONG` |
| `mcb state` | `MCB_STATE x=… y=… z=… hp=… dim=… name=…` |
| （桥不可用时） | `MCB_FAIL: <原因>` |

脚本与完整契约见你的整合包项目文档（本仓库不含服务端脚本）。

## 开发

```
main.py        插件入口、工具定义、授权与输出整形
mc_rcon.py     RCON 封装（不依赖 AstrBot，可单独测）
```

## License

见 `LICENSE`。
