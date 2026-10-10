# 桥接与部署

这个目录装的是跑在 Minecraft 那一侧的脚本 —— 插件本身（main.py + mcb/）不含它们。
要把整套东西跑起来，这四份文件都得落到正确的位置。

> 注意： 本目录是这些文件的原件。改完必须按下面的步骤部署过去才生效 ——
> 和插件一样，"放在这儿"不等于"在跑"。

---

## 目录

| 文件 | 是什么 | 去哪 |
|---|---|---|
| server/mcbridge_server.js | 服务端桥。注册 /mcb 命令，把 RCON 来的指令转成 sendData 发给客户端 | Minecraft 服务端的 kubejs/server_scripts/ |
| client/mcbridge.js | 客户端桥。收 sendData，驱动 Baritone / 行为槽 / 截图 / 点格子 | 角色客户端的 kubejs/client_scripts/（在 versions/<版本名>/ 下） |
| launcher/client.py | 客户端启动器。管 libraries 校验、准备、拉起游戏 | 客户端机器上的客户端根目录 |
| launcher/Dockerfile | 跑客户端的容器镜像（含 Xvfb 虚拟屏） | 客户端机器的镜像构建 |

---

## 拓扑

```
AstrBot（本机）
   │  插件 mcb/*.py                     ← 和本仓库同一个 git 仓库
   │
   │  RCON（TCP 明文，别暴露到公网）
   ▼
Minecraft 服务端  ── kubejs/server_scripts/mcbridge_server.js
   │
   │  player.sendData('mcbridge', {...})   ← 原版网络通道，不需要额外 mod
   ▼
角色客户端        ── kubejs/client_scripts/mcbridge.js
   ├── Baritone（走）
   ├── 行为槽（朝向 / 使用物品）
   └── 截图 / 点格子 / 用物品
```

两端脚本之间的通道是原版的 sendData / dataReceived，
所以不需要装 QueQiao 之类的桥接 mod，服务端脚本改完 /reload 就能热生效。

---

## 部署

下面用占位符：<SERVER_DIR> = 服务端数据目录，<CLIENT_DIR> = 客户端实例目录
（NeoForge 客户端是 <客户端根>/versions/<版本名>/），<SSH_TARGET> = 客户端所在机器。

### 1. 服务端脚本

```bash
scp server/mcbridge_server.js <SSH_TARGET>:<SERVER_DIR>/kubejs/server_scripts/
```

生效：RCON 发一次 /reload。

```bash
# 例：rcon-cli，或任何 RCON 客户端
rcon-cli reload
```

> 注意： /reload 要给 60~90 秒超时。实测它经常比默认超时慢，会让客户端误判成失败 —— 其实已经成功了。

### 2. 客户端脚本

```bash
# 改完先本地查语法，别赌
node --check client/mcbridge.js

scp client/mcbridge.js <SSH_TARGET>:<CLIENT_DIR>/kubejs/client_scripts/
```

生效：注意： 必须重启客户端。KubeJS 的 /kubejs reload client 在服务端不认，
实测返回 Unknown or incomplete command。

```bash
# 1) 掐掉游戏进程
pkill -TERM -f net.neoforged ; sleep 4
# 2) 掐掉启动器
pkill -TERM -f "client.py launch"
# 3) ⚠️ 必须等够 —— 启动器有文件锁（flock），等太短会起不来
sleep 20
# 4) 重新拉起（日志追加，不覆盖）
cd <CLIENT_DIR> && python3 -u client.py launch >> launch.log 2>&1
```

上线大约 80 秒。

> 注意： 只等 6 秒就拉起会报 BlockingIOError: Resource temporarily unavailable
> —— 那是启动器的 flock 还握着。等 20 秒稳妥。

### 3. 启动器与镜像

```bash
scp launcher/client.py <SSH_TARGET>:<CLIENT_DIR>/client.py
scp launcher/Dockerfile <SSH_TARGET>:<镜像构建目录>/
```

这两个只在重建环境时要用；日常改功能不碰它们。
改 client.py 后同样要按上面第 2 步重启客户端。

>  client.py 里有两处值得看一眼的常量：目标服务器地址（环境变量 MC_TARGET）
> 和 JVM 堆上限（-Xmx）。都按自己的环境改。

---

## 排错

| 症状 | 先看什么 |
|---|---|
| 插件所有工具都报"桥不可用" | 服务端脚本在不在、/reload 有没有跑、RCON 通不通 |
| 服务端有反应，她不动 | 客户端脚本 —— 看客户端日志 launch.log |
| 客户端日志里没有动作记录 | 脚本没上传对位置，或者客户端没重启 |
| 重启后连不上 | 上次没退干净。ps 看一眼有没有残留的 java 进程 |
| 服务端脚本改完没生效 | /reload 其实超时失败了（默认超时太短），重发一次并给足时间 |

一句话判据：服务端日志看她"收没收到"，客户端日志看她"做没做" ——
两边都写了详细的执行记录，别靠猜。

---

## 安全

- RCON 是明文协议，只能走 SSH 隧道或内网，不要暴露到公网或局域网
- 服务端/客户端脚本里不含任何凭证
- 插件侧的 RCON 密码只从 AstrBot 的配置读，不写进源码
