# AI 部署操作单

你是协助用户安装本项目的技术助手，不是游戏里控制角色的 AstrBot。按本操作单检查用户自己的环境、部署组件并逐层验收。用户手动步骤见[部署教程](用户使用指南.md)。

适用：astrbot_plugin_mc_body 0.40.0，Minecraft 1.21.1 NeoForge，AstrBot >=4.27,<5。目标是独立服务端加独立 AI 客户端；不实现新模式、不改身体架构。

## 1. 先收集部署参数

询问用户未知项，已有文件可定点读取，不扫描全盘。先填写下表再操作；路径、主机名和账号均从用户现场取得，不沿用作者机器配置。

| 参数 | 需要记录的值 |
|---|---|
| 插件文件 | 当前仓库绝对路径、版本、是否存在未提交改动 |
| AstrBot | 操作系统、实际运行目录、主机或容器、版本、插件目录、配置入口 |
| 服务端 | 操作系统、目录、原有启动方式、MC/NeoForge版本、Java路径、模组来源 |
| AI 客户端 | 操作系统、启动器、隔离实例目录、实际玩家名、认证方式 |
| 游戏连接 | 从 AI 客户端看见的服务端地址和游戏端口 |
| RCON | 从 AstrBot 运行环境可达的地址、端口及受保护传输方案 |
| 主要玩家 | 用户实际游戏名 |
| AstrBot 会话 | 平台发送者 ID、真实 UMO、使用的人格和模型 |
| 额外功能 | 是否需要远端无头运行或截图；默认先不部署 |
| 数据保护 | 现有存档、脚本、配置、插件数据的备份位置 |

不要要求用户在对话中粘贴令牌或密码。优先让用户在面板填写秘密；必须由工具读取时，不回显到报告、命令日志或公开文件。

## 2. 操作规则

1. 先读本仓库 README、metadata.yaml、_conf_schema.json 和 bridge 两端脚本的相关配置。版本与本文不一致时核对接口，不机械覆盖新版。
2. 复用用户已有服务端、启动器和进程管理，不重新安装一套抢占端口。
3. 修改前备份具体文件到扫描目录之外，保留用户已有改动和原编码、换行；列出需要变更的项。
4. 停服、重启、改模组、防火墙、容器映射、认证或白名单之前取得用户同意。不要关闭正版验证、白名单或扩大监听面作为默认排错手段。
5. 不给 AI 角色 OP，不修改玩法规则，不清空世界，不写 AstrBot 历史数据库，不覆盖人格。测试聊天与模型调用先说明会留痕并产生费用。
6. Windows 使用 PowerShell，Linux 使用对应 shell；脚本中的占位参数必须先换成现场值，不向未知 PID 发终止信号。
7. 每完成一层记录通过证据；失败就停在该层排查，不把后面步骤标为已完成。状态未知时不要反复执行有副作用的动作。
8. 下载大文件和长时间安装先给用户命令及预计影响，由用户决定执行。不要擅自提交、推送或公开日志。

## 3. 分步部署

### 步骤A：确认基础游戏环境

检查：

- Java 21 实际用于服务端，不仅是 PATH 中另一个 java 的版本。
- 服务端和 AI 客户端都是 Minecraft 1.21.1 NeoForge，加载器与整合包要求匹配。
- 服务端及 AI 客户端都有兼容 KubeJS 和所需依赖。
- AI 客户端有可加载 baritone.api.BaritoneAPI 的 Baritone API 构建；参考产物为 baritone-api-neoforge-1.11.3.jar，不凭名称替代实际加载检查。
- AI 使用隔离实例及独立身份，能在尚未接插件时正常进入服务端。

缺独立服务端时，按用户选定的 NeoForge 或整合包服务端安装流程创建。要求用户自行确认 EULA，不替用户同意。不要把客户端模组全量复制到服务端。

通过证据：服务端启动到 Done，list 中能看到 AI 的实际玩家名，客户端没有因模组、认证或注册表不匹配而断开。

### 步骤B：配置 RCON 与网络

备份 server.properties，经同意在停服后设置 enable-rcon=true、rcon.port 和随机长 rcon.password，按原方式重启。

选择一种接法：

| 部署 | 执行要求 |
|---|---|
| 同机主机进程 | AstrBot 连接回环地址，防火墙拒绝外部访问 RCON |
| 远端服务端 | 服务端 RCON 对外隔离；从 AstrBot 所在环境建立 SSH 隧道 |
| Docker | 先查实际网络、服务名与端口发布；宿主回环地址与容器回环地址分开记录 |

