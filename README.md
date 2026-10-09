# astrbot_plugin_mc_body

给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的智能体接上一具 Minecraft 里的身体。

智能体通过 RCON 桥接查询角色状态、说话、移动。桥接跑在 Minecraft 服务端一侧（KubeJS），
本插件只负责把 RCON 命令包成 `@filter.llm_tool` 工具，让智能体在**自己的 agent 循环里**
调用 —— 不另起一条裸 LLM 通路，人格与记忆保持连续。

## 当前版本（v0.1.0）

**只提供 `mc_state` 一个工具**，纯只读，用来先把管线验通：

| 工具 | 作用 |
|---|---|
| `mc_state` | 查询角色是否在线、坐标、血量、所在维度。读服务端即时数据，不依赖游戏客户端 |

动作类工具（说话 / 移动 / 停止）在管线验证通过后加入。

另有一个不带 LLM 的调试指令：

| 指令 | 作用 |
|---|---|
| `/mcbody` | 直接打一次 RCON，报告隧道通不通。排查用 |

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
