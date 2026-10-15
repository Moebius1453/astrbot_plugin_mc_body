# 第三方来源说明

本表区分设计与接口参考、可核对的实现对应及文字引用。比对对象为本地参考副本，不代表已经确定实际采用版本或全部分发义务。项目许可证见 [LICENSE](LICENSE)；本表不替换第三方许可证，也不表示许可核查已经完成。

## 许可证据与版本边界

- [Numen（言出法随）](https://github.com/Dwinovo/minecraft-numen)：参考副本根 LICENSE 为 GNU LGPL Version 3，gradle.properties 声明 LGPL-3.0-only。下表简称 N；路径相对于该参考仓库。当前核查副本 HEAD 为 2df6979ec33ae45edd623c512ee6644dd94ef11a，仅用于定位比对，不是已确认的采用提交。没有据此填写个人版权归属；实际采用版本、各文件通知及所需许可材料待核。
- [mcpfabric](https://github.com/denfry/mcpfabric)：参考副本根 LICENSE 为 MIT，含 Copyright (c) 2026 dabinayo。下表简称 M；该权利人文字来自许可文件，不由仓库账号推断。采用版本待核。
- 本仓库根 LICENSE 为 GNU AGPL Version 3，保持原状。设计参考或调用外部 API 不自动等于复制实现；具有实现对应的部分也不因改用 Python 或 KubeJS 就归为纯灵感。

## 设计与接口参考

这里记录可确认的设计关联或来源线索，不将通用算法、同名接口或 API 调用单独作为复制代码的证据。未取得对应源码的项目不填写推测许可证。

| 本地文件或符号 | 上游文件或符号 | 可证实性质 | 许可证据及未决 |
|---|---|---|---|
| mcb/protocol.py：for_wire、CLOSED_BEFORE_RESULT | N：ai/src/main/java/com/dwinovo/numen/api/agent/llm/ProtocolView.java：forWire | 参考历史与出站视图分离、补悬空结果、合并 user、工具文本化。上游为类型消息与 Halt 状态处理，本地为字典扫描、去重并丢孤立 tool；常量名对应但文本不同，不认定该文本为原文引用 | N；未发现足以确认逐段移植的对应，采用版本仍待核 |
| mcb/reflexes.py：Instinct、overview | N：numen-api/common/src/main/java/com/dwinovo/numen/api/task/reflex/ReflexRegistry.java | 名册生成能力自述的设计参考。上游为同步动态注册表与中文总览；本地为静态元组、英文触发与动作、配置键，未见总览原文复制 | N；不能将整个反射执行层归为其移植 |
| mcb/arbiter.py；bridge/client/mcbridge.js：mcbSlots、mcbTickSlots | N：numen-api/common/src/main/java/com/dwinovo/numen/api/task/TaskSelector.java；原版 Brain memory；AltoClef TaskChain / chains | 固定层序与看、走分通道的参考。本地为 TTL 声明栈与显式释放，上游 TaskSelector 每 tick 选择 canRun 的任务，非同一调度实现 | N；Brain、AltoClef 具体采用源码及许可未核，不凭层序相似认定衍生 |
| mcb/idle.py | N：身体与模型分层 | 自主身体行为不调用模型的设计参考；当前来源说明未指定某个上游实现 | N；具体文件与采用版本待核 |
| mcb/events.py：EventFeed；bridge/server/mcbridge_server.js：mcbEvAdd、mcbEvSince | M：src/main/java/dev/mcpfabric/events/EventBus.java：emit、recent | 有界缓冲与单调游标轮询参考；上游含 AtomicLong、同步及 SSE，本地为 JS 数组、连续事件合并及 Python 轮询，不凭环形缓冲这一通用结构认定代码复制 | M；采用版本待核 |
| mcb/events.py：_age_note；mcb/journal.py：_dropped | N：agent/src/main/java/com/dwinovo/numen/api/agent/inbox/EventQueue.java：annotateAge、flushDropped | 旧事件标年龄、溢出计数并报告的参考；本地文本、单位分支和存储结构不同 | N；不将计数和时间格式化本身认定为受保护实现复制 |
| mcb/places.py：remember、check；main.py：mc_goto | M：mcp-server/src/agent/memory.ts：fingerprint、MEMORY_STATUSES | 用方块 ID 做位置指纹并报告变化的接口语义参考；本地 JSON 地点簿，不是上游 SQLite / FTS5 存储实现 | M；上游状态 valid，本地为 ok / unknown 等，采用版本待核 |
| mcb/render.py：Kind、fail、describe_click_result | N：agent/src/main/java/com/dwinovo/numen/api/agent/script/ErrorKind.java；ai/src/main/java/com/dwinovo/numen/api/agent/llm/ToolOutcome.java；numen/common/src/main/java/com/dwinovo/numen/tools/ContainerOps.java：route、place | 失败分类、不猜 JSON 子串、动作后核对的参考。本地固定文本回执和前后菜单 diff，不是上游 JSON 信封解析或搬运实现 | N；常用错误标识和核对动作不足以单独证明表达复制 |
| mcb/tasks.py：Task.precheck、TaskRunner.start | N：numen-api/common/src/main/java/com/dwinovo/numen/api/task/Task.java：prepare、TaskDispatch.java：setTask | 先检查新任务、通过才替换旧任务的受理纪律；本地为 asyncio 步骤执行器，不是上游 tick 任务槽 | N；未据此认定整个任务层移植 |
| mcb/uplink.py：_save_turn | N：ai/src/main/java/com/dwinovo/numen/api/agent/llm/ConvoLog.java | 保留真实历史的设计参考。上游 append-only JSONL，本地使用 AstrBot 替换 API，类型、合并及膨胀保护不能归为上游 JSONL 实现 | N；AstrBot 接口适配不是 Numen 代码来源证明 |
| main.py：BARITONE_VERBS、inject_body_state | N：原始命令限制（旧说明未指定文件）；astrbot_plugin_dafeiyu_pet：状态注入先例 | 白名单约束与状态注入方式的来源线索；未证实具体实现复制 | N 的具体符号待核；dafeiyu_pet 对应源码、版本与许可未核。钩子 docstring 本轮保持原文 |
| mcb/craft.py：Crafter；mcb/containers.py：put_one；bridge/client/mcbridge.js：clickSlot | mineflayer-crafting-util（具体文件未核）；mineflayer lib/plugins/craft.js、bot.clickWindow(slot, button, mode) | 配方依赖规划、拿起再右键放一个、点击参数的参考线索；通用递归与点击操作不是直接复制证据 | 本地 reference 未提供这些对应源码；实现复用程度、采用版本与许可证均待核，不预填 MIT |
| mcb/reflex.py；mcb/tasks.py；bridge/server/mcbridge_server.js：mcbStackInfo、mcbThreats | 车万女仆（TLM）：MaidHealSelfTask、MaidPanicTask、MaidHostilesSensor、isHealMeal、setMaxCheckRate、memoryHandItemStack、StartAttacking、StopAttackingIfTargetInvalid、SetWalkTargetFromAttackTargetIfTargetOutOfReach、getTaskIdParameterDesc、Already on task；universal-modder 的 Kindred 笔记 | 现有来源线索指向进食、战斗、任务结果语义和实体攻击目标判据；未取得对应原文逐段比对，不能仅按旧注释的措辞认定直接移植，也不能证明全部仅为设计 | 具体仓库、文件、采用版本、表达复用程度与许可待核；不是已确认无义务 |
| bridge/client/mcbridge.js：attackAt、placeRecipe | 原版 Minecraft.startAttack；MC_baritone_AI_Custer_Auto/baritone_AI_Cluster_Auto_Mod/src/main/java/com/baritonecluster/bridge/ClusterBridgeClient.java：processPendingCraft | 攻击冷却与配方书 API 使用参考；已核上游 processPendingCraft 调用 handlePlaceRecipe，本地通过不同映射匹配配方并调用同一 API，不据一条 API 调用认定移植整个流程 | 原版许可未核；该 Mod 的适用许可未核，不能套用相邻 baritone-1.19.4 目录的 LICENSE |
| bridge/server/mcbridge_server.js：mcbRayBlock、mcbScanProbe、mcbAround、mcbBlockInfo | M：扫描、palette 聚合、exposed / getCollisionShape 的旧对照线索 | 当前代码记录了未采用 palette 聚合、预算限制和视觉遮挡与碰撞语义的差异；不把未采用方案列作复制实现 | M；精确上游符号与采用版本待核 |

AstrBot、KubeJS、Baritone 是运行框架或 API 提供方；这些依赖关系本身与源码复用分开记录。

## 具有实现对应或文字引用、需按具体来源核对的部分

以下存在比一般设计相似更具体的对应，集中保留来源，不概括为整个项目改编。N 的许可证据见首节；采用版本、文件级权利通知、修改标记及分发所需许可证与源码材料仍需逐项核实。

| 本地文件或符号 | 上游文件或符号 | 可证实性质与差异 | 许可与待核 |
|---|---|---|---|
| mcb/permission.py：Rule.parse、_parse_term、_Term、Judge.decide、RuleSet、FACTORY_ALLOW / ASK | N：numen-api/common/src/main/java/com/dwinovo/numen/api/permission/Rule.java：parse、Term.parse；Gate.java：decide；RuleSet.java | 解析顺序从括号校验、动词通配、拆 & 到 !、entity:、命名空间 ID、信号分支相对应；裁决按模式、主人层、出厂层及 deny / allow / ask 首次命中，连贯结构高度对应。默认表多条规则逐字相同，如 break(self_placed & !contents)、place(!hazard_item)、attack(!owned & !named & !villager)。本地不支持标签和任务期授权、将 take(*) 移至 ask、危险放置不依赖 near_placed，不能写成完整原样复制 | N；按具体实现对应记录，移植边界与采用版本待核，不以一般规则解析相似单独作法律结论 |
| mcb/permission.py：RuleSet docstring；Mode、Action、Signals、Verdict | N：同目录 RuleSet.java 及相应 Mode / Action / Signals / Verdict | RuleSet 的“规则是数据……这里只存和查，不判世界”与上游有近原文文字对应，保留原文与文件级来源。其他类型、信号、保守值与该权限体系有对应；不据短类型名独立判定复制 | N；具体文字与实现范围一并核对，不将整文件所有代码都算上游 |
| mcb/events.py：Joins、KindSpec | N：agent/src/main/java/com/dwinovo/numen/api/agent/inbox/EventTypes.java：Delivery.Joins、Type | ANY / OWN / NONE 与上游三类 joins 语义、wakes / clearedByInterrupt / toModel 声明有具体结构对应。本地部分列尚无消费者，不具备上游完整投递机制 | N；声明结构对应，完整移植与否待核 |
| mcb/ripeness.py：wait_seconds、RipenessDesk.why | N：agent/src/main/java/com/dwinovo/numen/api/agent/inbox/EventQueue.java：clampLevel、thresholdOf、maxWaitMsOf、ripeness；EventTypes.OWNER_HURT | 同为 1–10 档、条数阈值等于档位、60000*N*N/3 毫秒等待公式及 urgent / enough / long_enough 判定顺序；本地增加主人在线与冷却门。具体策略参数与分支组合相对应，不能只写成泛泛“队列灵感” | N；公式本身不单独证明版权衍生，需核采用实现及版本 |
| bridge/client/mcbridge.js：mcbPickHit、mcbFacePoint、mcbSlotRole | N：numen-api/common/src/main/java/com/dwinovo/numen/api/entity/Look.java：point、candidates、nearestOnFace；numen/common/src/main/java/com/dwinovo/numen/tools/GuiOps.java：window | 候选命中点与六面坐标构造、ResultSlot / CraftingContainer / 玩家背包分类有具体实现对应。差异包括本地面排序、面上中心优先、单 bounds、Rhino 访问与不同可放判据；不是原样三档算法或完整 GuiOps 移植 | N；表达复用范围需按采用版本进一步核对，文件级通知保留 |

## 后续核对

先确认上述实现对应部分的实际采用版本、对应文件声明和必需分发材料；再补尚缺源码的来源。本说明不作全仓法律认证，也不变更项目许可。原有真实版权和许可证通知应随适用内容保留；致谢不能代替授权或通知义务。