使用系统监听工具检查真实监听。0.0.0.0 或 :: 上监听时继续核防火墙；不要说“插件连127.0.0.1所以没有外部暴露”。不要随意设置 server-ip，也不要把 RCON 端口开放到公网。

若使用 SSH 隧道，取得用户的主机名、SSH用户名和端口，保留主机密钥验证，不使用 StrictHostKeyChecking=no。隧道保持方式和停止方法需交代给用户。插件本身不管理隧道。

通过证据：从 AstrBot 实际网络环境到 RCON 的 TCP 连接可达，并记录外部访问被限制的配置依据。TCP 可达不是密码认证或桥命令通过，后续还需验收。

### 步骤C：部署桥脚本

记录本版本两份桥脚本的哈希。读取服务端桥的 MCB_TARGET，将它设置为现场 AI 实际玩家名，只改这一处必要身份配置。

复制：

| 源 | 目标 |
|---|---|
| bridge/server/mcbridge_server.js | 服务端实际实例/kubejs/server_scripts/mcbridge_server.js |
| bridge/client/mcbridge.js | AI实际实例/kubejs/client_scripts/mcbridge.js |

不要把客户端桥复制给用户日常客户端；不要只上传到仓库目录而未进入实际实例。已有脚本备份到脚本扫描目录之外。

经批准后，服务端 reload，AI 客户端重启。不要按进程名批量杀所有 Java。

在服务端控制台或已获授权的受保护 RCON 客户端，依次执行只读检查：

```text
list
mcb ping
mcb diag
mcb state
```

通过证据：

- ping 返回 MCB JSON，ok=true，data.pong=true。
- diag 显示 serverEntry=true、targetOnline=true。
- 多次 diag 的 upSeq 增长，upAgeMs 不持续变旧。
- state 显示 online=true，有客户端 task 快照，菜单等字段能够读取。
- AI 客户端日志含 BaritoneAPI 类可见，无对应类加载失败。

serverEntry=true 只证明能取得服务端对象，不单独证明动作通路。targetOnline=true 但无新上报时，停在客户端桥排查。

### 步骤D：接入 AstrBot

通过面板仓库安装或完整目录安装到实际 data/plugins/astrbot_plugin_mc_body。核对 metadata 版本和加载日志；备份不留在插件扫描目录。

优先通过 AstrBot 面板配置。需要直接修改配置文件时，先确认该部署的真实配置路径和写入语义，备份后只更改本插件相关键；不能猜测整个框架的配置格式。

填写以下最小接线：

| 配置 | 要求 |
|---|---|
| rcon_host、rcon_port | 步骤B确认的、从 AstrBot 环境可达的端点 |
| rcon_password | 用户填写服务端密码，不输出明文 |
| allowed_sender_ids | 真实平台发送者 ID 列表，不能留空 |
| character_name | 与实际玩家名、MCB_TARGET一致 |
| owner_player_name | 用户实际游戏名 |
| white_session | 从现有 AstrBot 会话管理取得的真实 UMO |
| enable_chat_uplink | true |
| require_wake_keyword | true |
| wake_keywords | 本次验收用的角色称呼 |
| enable_sight、enable_self_loop、enable_event_wake | 初次验收 false |
| enable_reflex、enable_idle_wander、enable_hunt | 只读接线阶段 false，在安全环境验证后由用户选择开启 |

如果还没有会话，让用户从目标平台发一条消息建立会话，再从会话管理读取 UMO，不自行编造平台实例名。若计划使用 WebUI 会话而非平台私聊，必须检查本版 AstrBot 在该会话中的发送者、授权和 CronMessageEvent 行为，不能假设与 QQ 完全相同。

确认所选会话有工具调用模型。人格有显式工具白名单时，在保留原工具的基础上加入需要的 mc_*，不要替换整份白名单。

保存并经批准重载。确认无密码缺失、allowed_sender_ids为空、white_session缺失告警。

### 步骤E：逐层验收

按以下顺序执行，不跳层：

| 检查 | 操作 | 通过依据 |
|---|---|---|
| 诊断 | 用户在授权会话发送 /mcbody | RCON认证成功并返回桥诊断 |
| 只读工具 | 请求 mc_state、mc_inventory，不执行动作 | 实际工具调用日志和对应状态数据 |
| 聊天上行 | 用户同意后，由另一位玩家发带唤醒词的测试消息 | 正确会话执行一轮，回复出现游戏公屏 |
| 移动 | 用户同意的测试区域短距离移动，随后停止 | 坐标实际变化及停止后的状态 |
| 可选自动行为 | 用户选择开启反射等开关并重载 | 相应配置生效，不扩大到未测试功能 |

不要调用 mcb claudecode 等作者测试入口伪装成真实玩家聊天验收。用户不允许模型测试时，只完成只读网络与桥检查，报告未验收聊天和模型工具，不声称部署全通过。

不要为了验收自动给物品、造怪、传送用户、改变游戏规则或清理地面。需要这些条件时另行说明与征求授权。

## 4. 可选无头与截图部署

不需要截图的用户直接跳到交付。需要无头运行时，先确认独立账号认证、游戏实例、Xvfb、容器挂载和启动管理均已具备，再部署截图。

仓库参考文件的边界：

- bridge/launcher/Dockerfile 安装 Java、Xvfb、截图工具等依赖，CMD 是 bash，不是自动启动完整客户端。
- bridge/launcher/client.py 导入 fcntl，绑定特定版本目录，固定离线登录参数，不是通用正版登录器，也不适用于 Windows 直接运行。
- 不在用户现有认证实例上用该脚本覆盖启动流程；优先保留可用启动器，按用户选定方案做无头化。

当前 mcb/sight.py 固定要求：

| 项目 | 值 |
|---|---|
| 虚拟显示 | DISPLAY=:99 |
| 容器截图 | /data/client/shots/mcb_look.png |
| 宿主截图 | /data/mc-brain/client/shots/mcb_look.png |
| 容器挂载 | /data/client 对应宿主 /data/mc-brain/client |
| 远端工具 | docker、ImageMagick import、可用的 shell |
| AstrBot主机工具 | ssh、scp，BatchMode免交互认证 |

不要谎称改 client_container 可以改变固定路径。用户已有结构不符时，列出所需调整并停止，取得授权后再做挂载或截图实现适配。

先人工抓图并打开检查，再配置 client_ssh_host、client_container、client_sudo_password、vision_provider_id 和 enable_sight。视觉测试会上传游戏画面并调用模型，需先获同意。缺图、黑屏、权限失败与视觉提供商失败分别报告，不混为“模型看不见”。

## 5. 排错顺序

| 失败点 | 只检查这些入口，得到证据后再扩展 |
|---|---|
| 无法入服 | 客户端与服务端 latest.log、版本、认证、白名单、注册表同步 |
| mcb不存在 | 服务端实际脚本路径、KubeJS加载日志及语法错误 |
| AI在线但没有上报 | AI实例client_scripts、客户端重启记录、BaritoneAPI、两端KubeJS与网络通道 |
| AstrBot连接被拒绝 | 监听端口、进程、隧道、容器网络、防火墙 |
| RCON认证失败 | 用户核对服务端与插件密码，不打印密码 |
| /mcbody被权限拒绝 | 实际sender_id与allowed_sender_ids |
| 有工具但游戏不回复 | 真实UMO、会话模型、上行开关、关键词、自身发言过滤 |
| 工具返回下发但游戏不变 | 客户端日志与世界状态，不重复发动作来掩盖问题 |
| 重载仍旧版本 | 运行目录文件哈希、加载日志、同名备份插件 |

错误报告记录具体步骤、时间、脱敏报错和下一项动作。不把模型回答、文件存在或语法通过当成真实功能证据。

## 6. 交付给用户

最终只交付实际完成的结果：

- 服务端与 AI 实例位置、启动和正常退出方式。
- 插件实际安装位置、目标玩家名、所绑定会话；不列密码。
- 游戏和 RCON 的访问方式，SSH 隧道如何启动、停止，哪些终端需保持运行。
- 每层验收的通过或未验情况，仍未完成的具体步骤。
- 改过的文件、备份位置和恢复方法；不要恢复世界或数据库来撤回脚本修改。
- 持久化仅写 AstrBot/data/plugin_data/astrbot_plugin_mc_body/。旧版插件data目录和外部places_file需在停用后备份，再复制到新目录；目标存在时停止，不覆盖、不合并队列。按部署教程第10节迁移，保留配置及存档；两端桥与插件成套更新。

不要把功能用法全文重复给用户，链接 README 即可。不要将作者机器实测写成当前用户环境已通过，也不要在没有完成安装时宣布“一键部署成功”。
