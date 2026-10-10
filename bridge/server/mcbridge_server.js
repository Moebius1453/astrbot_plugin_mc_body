// 服务端侧桥 —— Nanako 的神经
//
// 三个方向：
//   下行  RCON /mcb <action>  ->  sendData('mcbridge')  ->  客户端脚本  ->  Baritone / sendChat
//   回执  event.respond(Component)  ->  RCON 读到            （见下 mcbReply）
//   上行  玩家聊天  ->  PlayerEvents.chat  ->  环形缓冲  ->  /mcb chat 拉取
//        客户端状态  ->  sendData('mcbridge_up')  ->  最新快照  ->  并进 /mcb state
//
// 返回格式（统一信封，2026-10-09 起）：
//   成功  MCB {"ok":true,"action":"state","data":{...}}
//   失败  MCB {"ok":false,"action":"state","error":"Nanako 不在线"}
//
// 用法（RCON 里去掉斜杠）：
//   /mcb ping                    连通性
//   /mcb diag                    自检：回执策略 / 时钟 / 上行情链路
//   /mcb state                   坐标/血量/维度 + 客户端上报的任务状态
//   /mcb chat <since_seq>        聊天增量
//   /mcb say <文本>               让 Nanako 说话
//   /mcb baritone <命令>          透传 Baritone（不带 #）
//   /mcb stop                    取消寻路
//
// 注意： 服务端脚本改完 RCON /reload 热重载即可 —— 不重启服务端、不踢在线玩家。
// 注意： 客户端脚本改完必须重启客户端，跟这份不是一回事。
// 注意： Rhino 的坑（都踩过）：函数体内别用 const；别调 .getClass()；别用变量名 cls；
//    event.source 是死的（Java 字段，Rhino 不暴露）—— 回执一律走 event.respond()。

const MCB_TARGET = 'Nanako'
const MCB_CHANNEL_DOWN = 'mcbridge'      // 服务端  ->  客户端
const MCB_CHANNEL_UP = 'mcbridge_up'     // 客户端  ->  服务端

// --- 扫描预算（微秒是真的，别凭感觉调）-------------------------------------
//
// 注意：注意： 2026-10-10 实测：Rhino 下每次 getBlockState 约 8~12 微秒
//    （原生 Java 约 0.1µs —— 差 50~100 倍，因为每次调用都要过 JS <-> Java 包装）。
//    所以"50ms 能读多少格"= 50000 / 10 ≈ 5,000 格，而不是几十万。
//    每格 10µs 这个数加大到 30 万次采样也没变，不是 JIT 预热。要改参数先重测。

// mcb scan 一次最多读多少格方块（定点找方块用，球形体）
const MCB_SCAN_BUDGET = 6000

// mcb around（周边概览）的两段预算，单位毫秒。
// 注意： 实测典型耗时（2026-10-10，半径 6 / 抽样步长 8）：首次 ~50ms，之后更快 ——
//    第一次调用要把没加载的 chunk 拉进来，后面走缓存。所以看到耗时忽高忽低是正常的。
//    Minecraft 一个 tick 是 50ms —— 这已经是个能感觉到的尖峰，调用别太频繁。
//    （真要频繁用，得走 T2：跨 tick 后台跑 + 读缓存。）
const MCB_AROUND_MS_CHUNK = 45      // 1 走方块实体
const MCB_AROUND_MS_SURFACE = 55    // 2 heightmap 地表抽样（累计上限，不是每段）
const MCB_AROUND_POI_MAX = 60       // 最多回多少条 POI（按距离取近的）
const MCB_AROUND_SURFACE_MAX = 8    // 地表成分回前几种

// --- 基础小工具 -----------------------------------------------------------

function mcbSplit(raw) {
  var s = ''
  if (raw !== null && raw !== undefined) s = String(raw)
  s = s.trim()
  if (s.length === 0) return ['ping', '']
  var i = s.indexOf(' ')
  if (i < 0) return [s, '']
  return [s.substring(0, i), s.substring(i + 1).trim()]
}

// 不同 KubeJS 版本事件对象的 server 入口不一样，逐个试，并把命中的记下来
function mcbServer(event) {
  var ways = [
    ['event.server', function () { return event.server }],
    ['event.source.server', function () { return event.source.server }],
    ['event.level.server', function () { return event.level.server }],
    ['event.serverPlayer.server', function () { return event.serverPlayer.server }]
  ]
  for (var i = 0; i < ways.length; i++) {
    try {
      var s = ways[i][1]()
      if (s !== null && s !== undefined) return s
    } catch (e) {
      // 试下一个
    }
  }
  console.error('[mcb] 四种 server 入口都拿不到')
  return null
}

function mcbFindPlayer(server, name) {
  // 注意： 别用 server.getPlayer(名字) —— 那个重载吃 UUID。
  //    遍历在线列表逐个比 username。
  try {
    var list = server.players
    for (var i = 0; i < list.size(); i++) {
      var q = list.get(i)
      if (String(q.username) === name) return q
    }
  } catch (e) {
    console.error('[mcb] 遍历 players 失败: ' + e)
  }
  return null
}

// 单调时钟（毫秒）。Rhino 下哪种写法能用是探出来的，不是猜的 ——
// 之前只试过 Java.loadClass('java.lang.System').currentTimeMillis()，返回 0，是错的。
//
// 注意： 命中之后要把函数本身缓存下来（mcbClockFn），不能只缓存名字再回去遍历数组 ——
//    mcbNowMs() 是在计时循环里被调的（每次都建 new Date() + 走一遍闭包数组），
//    Rhino 下这层开销本身就有几微秒，等于拿一个会拖慢被测对象的秒表去量它。
var mcbClockWinner = null
var mcbClockFn = null

function mcbNowMs() {
  if (mcbClockFn !== null) {
    try {
      var fast = Number(mcbClockFn())
      if (fast > 0) return fast
    } catch (eFast) {
      mcbClockFn = null
      mcbClockWinner = null
    }
  }
  var ways = [
    ['Date.getTime', function () { return new Date().getTime() }],
    ['Java.type.System', function () { return Java.type('java.lang.System').currentTimeMillis() }],
    ['java.lang.System', function () { return java.lang.System.currentTimeMillis() }],
    ['Java.loadClass.System', function () { return Java.loadClass('java.lang.System').currentTimeMillis() }]
  ]
  for (var i = 0; i < ways.length; i++) {
    try {
      var v = Number(ways[i][1]())
      if (v > 0) {
        if (mcbClockWinner !== ways[i][0]) {
          mcbClockWinner = ways[i][0]
          console.info('[mcb] 时钟可用: ' + ways[i][0])
        }
        mcbClockFn = ways[i][1]      //  <-  缓存函数，下次直接调
        return v
      }
    } catch (e) {
      // 试下一个
    }
  }
  return 0
}

// --- 回执 -----------------------------------------------------------------

function mcbComponent(text) {
  try {
    return Java.loadClass('net.minecraft.network.chat.Component').literal(String(text))
  } catch (e) {
    console.error('[mcb] Component.literal 不可用: ' + e)
    return null
  }
}

// 把文本回给命令源（RCON 会读到）。
// 正路是 event.respond(Component) —— 实测一次命中；event.source 是死的。
function mcbReply(event, text) {
  if (event === null || event === undefined) return false
  var comp = mcbComponent(text)
  if (comp === null) return false
  try {
    event.respond(comp)
    return true
  } catch (e) {
    console.error('[mcb] respond 失败: ' + e)
    return false
  }
}

// 统一信封
function mcbEnvelope(event, obj) {
  var body = ''
  try {
    body = JSON.stringify(obj)
  } catch (e) {
    // JSON 挂了就退化成一个平淡的失败信封，别让 RCON 拿到半截东西
    console.error('[mcb] JSON.stringify 失败: ' + e)
    body = '{"ok":false,"action":"unknown","error":"服务端序列化失败"}'
  }
  console.info('[mcb] 回执 ' + body.length + ' 字符')
  mcbReply(event, 'MCB ' + body)
}

function mcbOk(event, action, data) {
  mcbEnvelope(event, { ok: true, action: action, data: data })
}

function mcbErr(event, action, message) {
  mcbEnvelope(event, { ok: false, action: action, error: String(message) })
}

// --- 上行之一：客户端状态快照 ----------------------------------------------
//
// 客户端脚本定时把 Baritone 状态 sendData('mcbridge_up') 上来。
// 这里只存"最新一份 + 到达时刻"，读的时候做陈旧判定。
//
// 注意： 两条纪律：
//   1. 陈旧 ≠ 当前 —— 客户端崩了/没起来，快照会冻住，必须能看出来
//   2. 缺失 ≠ 空闲 —— "从没收到过" 和 "她确实在发呆" 是两回事

const MCB_UP_STALE_MS = 4000

var mcbUpSeq = 0        // 收到过多少次
var mcbUpData = null    // { status, task, goal, dist, eta, posX, posY, posZ }
var mcbUpAtMs = 0
var mcbUpHasMenu = false   // 诊断：客户端到底有没有把 menu 字段发上来
var mcbUpMenuLen = 0       // 诊断：那串 JSON 有多长

NetworkEvents.dataReceived(MCB_CHANNEL_UP, event => {
  try {
    var tag = event.data
    mcbUpSeq = mcbUpSeq + 1
    mcbUpAtMs = mcbNowMs()
    mcbUpData = {
      status: mcbTagStr(tag, 'status'),
      task: mcbTagStr(tag, 'task'),
      goal: mcbTagStr(tag, 'goal'),
      note: mcbTagStr(tag, 'note'),
      dist: mcbTagNum(tag, 'dist'),
      eta: mcbTagNum(tag, 'eta'),
      posX: mcbTagNum(tag, 'posX'),
      posY: mcbTagNum(tag, 'posY'),
      posZ: mcbTagNum(tag, 'posZ'),
      menu: mcbTagStr(tag, 'menu')
    }
    try { mcbUpHasMenu = !!tag.contains('menu') } catch (eHM) { mcbUpHasMenu = false }
    try { mcbUpMenuLen = String(mcbUpData.menu).length } catch (eML) { mcbUpMenuLen = -1 }
  } catch (e) {
    console.error('[mcb] 解析上行快照失败: ' + e)
  }
})

// CompoundTag 取值：优先 getString/getDouble，取不到退回属性访问
function mcbTagStr(tag, key) {
  if (tag === null || tag === undefined) return ''
  try {
    var v = tag.getString(key)
    if (v !== null && v !== undefined) return String(v)
  } catch (e) { }
  try {
    var w = tag[key]
    if (w !== null && w !== undefined) return String(w)
  } catch (e2) { }
  return ''
}

function mcbTagNum(tag, key) {
  if (tag === null || tag === undefined) return null
  // 注意： 必须先 contains 再取值 —— CompoundTag.getDouble(不存在的键) 返回 0.0，
  //    不是 null。「缺失」被当成 0 会让下游说"还剩 0 格"，是假信息。
  var has = false
  try {
    has = !!tag.contains(key)
  } catch (e0) {
    has = false
  }
  if (!has) return null
  try {
    var v = tag.getDouble(key)
    if (v !== null && v !== undefined) return Number(v)
  } catch (e) { }
  try {
    var w = tag[key]
    if (w !== null && w !== undefined) return Number(w)
  } catch (e2) { }
  return null
}

// 上行里的 menu 是客户端序列化好的一串 JSON（见 mcbridge.js）。
// 注意： mcbTagStr 对不存在的键返回 ''（字符串没有 getDouble 那个 0.0 陷阱），
//    所以这里把 '' 当"没有"。
function mcbParseJsonField(s) {
  if (s === null || s === undefined) return null
  var t = String(s)
  if (t.length === 0) return null
  try {
    return JSON.parse(t)
  } catch (e) {
    return { err: String(e) }
  }
}

// 把快照整理成给 RCON 的形态，带陈旧判定
function mcbTaskSnapshot() {
  if (mcbUpData === null) {
    return { available: false, reason: '客户端从未上报（客户端脚本未加载，或 Nanako 客户端没在跑）' }
  }
  var now = mcbNowMs()
  var ageMs = (now > 0 && mcbUpAtMs > 0) ? (now - mcbUpAtMs) : -1
  var stale = (ageMs < 0) ? null : (ageMs > MCB_UP_STALE_MS)
  return {
    available: true,
    seq: mcbUpSeq,
    ageMs: ageMs,
    stale: stale,
    status: mcbUpData.status,
    task: mcbUpData.task,
    goal: mcbUpData.goal,
    note: mcbUpData.note,
    dist: mcbUpData.dist,
    eta: mcbUpData.eta,
    posX: mcbUpData.posX,
    posY: mcbUpData.posY,
    posZ: mcbUpData.posZ,
    // 注意： 客户端自己的朝向 —— 和 mcbPlayerState 里那个（服务端收到的）不是一回事。
    //    Baritone 的 SERVER 模式是"静默转"：服务端收到一个朝向、本地画面是另一个。
    //    调试 aim 时必须看这两个的差值，否则会把"转了但没同步"误判成"没转"。
    cliYaw: mcbUpData.yaw,
    cliPitch: mcbUpData.pitch,
    // 当前打开的界面（容器/工作台/背包合成格）。合成和拿箱子全靠它。
    menu: mcbParseJsonField(mcbUpData.menu)
  }
}

// --- 上行之二：聊天环形缓冲 -------------------------------------------------
//
// 注意： 不在这里过滤 Nanako —— 她自己的发言也要留档（AstrBot 侧要拿它当
//    "你刚才说过…" 的上下文前缀）。谁的话能唤醒白是策略，在插件里判。

const MCB_CHAT_MAX = 300
const MCB_CHAT_PAGE = 50

var mcbChatBuf = []
var mcbChatSeq = 0

PlayerEvents.chat(event => {
  try {
    var who = '?'
    try { who = String(event.username) } catch (e1) { }
    var txt = ''
    try { txt = String(event.rawText) } catch (e2) { }
    if (txt === '' || txt === 'undefined' || txt === 'null') {
      try { txt = String(event.message) } catch (e3) { txt = '?' }
    }
    if (txt.length > 256) txt = txt.substring(0, 256) + '…'

    // 注意： 说话的人在哪儿 —— 她才知道"山那边"是哪个方向。
    //    用户 2026-10-10 定的：视距内自己看，视距外靠人告诉；
    //    真人也没有世界地图，是听别人说。
    //    事件对象上 player 和 getEntity() 两条路都试（Rhino 下哪个通用是探出来的）。
    var px = null, py = null, pz = null
    var pl = null
    try { pl = event.player } catch (eP1) { }
    if (pl === null || pl === undefined) { try { pl = event.getEntity() } catch (eP2) { } }
    if (pl !== null && pl !== undefined) {
      try { px = mcbNumOrNull(pl.x) } catch (eP3) { }
      try { py = mcbNumOrNull(pl.y) } catch (eP4) { }
      try { pz = mcbNumOrNull(pl.z) } catch (eP5) { }
    }

    mcbChatSeq = mcbChatSeq + 1
    var line = { seq: mcbChatSeq, t: mcbNowMs(), who: who, text: txt }
    if (px !== null && py !== null && pz !== null) line.pos = [px, py, pz]
    mcbChatBuf.push(line)
    while (mcbChatBuf.length > MCB_CHAT_MAX) mcbChatBuf.shift()
    console.info('[mcb] chat #' + mcbChatSeq + ' <' + who + '> ' + txt
      + (line.pos ? ' @' + line.pos.join(',') : ''))
  } catch (e) {
    console.error('[mcb] chat 事件处理失败: ' + e)
  }
})

// --- 事件流（除了聊天以外的事）--------------------------------------------
//
// 注意： 和聊天缓冲分开 —— 聊天已经在 mcbChatBuf 里带 seq 了，
//    再录一份进事件流就是同一件事记两遍，白的 [log] 里会出现重复。
//    这张表只管聊天以外的事：挨打 / 死亡 / 进出服 / 背包变动。
//
// 机制抄 mcpfabric 的 EventBus：环形缓冲 + 单调 seq + 按 sinceId 增量拉取。
// 为什么不用"每次都推"：推送没法重连、没法去重、断一次就丢；拉取式天然可重放。
//
// 注意： 每个 Events.xxx(...) 注册都包在 try 里 —— 事件名要是这个版本没有，
//    整个脚本会加载失败（KubeJS 脚本是加载期执行的）。宁可少一个钩子，不能全挂。
const MCB_EV_MAX = 600

// 注意：注意： 同类事件必须合并 —— 这是 2026-10-10 实测踩出来的。
//
//    她淹在水里时 drown 每秒产生一条 hurt 事件，实测堆了 253+ 条 ——
//    环形缓冲只有 600 条，于是真正要看的（死亡 / 进服 / 给东西）全被挤出去了。
//    mcb events 拉回来的东西几乎全是同一句噪声。
//
//    规则：连续同 (kind, who, by) 且间隔小于这个窗口的，合并成一条并累加 n。
//    * 为什么要求"连续"：中间夹了别的事件就不该合并，否则会打乱先后顺序。
//    * 为什么刷新窗口（用 lastT 而不是 t）：溺水是持续性的，
//      不刷新的话每 3 秒又开一条新的，10 分钟照样 200 条。刷新  ->  整段只留一条。
const MCB_EV_MERGE_MS = 3000

var mcbEvBuf = []
var mcbEvSeq = 0

function mcbEvAdd(kind, who, text, extra) {
  var t = mcbNowMs()
  var by = null
  if (extra !== null && extra !== undefined && extra.by !== undefined) by = extra.by

  var last = mcbEvBuf.length > 0 ? mcbEvBuf[mcbEvBuf.length - 1] : null
  if (last !== null && last.kind === kind && last.who === who
      && (last.by === undefined ? null : last.by) === by
      && (t - (last.lastT !== undefined ? last.lastT : last.t)) < MCB_EV_MERGE_MS) {
    last.n = (last.n || 1) + 1
    last.lastT = t
    return last
  }

  mcbEvSeq = mcbEvSeq + 1
  var e = { seq: mcbEvSeq, t: t, kind: kind, who: who, text: text, n: 1 }
  if (extra !== null && extra !== undefined) {
    for (var k in extra) {
      if (Object.prototype.hasOwnProperty.call(extra, k)) e[k] = extra[k]
    }
  }
  mcbEvBuf.push(e)
  while (mcbEvBuf.length > MCB_EV_MAX) mcbEvBuf.shift()
  return e
}

function mcbEvSince(since) {
  var out = []
  for (var i = 0; i < mcbEvBuf.length; i++) {
    if (mcbEvBuf[i].seq > since) out.push(mcbEvBuf[i])
  }
  return {
    max: mcbEvSeq,
    count: out.length,
    lines: out,
    total: mcbEvBuf.length
  }
}

// 注意：注意： 背包事件已废弃（2026-10-10 实测后删除）—— 别再往这儿加回来
//
// 这里原来注册的是 PlayerEvents.inventoryChanged，用来抓"谁给了我东西"。
// 实测它根本不 fire：墓碑取物明明把背包改了（grave_key×2  ->  ×1、
// 多了 oak_log×3），服务端日志里一条都没有，mcb events 是 0，
// 而且注册本身不抛错（所以不看数据根本发现不了）。
//
// 原因：/give 和接墓碑走的都是 Inventory.add()，
// 绕过 AbstractContainerMenu 的槽位监听器 ——
// 而 KubeJS 的 KubeJSInventoryListener.slotChanged(menu, slot, item)
// 挂在后者上。
//
// 已完成： 替代方案在插件侧：main/astrbot_plugin_mc_body/mcb/events.py 的
//    EventFeed._poll_inventory() —— 拉取式背包 diff（docs/13 §4 原本的方案），
//    保证有效，代价只是延迟一个轮询周期。
//
// 这张表（mcbEvBuf）只管聊天以外的事件：挨打 / 死亡 / 进出服。
// 那三个挂的是 NeoForge 事件，不是槽位监听器，所以是可靠的。

// ---- 挨打 / 死亡 / 进出服 -------------------------------------------------
//
// 判据：只记和白有关的（她被谁打、谁死了、谁进服了、在场玩家挨打）。
// 全场每一只怪挨打都记的话，缓冲立刻被刷干净。

function mcbIsPlayer(e) {
  try { return !!e.isPlayer() } catch (e1) { }
  try { return String(e.type).indexOf('player') >= 0 } catch (e2) { }
  return false
}

function mcbEntityLabel(e) {
  try { return String(e.name.string) } catch (e1) { }
  try { return String(e.username) } catch (e2) { }
  try { return String(e.type) } catch (e3) { }
  return '?'
}

// 读玩家的游戏名。
//
// 注意：注意： 2026-10-10 踩到的大坑：Player 这个类没有 username 字段
//    （那是 PlayerChatReceivedKubeEvent.getUsername() 那个事件上的方法，
//     或者旧映射 / ServerPlayer 才有的东西）。
//    所以 String(pl.username) 拿到的是 "undefined" 这个字符串，
//    不是空串 —— 于是
//        if (nm === '') { 试下一个 }    <-  永远不进
//        if (nm !== MCB_TARGET) return   <-  永远命中，静默跳过
//    实测后果：inventoryChanged 一条都没记（背包明明变了，mcb events 是 0）。
//    注意： 教训：判"没读到"要判 undefined / "undefined" / null / "null"，
//       不能只判空串 —— String(undefined) 是有内容的。
function mcbPlayerName(pl) {
  if (pl === null || pl === undefined) return null
  var cands = []
  try { cands.push(pl.username) } catch (e1) { }
  try { cands.push(pl.name.string) } catch (e2) { }
  try { cands.push(pl.getName().getString()) } catch (e3) { }
  try { cands.push(pl.scoreboardName) } catch (e4) { }
  try { cands.push(pl.gameProfile.name) } catch (e5) { }
  for (var i = 0; i < cands.length; i++) {
    var s = cands[i]
    if (s === null || s === undefined) continue
    s = String(s)
    if (s !== '' && s !== 'undefined' && s !== 'null') return s
  }
  return null
}

var mcbWalkIntent = null

const MCB_PLACED_KEY = 'mc_body_placed_v1'

function mcbPlacedTable(level) {
  var data = level.server.persistentData
  var $Tag = Java.loadClass('net.minecraft.nbt.CompoundTag')
  if (!data.contains(MCB_PLACED_KEY, 10)) data.put(MCB_PLACED_KEY, new $Tag())
  var root = data.getCompound(MCB_PLACED_KEY)
  var dim = String(level.dimension)
  if (!root.contains(dim, 10)) root.put(dim, new $Tag())
  return root.getCompound(dim)
}

function mcbPlacedKey(pos) {
  return pos.getX() + ',' + pos.getY() + ',' + pos.getZ()
}

function mcbPlacedFact(level, pos) {
  var table = mcbPlacedTable(level)
  var key = mcbPlacedKey(pos)
  var state = level.getBlockState(pos)
  var id = mcbBlockId(state)
  var record = null
  if (table.contains(key, 10)) {
    var stored = table.getCompound(key)
    if (state.isAir() || String(stored.getString('block')) !== id) {
      table.remove(key)
    } else {
      record = {
        uuid: String(stored.getString('uuid')),
        name: String(stored.getString('name')),
        block: String(stored.getString('block'))
      }
    }
  }
  return { pos: [pos.getX(), pos.getY(), pos.getZ()], dim: String(level.dimension),
    block: id, placed: record, blockEntity: level.getBlockEntity(pos) !== null }
}

function mcbBreakVerdict(fact, uuid) {
  if (fact.placed !== null && fact.placed.uuid !== uuid) {
    return { allowed: false, kind: 'needs_consent',
      detail: '这是 ' + fact.placed.name + ' 放置的方块，尚未获得拆除许可' }
  }
  if (fact.blockEntity && fact.placed === null) {
    return { allowed: false, kind: 'needs_consent',
      detail: '这是来源未知的容器或设施，不能直接拆除' }
  }
  return { allowed: true, kind: 'allowed', detail: '' }
}

BlockEvents.placed(event => {
  try {
    var entity = event.getEntity()
    if (entity === null || !mcbIsPlayer(entity)) return
    var block = event.getBlock()
    var level = event.getLevel()
    var uuid = String(entity.uuid)
    if (!mcbIsUsableName(uuid)) throw new Error('放置者 UUID 不可用')
    var $Tag = Java.loadClass('net.minecraft.nbt.CompoundTag')
    var record = new $Tag()
    record.putString('uuid', uuid)
    record.putString('name', mcbPlayerName(entity) || uuid)
    record.putString('block', mcbBlockId(block.getBlockState()))
    mcbPlacedTable(level).put(mcbPlacedKey(block.getPos()), record)
  } catch (err) {
    console.error('[mcb] 放置记录失败: ' + err)
  }
})

BlockEvents.broken(event => {
  var deny = null
  try {
    var entity = event.getEntity()
    if (mcbPlayerName(entity) !== MCB_TARGET) return
    var block = event.getBlock()
    var fact = mcbPlacedFact(block.getLevel(), block.getPos())
    var uuid = String(entity.uuid)
    if (!mcbIsUsableName(uuid)) throw new Error('执行者 UUID 不可用')
    var verdict = mcbBreakVerdict(fact, uuid)
    if (!verdict.allowed) {
      deny = verdict.detail
      mcbEvAdd('permission', MCB_TARGET,
        '[mc:permission] error: ' + verdict.kind + '；不能拆 ' + fact.block
          + ' @ ' + fact.pos.join(',') + '：' + deny
          + '。请停止这项操作并告诉用户，重复寻路不会获得许可。',
        { by: fact.pos.join(',') + ':' + (mcbWalkIntent ? mcbWalkIntent.token : ''),
          pos: fact.pos, block: fact.block,
          walkToken: mcbWalkIntent ? mcbWalkIntent.token : '',
          walkOwner: mcbWalkIntent ? mcbWalkIntent.owner : '' })
    }
  } catch (err) {
    deny = '权限事实读取失败: ' + err
    mcbEvAdd('permission', MCB_TARGET, '[mc:permission] error: refused；' + deny,
      { by: 'facts-error:' + (mcbWalkIntent ? mcbWalkIntent.token : ''),
        walkToken: mcbWalkIntent ? mcbWalkIntent.token : '',
        walkOwner: mcbWalkIntent ? mcbWalkIntent.owner : '' })
    console.error('[mcb] ' + deny)
  }
  // cancel 通过 EventExit 结束处理，不能被上面的错误处理吞掉。
  if (deny !== null) event.cancel()
})

// DamageSource  ->  "谁打的 / 什么打的"。
//
// 注意：注意： 2026-10-10 实测结论（一次跑完全部候选访问器得出的，别再猜）：
//
//     str              = "DamageSource (arrow)"    <-  已完成： 唯一可靠的一条
//     getMsgId         = notFn      ┐
//     getEntity        = notFn      │ KubeJS 给我们的这个 DamageSource 对象
//     getDirectEntity  = notFn      │ 实体访问器一个都没有
//     getCausingEntity = notFn      ┘
//     getType          = 存在，但返回的不是实体（是 Holder<DamageType>）
//
// 所以拿不到"具体是谁打的"。但 String(src) 稳定给出伤害类型：
//     mob（近战怪）· arrow（远程）· fall（摔）· lava · player_attack …
//
//  ->  报类型，不编造攻击者。 类型本身就有用："摔的"和"怪咬的"处理方式完全不同。
//   （"谁在打我"那件事由反射的 mc_threats 负责 —— 它有 targeting 字段。）
function mcbSourceLabel(src) {
  if (src === null || src === undefined) return null
  // 先试实体访问器 —— 万一别的伤害类型上它们存在
  var ent = null
  try { if (typeof src.getEntity === 'function') ent = src.getEntity() } catch (e1) { }
  if (ent !== null && ent !== undefined) {
    var nm = mcbEntityName(ent)
    if (mcbIsUsableName(nm)) return nm
  }
  // 兜底也是唯一可靠的路：从 toString() 里抠出括号里的类型
  try {
    var s = String(src)
    var open = s.indexOf('(')
    var close = s.lastIndexOf(')')
    if (open >= 0 && close > open + 1) {
      var kind = s.substring(open + 1, close).trim()
      if (mcbIsUsableName(kind)) return kind
    }
  } catch (e2) { }
  return null
}

// "这个名字能不能用"。注意： 判据要全：String(undefined) 会给出带内容的 "undefined"，
// 光判空串是不够的（这个坑踩过两次了）。
function mcbIsUsableName(v) {
  if (v === null || v === undefined) return false
  if (typeof v === 'function') return false
  var s = String(v)
  if (s === '' || s === 'undefined' || s === 'null' || s === 'Function') return false
  if (s.indexOf('function ') === 0) return false
  return true
}

// DamageSource 的原始诊断 —— 只留一行 str。
//
// 注意： 原来这里把"所有候选访问器全试一遍"的结果塞进事件里，那是一次性的排查手段。
//    答案已经拿到了（见 mcbSourceLabel 的结论），再留着就是每条挨打事件多背十几行噪声。
//    排查完就收窄 —— 别让诊断永久占着数据带宽。
function mcbSourceDebug(src) {
  if (src === null || src === undefined) return { null: true }
  try { return { str: String(src).substring(0, 60) } } catch (e) { return { str: 'ERR' } }
}

try {
  EntityEvents.afterHurt(event => {
    try {
      var victim = null
      try { victim = event.entity } catch (eV) { }
      if (victim === null || victim === undefined) { try { victim = event.getEntity() } catch (eV2) { } }
      if (victim === null || victim === undefined) return

      var dmg = null
      try { dmg = Number(event.damage) } catch (eD) {
        try { dmg = Number(event.getDamage()) } catch (eD2) { }
      }
      // 注意： 先试 getSource() —— 它是这个事件文档上有的方法（已解常量池确认）。
      //    event.source 那条路实测会拿到不对的东西（by 报成 "Function"），
      //    所以只当兜底，不抢先。
      var src = null
      try { src = event.getSource() } catch (eS2) {
        try { src = event.source } catch (eS1) { }
      }
      var vname = mcbEntityLabel(victim)
      var attacker = mcbSourceLabel(src)

      // 注意： 一次 LivingDamageEvent 会同时产生"她挨打"和"对方挨打"两条，
      //    只看我们关心的那两头，不然一次互殴记两条噪声。
      var iAmVictim = (vname === MCB_TARGET)
      var iAmAttacker = (attacker === MCB_TARGET)
      if (!iAmVictim && !iAmAttacker && !mcbIsPlayer(victim)) return

      var hp = null
      try { hp = mcbNumOrNull(victim.health) } catch (eH) { }
      var extra = {}
      if (dmg !== null && !isNaN(dmg)) extra.dmg = Math.round(dmg * 10) / 10
      if (attacker !== null) extra.by = attacker
      if (hp !== null) extra.hp = hp
      // 注意： 诊断字段 —— by 要是不对，看这个就知道是哪条路断了。
      //    （僵尸咬一口报 "Function" 那次，就是靠它定位的）
      try { extra.srcDbg = mcbSourceDebug(src) } catch (eSd) { }

      if (iAmVictim) {
        mcbEvAdd('hurt', attacker === null ? '环境' : attacker, MCB_TARGET + ' 挨打', extra)
      } else if (iAmAttacker) {
        mcbEvAdd('hit', MCB_TARGET, '打了 ' + vname, extra)
      } else {
        mcbEvAdd('hurt', attacker === null ? '环境' : attacker, vname + ' 挨打', extra)
      }
    } catch (e) { }
  })
} catch (eHurtReg) {
  console.error('[mcb] afterHurt 注册失败: ' + eHurtReg)
}

try {
  EntityEvents.death(event => {
    try {
      var ent = null
      try { ent = event.entity } catch (eE) { }
      if (ent === null || ent === undefined) { try { ent = event.getEntity() } catch (eE2) { } }
      if (ent === null || ent === undefined) return
      var src = null
      try { src = event.source } catch (eS1) {
        try { src = event.getSource() } catch (eS2) { }
      }
      var who = mcbEntityLabel(ent)
      var killer = mcbSourceLabel(src)
      var isPlayer = mcbIsPlayer(ent)
      if (!isPlayer && killer !== MCB_TARGET) return   // 怪之间互杀不记
      mcbEvAdd('death', killer === null ? '环境' : killer, who + ' 死了')
    } catch (e) { }
  })
} catch (eDeathReg) {
  console.error('[mcb] death 注册失败: ' + eDeathReg)
}

try {
  PlayerEvents.loggedIn(event => {
    try {
      var pl = null
      try { pl = event.player } catch (eP) { }
      if (pl === null || pl === undefined) { try { pl = event.getEntity() } catch (eP2) { } }
      var nm = pl === null || pl === undefined ? '?' : mcbEntityLabel(pl)
      var extra = null
      if (pl !== null && pl !== undefined) {
        try {
          extra = { pos: [mcbNumOrNull(pl.x), mcbNumOrNull(pl.y), mcbNumOrNull(pl.z)] }
        } catch (ePos) { }
      }
      mcbEvAdd('join', nm, nm + ' 进服了', extra)
    } catch (e) { }
  })
} catch (eInReg) {
  console.error('[mcb] loggedIn 注册失败: ' + eInReg)
}

try {
  PlayerEvents.loggedOut(event => {
    try {
      var pl = null
      try { pl = event.player } catch (eP) { }
      if (pl === null || pl === undefined) { try { pl = event.getEntity() } catch (eP2) { } }
      var nm = pl === null || pl === undefined ? '?' : mcbEntityLabel(pl)
      mcbEvAdd('leave', nm, nm + ' 退服了')
    } catch (e) { }
  })
} catch (eOutReg) {
  console.error('[mcb] loggedOut 注册失败: ' + eOutReg)
}

// ===== T2：跨 tick 增量扫描 ===============================================
//
// 它补的是 T1 的缺口。
//   T1（mcb around）走「方块实体 + heightmap 抽样」，一次 ~50ms 就能给个概览，
//   但它拿不到没有方块实体的方块 —— 工作台 / 铁砧 / 石切机 / 织布机 / 制箭台 /
//   制图台 / 锻造台 / 砂轮 / 堆肥桶 / 营火 / 传送门框… 这些统统不在
//   chunk.getBlockEntities() 那张表里，T1 看不见它们。
//
//   全分辨率逐方块扫一遍要几十万次读（Rhino 下每格 ~10µs），一个 tick 干不完 ——
//   600 格/tick 就是 6ms，扫 6 chunk 的地表带要跑好几百个 tick。
//   所以做成跨 tick 的后台任务：每 tick 推进一小块，结果进缓存，随时可查。
//
// 注意： 每 tick 的读数是"预算"，不是"能扫多快扫多快" —— 这是共用服务器，
//    用户就在同一个服里玩。预算调大会让 MSPT 直接涨，别乱调。
//
// 注意： tick 钩子的第一句必须是纯 JS 判断（mcbT2 === null），不碰任何 Java。
//    挂 tick 最怕"每 tick 都白跑一遍 Java 调用" —— 那是 20 次/秒的纯浪费。

const MCB_T2_READS_PER_TICK = 600        // 每 tick 最多读多少格（≈6ms，占 50ms tick 的 12%）
const MCB_T2_BAND_BELOW = 4              // 地表往下扫几格
const MCB_T2_BAND_ABOVE = 4              // 地表往上扫几格
const MCB_T2_POI_MAX = 300               // POI 最多留多少条（按距离近的优先）
const MCB_T2_KEEP_MS = 10 * 60 * 1000    // 扫完之后结果保留多久

// 值得单列出来的方块（键是注册名的路径部分，不带命名空间）。
// 挑的原则：没有方块实体，因此 T1 看不见 —— 这正是 T2 存在的理由。
// 少数有方块实体的（刷怪笼/唱片机）也顺手带上，重复了由下游按坐标去重。
const MCB_T2_POI_IDS = {
  'crafting_table': '工作台',
  'anvil': '铁砧', 'chipped_anvil': '开裂的铁砧', 'damaged_anvil': '损坏的铁砧',
  'stonecutter': '石切机', 'loom': '织布机', 'fletching_table': '制箭台',
  'cartography_table': '制图台', 'smithing_table': '锻造台', 'grindstone': '砂轮',
  'composter': '堆肥桶', 'bell': '钟',
  'campfire': '营火', 'soul_campfire': '灵魂营火',
  'cauldron': '炼药锅', 'water_cauldron': '装水的炼药锅',
  'lava_cauldron': '装岩浆的炼药锅', 'powder_snow_cauldron': '装细雪的炼药锅',
  'end_portal_frame': '末地传送门框', 'nether_portal': '下界传送门',
  'scaffolding': '脚手架', 'lightning_rod': '避雷针', 'target': '标靶',
  'spawner': '刷怪笼', 'jukebox': '唱片机', 'bee_nest': '蜂巢', 'beehive': '蜂箱'
}

var mcbT2 = null        // 没在跑、也没有结果时是 null
var mcbT2BP = null      // 缓存的 BlockPos 类
var mcbT2Height = null  // 缓存的 Heightmap.Types.MOTION_BLOCKING

function mcbT2Setup() {
  if (mcbT2BP === null) {
    try { mcbT2BP = Java.loadClass('net.minecraft.core.BlockPos') } catch (eBP) { }
  }
  if (mcbT2Height === null) {
    try {
      var $T = Java.loadClass('net.minecraft.world.level.levelgen.Heightmap$Types')
      mcbT2Height = $T.MOTION_BLOCKING
    } catch (eH) { }
  }
  return mcbT2BP !== null && mcbT2Height !== null
}

// 注意： 热路径专用：mcbBlockId 每次调用都按顺序试 4 条路 —— 一次扫描几百万次读，
//    那个开销会让 10µs/格 直接翻几倍。这里认准已经探明的那一条。
function mcbBlockIdFast(state) {
  if (mcbBlockIdVia === 'state.id') { try { return String(state.id) } catch (e1) { } }
  else if (mcbBlockIdVia === 'state.block.id') { try { return String(state.block.id) } catch (e2) { } }
  else if (mcbBlockIdVia === 'state.block') { try { return String(state.block) } catch (e3) { } }
  else if (mcbBlockIdVia === 'state.toString') { try { return String(state) } catch (e4) { } }
  return mcbBlockId(state)     // 还不知道走哪条 —— 走一次慢路把它定下来
}

function mcbShortId(id) {
  var i = id.indexOf(':')
  return i < 0 ? id : id.substring(i + 1)
}

function mcbT2Start(player, radiusChunks) {
  if (!mcbT2Setup()) return { err: 'BlockPos / Heightmap 取不到，扫不了' }
  var level = null
  try { level = player.level } catch (e0) { return { err: 'level: ' + e0 } }

  var px = Number(player.x), py = Number(player.y), pz = Number(player.z)
  var pcx = Math.floor(px / 16), pcz = Math.floor(pz / 16)

  var chunks = []
  for (var dx = -radiusChunks; dx <= radiusChunks; dx++) {
    for (var dz = -radiusChunks; dz <= radiusChunks; dz++) {
      chunks.push([pcx + dx, pcz + dz, dx * dx + dz * dz])
    }
  }
  chunks.sort(function (a, b) { return a[2] - b[2] })   // 近的 chunk 先扫

  mcbT2 = {
    level: level, who: mcbEntityLabel(player),
    px: px, py: py, pz: pz, cx: pcx, cz: pcz,
    radiusChunks: radiusChunks,
    chunks: chunks, ci: 0, col: 0,
    counts: {}, nearest: {}, poi: [], total: 0, scannedCols: 0, reads: 0,
    skippedChunks: 0, startedAt: mcbNowMs(), doneAt: 0, ticks: 0, ms: 0, err: []
  }
  return mcbT2Status()
}

function mcbT2Status() {
  mcbT2Reap()
  if (mcbT2 === null) return { job: 'none' }
  var t = mcbT2
  var totalCols = t.chunks.length * 256
  var doneCols = t.ci * 256 + t.col
  return {
    job: t.doneAt > 0 ? 'done' : 'running',
    who: t.who,
    center: [Math.round(t.px), Math.round(t.py), Math.round(t.pz)],
    radiusChunks: t.radiusChunks,
    pct: Math.round(doneCols * 1000 / totalCols) / 10,
    scannedCols: t.scannedCols,
    reads: t.reads,
    ticks: t.ticks,
    ms: t.doneAt > 0 ? t.ms : Math.round(mcbNowMs() - t.startedAt),
    types: Object.keys(t.counts).length,
    poi: t.poi.length,
    skippedChunks: t.skippedChunks,
    perTick: MCB_T2_READS_PER_TICK,
    err: t.err.slice(0, 3)
  }
}

// 每 tick 推进一小块。调用方已经确认过 mcbT2 !== null && doneAt === 0。
function mcbT2Step() {
  var t = mcbT2
  if (t === null || t.doneAt > 0) return
  t.ticks++

  var level = t.level
  if (level === null || level === undefined) { t.err.push('level 丢了'); mcbT2 = null; return }

  var perCol = MCB_T2_BAND_ABOVE + MCB_T2_BAND_BELOW + 1   // 每列几次方块读
  var colCost = perCol + 1                                  // 再加一次 getHeight
  var budget = MCB_T2_READS_PER_TICK
  // 注意：注意： used 是本 tick 的计数器，t.reads 是累计的 —— 别混用。
  //    第一版拿 t.reads 直接当预算判据，于是第一个 tick 就冲到 600，
  //    之后每 tick 只扫 1 列、再往后 0 列，任务永远跑不完（实测 8 秒才 67 列）。
  var used = 0

  while (used < budget) {
    if (t.ci >= t.chunks.length) {
      t.doneAt = mcbNowMs()
      t.ms = Math.round(t.doneAt - t.startedAt)
      return
    }

    var c = t.chunks[t.ci]
    var ch = null
    try { ch = level.getChunk(c[0], c[1]) } catch (eC) { ch = null }
    var skip = (ch === null || ch === undefined)
    if (!skip) { try { if (ch.isEmpty()) skip = true } catch (eE) { } }
    if (skip) { t.ci++; t.col = 0; t.skippedChunks++; continue }   // 没加载的 chunk 直接跳

    var colsLeft = 256 - t.col
    var colsNow = Math.floor((budget - used) / colCost)
    if (colsNow < 1) colsNow = 1
    if (colsNow > colsLeft) colsNow = colsLeft

    for (var k = 0; k < colsNow; k++, t.col++) {
      var wx = c[0] * 16 + (t.col & 15)
      var wz = c[1] * 16 + ((t.col >> 4) & 15)

      // 注意： getHeight 收的是世界坐标（内部自己 & 15），别传 chunk 内局部坐标。
      var sy = -1
      try { sy = Number(ch.getHeight(mcbT2Height, wx, wz)) } catch (eH) { sy = -1 }
      used += colCost
      t.reads += colCost
      if (!(sy > -100)) continue          // 拿不到高度就跳过这一列（缺失 ≠ 0）
      t.scannedCols++

      for (var dy = -MCB_T2_BAND_BELOW; dy <= MCB_T2_BAND_ABOVE; dy++) {
        var wy = sy + dy
        var id = null
        try { id = mcbBlockIdFast(level.getBlockState(new mcbT2BP(wx, wy, wz))) } catch (eS) { id = null }
        if (id === null) continue
        var s = mcbShortId(id)
        if (s === 'air' || s === 'cave_air' || s === 'void_air') continue
        t.total++
        t.counts[id] = (t.counts[id] || 0) + 1
        if (t.nearest[id] === undefined) t.nearest[id] = [wx, wy, wz]
        if (MCB_T2_POI_IDS[s] !== undefined && t.poi.length < MCB_T2_POI_MAX) {
          var ddx = wx + 0.5 - t.px, ddy = wy + 0.5 - t.py, ddz = wz + 0.5 - t.pz
          t.poi.push({
            id: id, what: MCB_T2_POI_IDS[s], x: wx, y: wy, z: wz,
            d: Math.round(Math.sqrt(ddx * ddx + ddy * ddy + ddz * ddz) * 10) / 10
          })
        }
      }
    }
    if (t.col >= 256) { t.ci++; t.col = 0 }
  }
}

function mcbT2Get() {
  mcbT2Reap()
  if (mcbT2 === null) return { job: 'none' }
  var t = mcbT2
  var arr = []
  for (var k in t.counts) {
    if (Object.prototype.hasOwnProperty.call(t.counts, k)) {
      arr.push({ id: k, n: t.counts[k], nearest: t.nearest[k] })
    }
  }
  arr.sort(function (a, b) { return b.n - a.n })
  var poi = t.poi.slice(0)
  poi.sort(function (a, b) { return a.d - b.d })
  return { job: t.doneAt > 0 ? 'done' : 'running', status: mcbT2Status(), types: arr, poi: poi }
}

function mcbT2Stop() {
  var s = mcbT2Status()
  mcbT2 = null
  return { stopped: true, was: s }
}

// 扫完的结果留一会儿好让插件拉到，过期就清掉。
// 注意： 惰性清理，不在 tick 里判过期 —— mcbNowMs() 在 Rhino 里是一次 Java 调用，
//    放进 tick 就是每 tick 白烧一次，而且一烧就是 MCB_T2_KEEP_MS 那么久。
//    这两个入口本来就调用得稀疏，放这儿判最划算。
function mcbT2Reap() {
  if (mcbT2 === null) return
  if (mcbT2.doneAt <= 0) return
  if (mcbNowMs() - mcbT2.doneAt > MCB_T2_KEEP_MS) mcbT2 = null
}

try {
  ServerEvents.tick(event => {
    // 注意： 第一句必须是纯 JS 判断，不碰任何 Java。 没任务时这个钩子成本接近零 ——
    //    挂 tick 最怕的就是"每 tick 都白跑一遍 Java 调用"，那是 20 次/秒的纯浪费。
    if (mcbT2 === null || mcbT2.doneAt > 0) return
    try { mcbT2Step() } catch (e) {
      if (mcbT2 !== null) mcbT2.err.push('tick: ' + e)
    }
  })
} catch (eT2Reg) {
  console.error('[mcb] ServerEvents.tick 注册失败: ' + eT2Reg)
}

// --- 背包与饥饿（服务端可读，不需要客户端）--------------------------------

// 一个物品槽的信息。每个访问器独立 try —— Rhino 下哪个能用的是探出来的，
// 所以把失败原因也带回去，一次测试就能知道要改哪里。
function mcbStackInfo(stack) {
  if (stack === null || stack === undefined) return null
  var empty = false
  try {
    empty = !!stack.isEmpty()
  } catch (e0) {
    empty = false
  }
  if (empty) return null

  var name = null
  var nameVia = null
  try {
    name = String(stack.hoverName.string); nameVia = 'hoverName'
  } catch (e1) {
    try {
      name = String(stack.getHoverName().getString()); nameVia = 'getHoverName'
    } catch (e2) {
      try {
        name = String(stack.item.descriptionId); nameVia = 'item.descriptionId'
      } catch (e3) {
        try {
          name = String(stack.item); nameVia = 'item.toString'
        } catch (e4) {
          name = '?'; nameVia = 'ALL_FAILED:' + e1 + '|' + e2
        }
      }
    }
  }

  var count = null
  try {
    count = Number(stack.count)
  } catch (e5) {
    try {
      count = Number(stack.getCount())
    } catch (e6) {
      count = null
    }
  }

  // 能不能吃、吃了顶多少 —— 判据跟车万女仆的 isHealMeal 对齐：
  // 看 FoodProperties 存不存在，而不是维护一份食物白名单（这样 mod 食物自动兼容）。
  var food = null
  var foodErr = null
  try {
    var fp = stack.getFoodProperties(null)
    if (fp !== null && fp !== undefined) {
      var nut = null, sat = null
      try { nut = Number(fp.nutrition()) } catch (f1) { try { nut = Number(fp.nutrition) } catch (f2) { nut = null } }
      try { sat = mcbNumOrNull(fp.saturation()) } catch (f3) { try { sat = mcbNumOrNull(fp.saturation) } catch (f4) { sat = null } }
      food = { nutrition: nut, saturation: sat }
    }
  } catch (eF) {
    foodErr = String(eF)
  }

  // ---- 攻击伤害：这东西能不能打 ------------------------------------------
  //
  // 注意：注意： 为什么不用名字判"是不是武器"（2026-10-10 实测）：
  //    TaCZ 的枪 hoverName 返回的是本地化 key（item.tacz.modern_kinetic_gun），
  //    拿名字判"这是不是武器"必错。
  //    属性才是最稳的判据 —— 跟"用 FoodProperties 判食物"一个思路，
  //    mod 加的近战武器自动兼容，不用我们维护名单。
  //
  // 注意：注意： 只有下面这一条读法能用（另外两条实测都不行，别再试）：
  //    不成立或禁止： stack.getAttributeModifiers(EquipmentSlot.MAINHAND) —— 方法不存在
  //    不成立或禁止： getItem().getDefaultAttributeModifiers().entries() —— 那对象没 .entries()
  //    已完成： stack.getAttributeModifiers().modifiers()  <-  这条通
  //       （元素上有 .attribute() 和 .modifier().amount()）
  //
  // 注意： modifiers=[] 空数组是正常的 —— 那就是"这东西没有攻击加成"。
  //    atk 缺省 = 没有攻击加成（不是"读失败"）。
  var atk = null
  try {
    var $Attr = Java.loadClass('net.minecraft.world.entity.ai.attributes.Attributes')
    var want = $Attr.ATTACK_DAMAGE
    var lst = stack.getAttributeModifiers().modifiers()
    var it = lst.iterator()
    while (it.hasNext()) {
      var e = it.next()
      if (String(e.attribute()) === String(want)) {
        var v = Number(e.modifier().amount())
        // 原版存的是"加成"（剑 +3），徒手基础是 1 —— 加回去才是真实伤害
        if (!isNaN(v)) atk = v + 1
        break
      }
    }
  } catch (eAtk) {
    console.warn('[mcb] 读攻击伤害失败: ' + eAtk)
  }

  // ---- 标签：#c:tools/melee_weapon 这种 ----------------------------------
  // 注意： 只取前 8 个、且去掉命名空间前缀以外的杂项 —— 全量塞进上行会把 RCON 撑爆。
  var tags = null
  try {
    var t = stack.getTags()
    if (t !== null && t !== undefined) {
      var arr = []
      var it4 = t.iterator()
      while (it4.hasNext() && arr.length < 8) {
        var tg = it4.next()
        arr.push(String(tg.location ? tg.location() : tg).replace('minecraft:', ''))
      }
      if (arr.length > 0) tags = arr
    }
  } catch (eTag) { }

  var info = { n: name, c: count, via: nameVia }
  // 注意： 必须带上注册名。「名字」是显示名，跟语言走（同一个面包在中文客户端叫"面包"、
  //    英文客户端叫"Bread"），拿它做匹配一定会错。配方里用的是 minecraft:oak_planks
  //    这种注册名 —— 合成规划就靠这个字段对齐。
  try {
    var regId = mcbItemId(stack)
    if (regId !== null) info.id = regId
  } catch (eId) { }
  if (food !== null) info.food = food
  if (atk !== null) info.atk = atk
  if (tags !== null) info.tags = tags
  if (foodErr !== null) info.foodErr = foodErr
  return info
}

function mcbInventory(player) {
  var out = { err: [], hotbar: [], main: [], offhand: null, held: null, food: null, via: null }

  try {
    var inv = player.inventory
    var i
    for (i = 0; i < 9; i++) out.hotbar.push(mcbStackInfo(inv.getItem(i)))
    for (i = 9; i < 36; i++) {
      var s = mcbStackInfo(inv.getItem(i))
      if (s !== null) {
        s.slot = i
        out.main.push(s)
      }
    }
    out.offhand = mcbStackInfo(inv.getItem(40))
    try {
      out.held = Number(inv.selected); out.via = 'inv.selected'
    } catch (e1) {
      try {
        out.held = Number(player.inventory.selected); out.via = 'player.inventory.selected'
      } catch (e2) {
        out.err.push('held: ' + e1 + ' | ' + e2)
      }
    }
  } catch (e) {
    out.err.push('inventory: ' + e)
  }

  try {
    var fd = player.foodData
    out.food = { level: Number(fd.foodLevel), saturation: Number(fd.saturationLevel) }
  } catch (e7) {
    out.err.push('food: ' + e7)
  }

  return out
}

// --- 周围扫描（服务端可读，不需要客户端）----------------------------------
//
// 白要"做计划"就得先知道周围有什么。这个也让我们测试时不瞎猜。

var mcbBlockIdVia = null

// 取方块 ID。Rhino 下哪个访问器能用是探出来的，命中一次就记住。
function mcbBlockId(state) {
  var ways = [
    ['state.id', function () { return state.id }],
    ['state.block.id', function () { return state.block.id }],
    ['state.block', function () { return state.block }],
    ['state.toString', function () { return String(state) }]
  ]
  for (var i = 0; i < ways.length; i++) {
    try {
      var v = ways[i][1]()
      if (v !== null && v !== undefined) {
        var s = String(v)
        if (s.length > 0 && s !== 'undefined' && s !== 'null') {
          if (mcbBlockIdVia !== ways[i][0]) {
            mcbBlockIdVia = ways[i][0]
            console.info('[mcb] 方块 ID 访问器命中: ' + ways[i][0])
          }
          return s
        }
      }
    } catch (e) {
      // 试下一个
    }
  }
  return null
}

function mcbScan(player, radius) {
  var out = { err: [], types: [], via: mcbBlockIdVia }
  var level = null
  try {
    level = player.level
  } catch (e0) {
    out.err.push('level: ' + e0)
    return out
  }
  if (level === null || level === undefined) {
    out.err.push('level 为空')
    return out
  }

  var px = Math.floor(Number(player.x))
  var py = Math.floor(Number(player.y))
  var pz = Math.floor(Number(player.z))
  out.center = [px, py, pz]
  out.radius = radius

  var counts = {}
  var nearest = {}
  var $BP = null
  try {
    $BP = Java.loadClass('net.minecraft.core.BlockPos')
  } catch (e1) {
    out.err.push('BlockPos: ' + e1)
    return out
  }

  var scanned = 0
  var rr = radius * radius
  for (var dx = -radius; dx <= radius; dx++) {
    for (var dy = -radius; dy <= radius; dy++) {
      for (var dz = -radius; dz <= radius; dz++) {
        // 注意： 球，不是立方体 —— 立方体的八个角占了体积的一大半，
        //    而那正是"离玩家最远"的部分，性价比最低。
        if (dx * dx + dy * dy + dz * dz > rr) continue
        var bx = px + dx, by = py + dy, bz = pz + dz
        var id = null
        try {
          id = mcbBlockId(level.getBlockState(new $BP(bx, by, bz)))
        } catch (e2) {
          id = null
        }
        scanned = scanned + 1
        if (id === null) continue
        if (id === 'minecraft:air' || id === 'air' || id.indexOf('air') === 0) continue
        counts[id] = (counts[id] || 0) + 1
        if (nearest[id] === undefined) nearest[id] = [bx, by, bz]
      }
    }
  }

  out.scanned = scanned
  out.estimateMs = Math.round(scanned * 10 / 1000)   // 按实测 10µs/格 估的
  out.via = mcbBlockIdVia

  var arr = []
  for (var k in counts) {
    if (Object.prototype.hasOwnProperty.call(counts, k)) {
      arr.push({ id: k, n: counts[k], nearest: nearest[k] })
    }
  }
  arr.sort(function (a, b) { return b.n - a.n })
  // 注意：注意： 别截断类型列表 —— 踩过（2026-10-09）：
  //    原来这里是 arr.slice(0, 25)，结果工作台（全图只有 1 个）被挤掉了，
  //    下游报"附近没有工作台"，可她离工作台只有 1.5 格。
  //    稀有方块恰恰是最要紧的那种，按数量截断等于专挑它们下手。
  //    一个预算内的扫描撑死几十种类型，全回也没多少字节。
  out.types = arr
  out.totalTypes = arr.length
  return out
}

// --- 索敌（服务端可读，不需要客户端）--------------------------------------
//
// 抄的是原版那套（女仆的 MaidHostilesSensor 也是抄原版）：
// 扫附近实体  ->  按"是不是敌对"过滤  ->  按距离排序  ->  取最近的。
// 我们不用实体的 Brain/Sensor 系统（那是给实体用的），直接查世界就行。

// 注意： 只按类判"是不是怪"是不够的 —— 抄 Kindred 的坑 #1：
// 他们的 AI 同伴被狼咬死，因为 Senses 只把 HostileEntity 当威胁，
// 而狼在 1.21 里不是 Monster。所以判据是三条并集：
//   1 是 Monster 类  2 名字像怪（mod 生物兜底）  3 正瞄着我
var MCB_HOSTILE_RE = /zombie|skeleton|creeper|spider|witch|slime|phantom|drowned|husk|stray|pillager|vindicator|ravager|blaze|ghast|magma|endermite|silverfish|guardian|shulker|wither|hoglin|piglin|zoglin|warden|bogged|breeze|creaking|monster|wolf|bear|llama|hog|piranha|revenant|necromancer|undead/i

function mcbEntityPos(e) {
  try {
    return [Math.round(Number(e.x) * 10) / 10, Math.round(Number(e.y) * 10) / 10, Math.round(Number(e.z) * 10) / 10]
  } catch (err) {
    return null
  }
}

function mcbEntityName(e) {
  if (e === null || e === undefined) return '?'
  var cands = []
  try { if (e.name !== undefined && e.name !== null) cands.push(e.name.string) } catch (e1) { }
  try { if (e.displayName !== undefined && e.displayName !== null) cands.push(e.displayName.string) } catch (e2) { }
  try { cands.push(e.getName().getString()) } catch (e3) { }
  try { cands.push(e.type) } catch (e4) { }
  for (var i = 0; i < cands.length; i++) {
    if (mcbIsUsableName(cands[i])) return String(cands[i])
  }
  return '?'
}

function mcbEntityHealth(e) {
  try { return mcbNumOrNull(e.health) } catch (e1) { }
  try { return mcbNumOrNull(e.getHealth()) } catch (e2) { }
  return null
}

function mcbThreats(player, radius) {
  var out = { err: [], threats: [], via: [], radius: radius }
  var level = null
  try {
    level = player.level
  } catch (e0) {
    out.err.push('level: ' + e0)
    return out
  }

  var px = Number(player.x), py = Number(player.y), pz = Number(player.z)
  out.origin = [Math.round(px * 10) / 10, Math.round(py * 10) / 10, Math.round(pz * 10) / 10]

  var $B = Java.loadClass('net.minecraft.world.phys.AABB')
  var box = new $B(px - radius, py - radius, pz - radius, px + radius, py + radius, pz + radius)

  var seen = {}      // uuid -> entry（两个来源合起来去重）
  var order = []     // 插入顺序；最后按距离排

  function collect(list, isMonster) {
    if (list === null || list === undefined) return
    var n = Number(list.size())
    for (var i = 0; i < n; i++) {
      var e = list.get(i)
      var u
      try { u = String(e.uuid) } catch (eU) { continue }
      if (u === String(player.uuid)) continue
      if (seen[u] !== undefined) {
        if (isMonster) seen[u]._m = true
        continue
      }
      var pos = mcbEntityPos(e)
      if (pos === null) continue
      var tn = ''
      try { tn = String(e.type) } catch (eT) { }
      // 它是不是正瞄着白？抄 Kindred 坑 #1 的修复。
      var targeting = false
      try {
        var tg = null
        try { tg = e.target } catch (eG1) { }
        if (tg === null || tg === undefined) {
          try { tg = e.getTarget() } catch (eG2) { }
        }
        if (tg !== null && tg !== undefined) {
          targeting = (String(tg.uuid) === String(player.uuid))
        }
      } catch (eG) { }
      var ent = {
        name: mcbEntityName(e), type: tn, pos: pos,
        hp: mcbEntityHealth(e), targeting: targeting, _m: isMonster
      }
      seen[u] = ent
      order.push(ent)
    }
  }

  // 1 精确：Monster 类 —— 原版和大部分 mod 的怪都继承它
  try {
    var $M = Java.loadClass('net.minecraft.world.entity.monster.Monster')
    collect(level.getEntitiesOfClass($M, box), true)
    out.via.push('Monster')
  } catch (e1) {
    out.err.push('Monster: ' + e1)
  }

  // 2 兜底：所有实体里"正瞄着我"的。
  //    注意：注意： 这一步不能省 —— 抄 Kindred 的坑 #1：他们的同伴被狼咬死，
  //    就是因为只把 HostileEntity 当威胁。狼（Wolf）在 1.21 里不是 Monster，
  //    咬人时根本不会出现在1的结果里。"谁在盯着我"比"它是什么类"更重要。
  try {
    collect(level.getEntities(player, box), false)
    out.via.push('all-entities')
  } catch (e2) {
    out.err.push('all: ' + e2)
  }

  if (order.length === 0 && out.err.length >= 2) {
    out.err.push('没有可用的实体查询接口')
    return out
  }

  var arr = []
  for (var k = 0; k < order.length; k++) {
    var en = order[k]
    var ep = en.pos
    en.dist = Math.round(Math.sqrt(
      (ep[0] - px) * (ep[0] - px) + (ep[1] - py) * (ep[1] - py) + (ep[2] - pz) * (ep[2] - pz)
    ) * 10) / 10
    // 敌对 = Monster 类 或 名字像怪 或 正瞄着我
    en.hostile = !!en._m || en.targeting || MCB_HOSTILE_RE.test(en.type)
    delete en._m
    arr.push(en)
  }

  arr.sort(function (a, b) { return a.dist - b.dist })
  out.count = arr.length
  out.threats = arr.slice(0, 20)
  out.hostiles = arr.filter(function (x) { return x.hostile }).slice(0, 10)
  return out
}

// 准星指着的那只实体 —— 只读，不动手。
//
// 为什么要它：mc_attack 是准星制的（客户端 player.pick），而"那只是谁的宠物 / 有没有名字 /
// 是不是村民"只有服务端答得出来。插件侧要拿它过权限闸（docs\22 §4）：
// 不然她一刀下去可能砍死主人驯的狼。自动反击（reflex）按设计不过闸，走的是 attackAt。
//
// 手法：沿视线采样（和 mcbRayBlock 同一套眼睛/视线），在采样点上找实体；
// 撞到真遮挡的方块就停 —— 别隔着墙报"看得见"。
// 注意： 这是近似，不是权威：原版权威是客户端那份 hitResult（见 mcbLookAt 的注释）。
function mcbAttackTarget(player) {
  var out = { hit: false, err: [], via: [] }
  var level = null
  try { level = player.level } catch (e0) { out.err.push('level: ' + e0); return out }
  if (level === null || level === undefined) { out.err.push('level 为空'); return out }

  var reach = 3.0
  try { reach = Number(player.entityInteractionRange) } catch (eR) { }
  if (isNaN(reach) || reach <= 0) reach = 3.0
  out.reach = mcbR1(reach)

  var eye = null, look = null
  try { eye = player.getEyePosition(1.0) } catch (e1) {
    try { eye = player.getEyePosition() } catch (e2) { out.err.push('eye: ' + e2) }
  }
  try { look = player.getViewVector(1.0) } catch (e3) {
    try { look = player.getLookAngle() } catch (e4) { out.err.push('look: ' + e4) }
  }
  if (eye === null || eye === undefined || look === null || look === undefined) {
    out.err.push('拿不到眼睛位置或视线方向')
    return out
  }
  var ex = Number(eye.x), ey = Number(eye.y), ez = Number(eye.z)
  var lx = Number(look.x), ly = Number(look.y), lz = Number(look.z)

  var $AABB = null
  try { $AABB = Java.loadClass('net.minecraft.world.phys.AABB') } catch (eB) {
    out.err.push('AABB: ' + eB); return out
  }
  var $BP = null
  try { $BP = Java.loadClass('net.minecraft.core.BlockPos') } catch (eB2) { out.err.push('BlockPos: ' + eB2) }

  var STEP = 0.1
  var t = 0.0
  var steps = 0
  while (t <= reach && steps < 200) {
    steps++
    var ts = t + STEP * 0.5
    var sx = ex + lx * ts, sy = ey + ly * ts, sz = ez + lz * ts

    // 方块挡住视线就停 —— 隔着墙不该"看得见"
    if ($BP !== null) {
      try {
        var st = level.getBlockState(new $BP(Math.floor(sx), Math.floor(sy), Math.floor(sz)))
        if (mcbBlockId(st).indexOf('air') < 0) {
          var occl = true
          try { occl = !!st.canOcclude() } catch (eO) { occl = true }
          if (occl) { out.blockedAt = [Math.floor(sx), Math.floor(sy), Math.floor(sz)]; break }
        }
      } catch (eS) { }
    }

    var list = null
    try {
      list = level.getEntities(player, new $AABB(sx - 0.35, sy - 0.35, sz - 0.35,
                                                sx + 0.35, sy + 0.35, sz + 0.35))
    } catch (eL) { out.err.push('getEntities: ' + eL); break }

    var n = Number(list.size())
    for (var i = 0; i < n; i++) {
      var e = list.get(i)
      try { if (String(e.uuid) === String(player.uuid)) continue } catch (eU) { }
      // 判定箱读得到就用它收紧；读不到就算命中（宁可多问一次主人，也别放过）
      try {
        var bb = e.boundingBox
        if (bb !== null && bb !== undefined) {
          var inBox = (sx >= Number(bb.minX) && sx <= Number(bb.maxX)
                    && sy >= Number(bb.minY) && sy <= Number(bb.maxY)
                    && sz >= Number(bb.minZ) && sz <= Number(bb.maxZ))
          if (!inBox) continue
        }
      } catch (eBB) { }

      out.hit = true
      out.dist = mcbR1(ts)
      try { out.uuid = String(e.uuid) } catch (eU2) { out.err.push('uuid: ' + eU2) }
      try { out.type = String(e.type) } catch (eT2) { out.err.push('type: ' + eT2) }
      try { out.name = mcbEntityName(e) } catch (eN2) { }

      // 有没有名字 —— hasCustomName 所有实体都有，读不到就是读不到
      try { out.custom = !!e.hasCustomName() } catch (eC1) {
        try { out.custom = (e.customName !== null && e.customName !== undefined) } catch (eC2) {
          out.custom = null
          out.err.push('custom: 读不到')
        }
      }

      // 有没有主人 —— 注意： 关键是把"不是可驯服的实体"和"读不到"分开。
      //   getOwnerUUID 只存在于 OwnableEntity 上；不存在时取属性会抛，
      //   于是 typeof 判一下就知道是"没有这个概念"（= 没有主人，确定）还是"读不到"。
      var fnOwner = null
      try { fnOwner = e.getOwnerUUID } catch (eF) { }
      var isFn = false
      try { isFn = (typeof fnOwner === 'function') } catch (eF2) { }
      if (isFn) {
        try {
          var o = fnOwner.call(e)
          out.ownerVia = 'getOwnerUUID'
          out.owner = (o === null || o === undefined) ? null : String(o)
        } catch (eO1) {
          try {
            out.owner = (e.ownerUUID === null || e.ownerUUID === undefined) ? null : String(e.ownerUUID)
            out.ownerVia = 'ownerUUID'
          } catch (eO2) { out.owner = null; out.ownerVia = ''; out.err.push('owner: 读不到') }
        }
      } else {
        out.owner = null
        out.ownerVia = 'absent'          // 确定：这类实体没有主人这个概念
      }

      // 是不是村民 —— 先试 instanceof，不行退回按注册名（对 mod 村民不认，是已知缺口）
      var $V = null
      try { $V = Java.loadClass('net.minecraft.world.entity.npc.AbstractVillager') } catch (eV0) { }
      if ($V !== null) {
        try {
          out.villager = (e instanceof $V)
          out.villagerVia = 'instanceof'
        } catch (eV1) { out.villager = null; out.villagerVia = '' }
      } else {
        out.villager = null
        out.villagerVia = ''
      }
      if (out.villager === null) {
        var tid = String(out.type || '')
        if (tid.indexOf(':') >= 0) {
          out.villager = (tid === 'minecraft:villager' || tid === 'minecraft:wandering_trader')
          out.villagerVia = 'type-id'
        }
      }
      return out
    }
    t += STEP
  }
  if (!out.hit) out.err.push('准星没指着实体')
  return out
}

// --- 准星注视（服务端射线）------------------------------------------------
//
// "我正看着什么" —— 最自然的感知入口。
//
// 注意： 为什么走服务端，不读客户端的 mc.hitResult：
//    客户端那份在低帧率下经常 MISS（mcbridge.js:546 的原话），
//    而且改客户端脚本要重启那个 ~110 秒的进程。服务端这份用的是
//    服务端收到的朝向（约一 tick 延迟）—— 是近似，但零成本、当场能测。
//
//    顺带纠正一个容易搞反的事实：原版真正的权威其实是客户端的 hitResult
//    （ServerboundUseItemOnPacket 就是客户端把 BlockHitResult 发上来、
//    服务端只校验距离）。所以这里给的是近似，不是权威。
//    哪天真和"她看见的"对不上，再补客户端那条 —— mcbridge.js:586-595 代码现成。

// 从"上一格  ->  命中格"的位移反推命中面。
// 规则：命中面永远朝着上一格（prev 在 +Z 就是南面）。
function mcbFaceFromDelta(dx, dy, dz) {
  if (dy === 1) return 'up'
  if (dy === -1) return 'down'
  if (dz === 1) return 'south'
  if (dz === -1) return 'north'
  if (dx === 1) return 'east'
  if (dx === -1) return 'west'
  return null
}

// 视线射线 —— 自己走体素，不走 player.pick()。
//
// 注意： 为什么不用原版 pick：它用 ClipContext.Block.OUTLINE，
//    玻璃、玻璃板、树叶、铁栏杆这些只要有轮廓形状的都会挡住 ——
//    于是"隔着玻璃看东西"永远只答得出玻璃。
//
// 这里改成只认"真遮挡"的方块：state.canOcclude()。
//    玻璃 false（看得穿）· 石头 true（挡死）。
//    这是视觉语义，跟碰撞箱（getCollisionShape，mcpfabric 用的那个）不是一回事。
//
// 返回两个东西：
//   seen     = 第一个非空气方块（可能就是玻璃）
//   occluder = 第一个真挡住视线的方块（视线实质上停在这儿）
// 两个不一样时，就是"她隔着某样东西看着另一样东西"。
function mcbRayBlock(level, ex, ey, ez, lx, ly, lz, maxDist) {
  var out = { seen: null, occluder: null, steps: 0 }
  var $BP = null
  try { $BP = Java.loadClass('net.minecraft.core.BlockPos') } catch (e0) {
    out.err = 'BlockPos: ' + e0
    return out
  }
  var STEP = 0.05                 // 4.5 格 ≈ 90 步；一次调用而已，不用省
  var prev = null
  var t = 0.0
  while (t <= maxDist && out.steps < 3000) {
    out.steps++
    // 注意：注意： 采样点取步长中点，不能用步长起点（2026-10-10 定位）。
    //    射线正擦着方块边界走时，起点采样会落到边界下面那一格：
    //    实测 hit=[-4.0, 90.0, -7.0]（正好是玻璃的底面）——
    //    原版解析求交把边界判给"正在进入"的那格（y=90 的玻璃），
    //    而 Math.floor(89.9999) 判给 y=89 的安山岩，于是射线根本碰不到玻璃，
    //    表现成"自研射线和原版 pick 对不上"。
    //    取中点 = 偏向"已经进入"的那一侧，和原版语义一致。
    var ts = t + STEP * 0.5
    var bx = Math.floor(ex + lx * ts)
    var by = Math.floor(ey + ly * ts)
    var bz = Math.floor(ez + lz * ts)
    if (prev === null || bx !== prev[0] || by !== prev[1] || bz !== prev[2]) {
      var id = null
      var occl = true             // 读不出来就当它挡（退回原版行为，宁可保守）
      try {
        var st = level.getBlockState(new $BP(bx, by, bz))
        id = mcbBlockId(st)
        try {
          occl = !!st.canOcclude()
        } catch (eO) {
          occl = true
          // 记一次就够 —— canOcclude 要是不存在，through 永远不出现，
          // 光看结果是发现不了的（会安静地退化成原版 pick 的行为）
          if (out.occlErr === undefined) out.occlErr = String(eO)
        }
      } catch (eR) { id = null }
      if (id !== null && id.indexOf('air') < 0) {
        var here = {
          id: id,
          pos: [bx, by, bz],
          face: prev === null ? null : mcbFaceFromDelta(prev[0] - bx, prev[1] - by, prev[2] - bz),
          dist: mcbR1(ts),
          occl: occl                       // 诊断：这一格到底挡不挡视线
        }
        if (out.seen === null) out.seen = here
        if (occl) { out.occluder = here; break }
      }
      prev = [bx, by, bz]
    }
    t += STEP
  }
  return out
}

// 读一个 BlockPos 的 x/y/z 整数坐标。
//
// 注意：注意： 2026-10-10 实测：BlockPos 在 Rhino 下有两种形状，都得兜住 ——
//    · be.getBlockPos() 回的是正常 BlockPos  ->  x/y/z 是方法
//      （getX() …），字段访问给的是函数对象，Number() 出来是 NaN
//    · player.pick().getBlockPos() 回的是 MutableBlockPos  ->  那里
//      x/y/z 是字段（写 bp.x() 直接抛
//      TypeError: Cannot call property x in object MutableBlockPos{...}）
//    所以：字段先试，方法兜底，两边都不给就返回 null。
function mcbPosXYZ(p) {
  var x = null, y = null, z = null
  try { x = Number(p.x) } catch (e1) { }
  try { y = Number(p.y) } catch (e2) { }
  try { z = Number(p.z) } catch (e3) { }
  if (x === null || isNaN(x)) { try { x = Number(p.getX()) } catch (e4) { } }
  if (y === null || isNaN(y)) { try { y = Number(p.getY()) } catch (e5) { } }
  if (z === null || isNaN(z)) { try { z = Number(p.getZ()) } catch (e6) { } }
  if (x === null || y === null || z === null) return null
  if (isNaN(x) || isNaN(y) || isNaN(z)) return null
  return [Math.floor(x), Math.floor(y), Math.floor(z)]
}

// 保留一位小数。和 mcbNumOrNull 的区别：这个不把 null 变 NaN 就丢，而是给 null。
function mcbR1(v) {
  var n = Number(v)
  return isNaN(n) ? null : Math.round(n * 10) / 10
}

// yaw  ->  八向罗盘词。她要自己讲"我朝西北看"就得有这个词，
// 不然只能报一个数字，那是程序在说话。
// 注意： MC 的 yaw 约定：0=南(+Z) · 90=西(-X) · 180=北(-Z) · 270=东(+X)
function mcbCompass(yaw) {
  if (isNaN(yaw)) return null
  var names = ['south', 'southwest', 'west', 'northwest', 'north', 'northeast', 'east', 'southeast']
  var d = ((yaw % 360) + 360) % 360
  return names[Math.round(d / 45) % 8]
}

// 朝向（yaw/pitch），带三级兜底。
//
// 注意：注意： 2026-10-10 实测踩到的两个坑，别再犯：
//   1 player.yRot 在 Rhino 下不抛异常，返回 undefined ——
//      所以 try { player.yRot } catch { 试 getYRot() } 的兜底永远不触发，
//      Number(undefined) 静默变 NaN。必须显式判 NaN。
//   2 别用动态属性名取 Java 方法（player['getYRot']()）——
//      实测 getYRot() 走这个方法拿不到数。写成字面量调用才有用。
//
// 三级：字段  ->  getter  ->  从视线向量反推。
// 最后那级是保底 —— getViewVector 已经实测可用，所以这条路一定通。
//   MC 约定：view = ( -sin(yaw)cos(pitch), -sin(pitch), cos(yaw)cos(pitch) )
function mcbYawPitch(player, look) {
  var yaw = null, pitch = null, via = 'field'

  try { var v1 = Number(player.yRot); if (!isNaN(v1)) yaw = v1 } catch (e1) { }
  try { var v2 = Number(player.xRot); if (!isNaN(v2)) pitch = v2 } catch (e2) { }

  if (yaw === null || pitch === null) {
    via = 'getter'
    if (yaw === null) { try { var v3 = Number(player.getYRot()); if (!isNaN(v3)) yaw = v3 } catch (e3) { } }
    if (pitch === null) { try { var v4 = Number(player.getXRot()); if (!isNaN(v4)) pitch = v4 } catch (e4) { } }
  }

  if ((yaw === null || pitch === null) && look !== null && look !== undefined) {
    via = 'look'
    var lx = Number(look.x), ly = Number(look.y), lz = Number(look.z)
    if (!isNaN(lx) && !isNaN(ly) && !isNaN(lz)) {
      if (pitch === null) {
        var cl = Math.max(-1, Math.min(1, ly))
        pitch = -Math.asin(cl) * 180 / Math.PI
      }
      if (yaw === null) yaw = Math.atan2(-lx, lz) * 180 / Math.PI
    }
  }

  return {
    yaw: (yaw === null || isNaN(yaw)) ? null : yaw,
    pitch: (pitch === null || isNaN(pitch)) ? null : pitch,
    via: via
  }
}

function mcbLookAt(player, reach) {
  var out = { err: [], reach: reach }
  var level = null
  try { level = player.level } catch (e0) { out.err.push('level: ' + e0); return out }
  if (level === null || level === undefined) { out.err.push('level 为空'); return out }

  var eye = null, look = null
  try { eye = player.getEyePosition(1.0) } catch (e1) {
    try { eye = player.getEyePosition() } catch (e2) { out.err.push('eye: ' + e2) }
  }
  try { look = player.getViewVector(1.0) } catch (e3) {
    try { look = player.getLookAngle() } catch (e4) { out.err.push('look: ' + e4) }
  }
  if (eye === null || eye === undefined || look === null || look === undefined) {
    out.err.push('拿不到眼睛位置或视线方向')
    return out
  }
  var ex = Number(eye.x), ey = Number(eye.y), ez = Number(eye.z)
  var lx = Number(look.x), ly = Number(look.y), lz = Number(look.z)
  out.eye = [mcbR1(ex), mcbR1(ey), mcbR1(ez)]
  out.look = [mcbR1(lx), mcbR1(ly), mcbR1(lz)]

  // 朝向 —— 放在 look 之后算，因为它要拿 look 当兜底
  var ang = mcbYawPitch(player, look)
  out.rotVia = ang.via
  if (ang.yaw !== null) {
    out.yaw = mcbR1(ang.yaw)
    out.facing = mcbCompass(ang.yaw)
  }
  if (ang.pitch !== null) out.pitch = mcbR1(ang.pitch)
  if (ang.yaw === null) out.err.push('拿不到 yaw（字段/ getter / 视线反推 三条路都不通）')

  // 1 方块 —— 自研体素射线（能看穿玻璃，理由见 mcbRayBlock 的注释）
  var blockDist = reach
  out.block = null
  try {
    var ray = mcbRayBlock(level, ex, ey, ez, lx, ly, lz, reach)
    if (ray.err) out.err.push('ray: ' + ray.err)
    out.raySteps = ray.steps
    // 视线实质停在 occluder；一路没遮挡就报第一个看到的东西
    var hitBlock = ray.occluder !== null ? ray.occluder : ray.seen
    if (hitBlock !== null) {
      out.block = hitBlock
      blockDist = hitBlock.dist
      // 中间隔着东西（玻璃/树叶…） ->  报出来，她才知道"看得见，但隔了一层"
      if (ray.occluder !== null && ray.seen !== null && ray.seen !== ray.occluder) {
        out.through = ray.seen
      }
    } else if (ray.seen !== null) {
      // 射程内一个遮挡方块都没有，但看到过东西（穿过去了）—— 别静默丢掉
      out.block = ray.seen
      out.blockDist = ray.seen.dist
      out.onlySeen = true
    }
    // 诊断：原版 pick 怎么答的。两个不一致，就说明玻璃那类方块真的在起作用
    try {
      var vp = player.pick(reach, 1.0, false)
      if (vp !== null && vp !== undefined) {
        var vt = String(vp.getType())
        var vpv = { type: vt }
        if (vt === 'BLOCK') {
          var vbp = vp.getBlockPos()
          vpv.id = mcbBlockId(level.getBlockState(vbp))
          vpv.pos = mcbPosXYZ(vbp)
          // 注意： 命中点 + 算出来的距离 —— 有了它才能判断
          //    "两条射线是不是真的打在同一格上"（2026-10-10 遇到过一次对不上）
          try {
            var vloc = vp.getLocation()
            vpv.hit = [mcbR1(vloc.x), mcbR1(vloc.y), mcbR1(vloc.z)]
            vpv.dist = mcbR1(Math.sqrt(
              Math.pow(Number(vloc.x) - ex, 2) + Math.pow(Number(vloc.y) - ey, 2) + Math.pow(Number(vloc.z) - ez, 2)
            ))
          } catch (eVL) { }
        }
        out.pickVanilla = vpv
      }
    } catch (eVP) { out.pickVanilla = { err: String(eVP) } }
  } catch (e5) { out.err.push('ray: ' + e5) }

  // 2 实体 —— 服务端没有跨版本稳定的"射线 vs 实体"求交（ProjectileUtil 的签名
  //    每个版本都在变），自己做锥形命中：
  //    把实体中心投影到视线上，投影落在射程内、垂距小于它自己的碰撞箱半宽  ->  算看着它。
  out.entity = null
  try {
    var $AABB = Java.loadClass('net.minecraft.world.phys.AABB')
    var tx = ex + lx * reach, ty = ey + ly * reach, tz = ez + lz * reach
    var box = new $AABB(
      Math.min(ex, tx), Math.min(ey, ty), Math.min(ez, tz),
      Math.max(ex, tx), Math.max(ey, ty), Math.max(ez, tz)
    ).inflate(1.5)

    var list = level.getEntities(player, box)
    var n = Number(list.size())
    var best = null, bestT = 0
    for (var i = 0; i < n; i++) {
      var e = list.get(i)
      try { if (String(e.uuid) === String(player.uuid)) continue } catch (eU) { continue }
      var cp = mcbEntityPos(e)
      if (cp === null) continue
      var dx = cp[0] - ex, dy = cp[1] - ey, dz = cp[2] - ez
      var t = dx * lx + dy * ly + dz * lz              // 在视线方向上的投影长度
      if (t < -0.5 || t > reach + 1.0) continue         // 在身后 / 太远
      var qx = ex + lx * t, qy = ey + ly * t, qz = ez + lz * t
      var perp = Math.sqrt(
        Math.pow(cp[0] - qx, 2) + Math.pow(cp[1] - qy, 2) + Math.pow(cp[2] - qz, 2)
      )
      // 命中半径 = 碰撞箱水平半宽 + 一点余量；拿不到就按 0.6 算
      var rad = 0.6
      try {
        var bb = e.getBoundingBox()
        rad = Math.max(Number(bb.getXsize()), Number(bb.getZsize())) / 2
        if (isNaN(rad) || rad <= 0) rad = 0.6
      } catch (eB) { }
      if (perp > rad + 0.25) continue
      if (best === null || t < bestT) { best = e; bestT = t }
    }
    if (best !== null && bestT <= blockDist) {
      // 只有实体比方块近才算"看着她"。否则她在看那面墙，
      // 锥形测试没有遮挡概念，这一步就是拿来补遮挡的。
      var btype = ''
      try { btype = String(best.type) } catch (eT) { }
      out.entity = {
        name: mcbEntityName(best), type: btype, pos: mcbEntityPos(best),
        hp: mcbEntityHealth(best), dist: mcbR1(bestT)
      }
    } else if (best !== null) {
      out.entityOccluded = true
    }
  } catch (e6) { out.err.push('entities: ' + e6) }

  if (out.entity !== null) {
    out.target = 'entity'
    out.dist = out.entity.dist
  } else if (out.block !== null) {
    out.target = 'block'
    out.dist = out.block.dist
  } else {
    out.target = 'none'
    out.dist = null
  }
  return out
}

// --- 配方查询（合成规划要用）------------------------------------------------
//
// 注意： 不要自己编配方表 —— 服务端的 RecipeManager 认识所有 mod 的配方，
//    种类、数量、摆法、要不要工作台，全在里面。抄它就不会漏 mod。
//
// 索引按"产物物品 id"建，懒加载一次（脚本重载后全局重置，正好重建）。

var mcbRecipeIdx = null

function mcbItemId(stack) {
  try {
    var $R = Java.loadClass('net.minecraft.core.registries.BuiltInRegistries')
    var rl = $R.ITEM.getKey(stack.getItem())
    return String(rl)
  } catch (e) {
    return null
  }
}

// 一个 Ingredient 有哪些候选（比如"任意木板"会有 11 种）
//
// 注意：注意： NeoForge 1.21.1 的 Ingredient 没有 getItems() —— 实测报
//     Cannot find function getItems。能用的三条路（按优先级）：
//       1 ing.values —— Ingredient$Value[]，每个 Value 有 getItems()（正统，优先）
//       2 ing.getValues() / ing.getStacks() —— KubeJS 自己加的
//       3 ing.stacks —— KubeJS 的 ItemStackSet（可迭代）
//     另外 Java 数组不能调 .iterator()（抛 InternalError，被 catch 吞掉，
//     看着像"取不到"）—— 数组用下标 + .length。
function mcbIngredientIds(ing) {
  var out = []
  if (ing === null || ing === undefined) { out.push('#NULL'); return out }
  var errs = []

  function addStack(st) {
    var id = mcbItemId(st)
    if (id !== null && out.indexOf(id) < 0) out.push(id)
  }
  function addArr(arr) {
    try {
      var n = Number(arr.length)
      for (var i = 0; i < n; i++) addStack(arr[i])
    } catch (e) { errs.push('addArr:' + e) }
  }

  // 1 正统：values[i].getItems()
  try {
    var vals = ing.values
    if (vals !== null && vals !== undefined) {
      for (var i = 0; i < Number(vals.length); i++) {
        try { addArr(vals[i].getItems()) } catch (e1) { errs.push('v.getItems:' + e1) }
      }
    }
  } catch (e0) { errs.push('values:' + e0) }

  // 2 KubeJS 的 getValues() / getStacks()
  if (out.length === 0) {
    var cands = []
    try { var a = ing.getValues(); if (a) cands.push(a) } catch (e2) { errs.push('getValues:' + e2) }
    try { var b = ing.getStacks(); if (b) cands.push(b) } catch (e3) { errs.push('getStacks:' + e3) }
    for (var c = 0; c < cands.length && out.length === 0; c++) {
      var v = cands[c]
      try { addArr(v) } catch (e4) { }
      if (out.length === 0) {
        try {
          var it = v.iterator()
          while (it.hasNext()) addStack(it.next())
        } catch (e5) { errs.push('iter:' + e5) }
      }
    }
  }

  // 3 ItemStackSet
  if (out.length === 0) {
    try {
      var s = ing.stacks
      if (s !== null && s !== undefined) {
        var it2 = s.iterator()
        while (it2.hasNext()) addStack(it2.next())
      }
    } catch (e6) { errs.push('stacks:' + e6) }
  }

  if (out.length === 0) out.push('#DBG ' + errs.join(' | '))
  return out
}

// 一条配方  ->  给下游看的紧凑结构
//
// kind 决定下游怎么"摆料 + 取产物"（见插件侧 mcb/containers.py 的容器模板表）：
//   crafting / smelting / stonecutting / smithing
function mcbRecipeInfo(recipe, holder, kind) {
  var info = { id: null, kind: kind || 'crafting', type: 'unknown',
    w: 0, h: 0, grid: [], shapeless: [], out: null, outN: 1 }
  try { info.id = String(holder.id()) } catch (e0) {
    try { info.id = String(recipe.getId()) } catch (e0b) { }
  }

  // 产物
  try {
    var res = recipe.getResultItem(null)
    info.out = mcbItemId(res)
    try { info.outN = Number(res.getCount()) } catch (eN) { info.outN = 1 }
  } catch (e1) { }

  // 形状：ShapedRecipe 有 getWidth/getHeight/getIngredients
  try {
    var w = Number(recipe.getWidth())
    var h = Number(recipe.getHeight())
    info.w = w
    info.h = h
    info.type = 'shaped'
    var ings = recipe.getIngredients()
    for (var i = 0; i < w * h; i++) {
      info.grid.push(mcbIngredientIds(ings.get(i)))
    }
  } catch (e2) {
    // 不是 shaped —— 试试 shapeless
    try {
      var ings2 = recipe.getIngredients()
      info.type = 'shapeless'
      var n = Number(ings2.size())
      for (var k = 0; k < n; k++) {
        info.shapeless.push(mcbIngredientIds(ings2.get(k)))
      }
      info.w = n
      info.h = 1
    } catch (e3) {
      info.err = String(e3)
    }
  }

  // 要不要工作台：2x2 摆得下就不用
  info.needsTable = !(info.type === 'shaped' ? (info.w <= 2 && info.h <= 2) : (info.shapeless.length <= 4))
  return info
}

function mcbRecipeIndex(server) {
  if (mcbRecipeIdx !== null) return mcbRecipeIdx
  var idx = {}
  var errs = []
  var rm = null
  try { rm = server.getRecipeManager() } catch (e) { errs.push('getRecipeManager: ' + e) }
  if (rm === null) { try { rm = server.recipeManager } catch (e2) { errs.push('recipeManager: ' + e2) } }
  if (rm === null) { idx.__err = errs; mcbRecipeIdx = idx; return idx }

  var $RT = Java.loadClass('net.minecraft.world.item.crafting.RecipeType')
  var list = null
  var ways = [
    ['getAllRecipesFor', function () { return rm.getAllRecipesFor($RT.CRAFTING) }],
    ['recipes.get', function () { return rm.recipes.get($RT.CRAFTING) }]
  ]
  for (var wi = 0; wi < ways.length; wi++) {
    try {
      var r = ways[wi][1]()
      if (r !== null && r !== undefined) { list = r; errs.push('via=' + ways[wi][0]); break }
    } catch (e3) { errs.push(ways[wi][0] + ': ' + e3) }
  }
  if (list === null) { idx.__err = errs; mcbRecipeIdx = idx; return idx }

  var n = 0

  // 把一类配方灌进索引。加新容器 = 在这里多一行，别写新函数。
  function ingest(recipes, kind) {
    try {
      var it0 = recipes.iterator()
      while (it0.hasNext()) {
        var holder0 = it0.next()
        var rec0 = null
        try { rec0 = holder0.value() } catch (e4) { rec0 = holder0 }
        if (rec0 === null || rec0 === undefined) continue
        var info0 = mcbRecipeInfo(rec0, holder0, kind)
        if (info0.out === null) continue
        if (idx[info0.out] === undefined) idx[info0.out] = []
        idx[info0.out].push(info0)
        n++
      }
    } catch (e5) { errs.push(kind + ' 遍历: ' + e5) }
  }

  ingest(list, 'crafting')

  // 注意： 非合成的配方也要索引 —— 白要"把铁矿烧成锭"，光有 crafting 是不够的。
  //    冶炼/切石的产物种类远少于合成，代价很小。
  try { ingest(rm.getAllRecipesFor($RT.SMELTING), 'smelting') }
  catch (eS) { errs.push('SMELTING: ' + eS) }
  try { ingest(rm.getAllRecipesFor($RT.STONECUTTING), 'stonecutting') }
  catch (eSC) { errs.push('STONECUTTING: ' + eSC) }
  try { ingest(rm.getAllRecipesFor($RT.SMITHING), 'smithing') }
  catch (eSM) { errs.push('SMITHING: ' + eSM) }

  errs.push('count=' + n)
  idx.__err = errs
  idx.__count = n
  mcbRecipeIdx = idx
  return idx
}

function mcbRecipe(server, want) {
  var out = { err: [], want: want, recipes: [] }
  if (!want) { out.err.push('没给物品名'); return out }
  var id = String(want)
  if (id.indexOf(':') < 0) id = 'minecraft:' + id
  var idx = mcbRecipeIndex(server)
  out.err = idx.__err || []
  out.total = idx.__count || 0
  var got = idx[id]
  if (got === undefined) {
    // 宽容一点：也给一下"以这个名字结尾"的（mod 物品常有前缀）
    var keys = Object.keys(idx)
    var like = []
    for (var i = 0; i < keys.length; i++) {
      if (keys[i] !== '__err' && keys[i] !== '__count' && keys[i].indexOf(id) >= 0) like.push(keys[i])
    }
    out.suggest = like.slice(0, 10)
    if (like.length === 0) out.err.push('没有产出 ' + id + ' 的合成配方')
    return out
  }
  out.recipes = got
  return out
}

// --- 读数工具 -------------------------------------------------------------

function mcbNumOrNull(v) {
  if (v === null || v === undefined) return null
  var n = Number(v)
  return isNaN(n) ? null : Math.round(n * 100) / 100
}

// --- 环境（时间 / 天气）---------------------------------------------------
//
// 注意： 读法一律"字段  ->  判 NaN  ->  getter" —— Rhino 下字段不存在时
//    不抛异常、只给 undefined，光靠 catch 兜不住（见 mcbYawPitch 那个坑）。
//    而且 getter 必须写成字面量，obj['getX']() 这种动态调用拿不到数。
function mcbEnv(level) {
  var out = { err: [] }
  if (level === null || level === undefined) { out.err.push('level 为空'); return out }

  var tod = null
  try { var v1 = Number(level.dayTime); if (!isNaN(v1)) tod = v1 } catch (e1) { }
  if (tod === null) { try { var v2 = Number(level.getDayTime()); if (!isNaN(v2)) tod = v2 } catch (e2) { } }
  if (tod !== null) {
    var ticks = ((tod % 24000) + 24000) % 24000
    // 0 tick = 日出 = 06:00，所以先加 6 小时再取模
    var mins = Math.floor((((ticks / 1000) + 6) % 24) * 60)
    out.tod = Math.floor(ticks)
    out.day = Math.floor(tod / 24000)
    out.hhmm = ('0' + Math.floor(mins / 60)).slice(-2) + ':' + ('0' + (mins % 60)).slice(-2)
  }

  try { var b1 = level.isDay(); if (b1 !== null && b1 !== undefined) out.daytime = !!b1 } catch (e3) { }
  try { var b2 = level.isRaining(); if (b2 !== null && b2 !== undefined) out.raining = !!b2 } catch (e4) { }
  try { var b3 = level.isThundering(); if (b3 !== null && b3 !== undefined) out.thundering = !!b3 } catch (e5) { }

  // 月亮盈亏 —— 满月刷怪多，她会想知道"今晚危不危险"
  try { var p1 = Number(level.moonPhase); if (!isNaN(p1)) out.moon = p1 } catch (e6) {
    try { var p2 = Number(level.getMoonPhase()); if (!isNaN(p2)) out.moon = p2 } catch (e7) { }
  }
  return out
}

// --- 扫描能力探针（先量再设计）-----------------------------------------
//
// 注意：注意： 为什么不照抄 mcpfabric 的半径：
//    KubeJS 跑在 Rhino 上，每次 Java 调用都要过一层 JS <-> Java 包装 ——
//    纯 Java 里 100ns 的 getBlockState，在这里可能贵 10~100 倍。
//    所以"服务端能扫多大"是实测出来的，不是算出来的。
//
// 这个动作只读、不改世界，量三件事：
//   1 那几个关键 API 在 Rhino 下到底存不存在（getSections / hasOnlyAir /
//      getBlockEntities / heightmap / PalettedContainer）
//   2 每次方块读的实际微秒数 —— 用它反推"50ms 预算能扫多少格"
//   3 方块实体（箱子/熔炉那种）的迭代成本 —— POI 可能根本不用扫方块
function mcbScanProbe(player, arg) {
  var out = { err: [], ok: [], api: {}, bench: {} }
  var level = null
  try { level = player.level } catch (e0) { out.err.push('level: ' + e0); return out }

  var pcx = Math.floor(Number(player.x) / 16)
  var pcz = Math.floor(Number(player.z) / 16)
  out.chunk = [pcx, pcz]

  var chunk = null
  try { chunk = level.getChunk(pcx, pcz); out.ok.push('level.getChunk') } catch (e1) {
    out.err.push('getChunk: ' + e1)
  }
  if (chunk === null || chunk === undefined) { out.err.push('chunk 取不到 —— 后面的都测不了'); return out }

  try { out.api.chunkEmpty = !!chunk.isEmpty(); out.ok.push('chunk.isEmpty') } catch (e2) {
    out.err.push('isEmpty: ' + e2)
  }

  // 方块实体 —— POI 的便宜路子：直接拿箱子/熔炉那张表，不用扫 4096 个方块
  try {
    var bes = chunk.getBlockEntities()
    var nbe = -1
    try { nbe = Number(bes.size()) } catch (eB1) {
      try { nbe = Number(bes.length) } catch (eB2) { nbe = -1 }
    }
    out.api.blockEntityCount = nbe
    out.ok.push('chunk.getBlockEntities')
  } catch (e3) { out.err.push('getBlockEntities: ' + e3) }

  var sections = null
  try {
    sections = chunk.getSections()
    var ns = -1
    try { ns = Number(sections.length) } catch (eS1) { ns = -1 }
    out.api.sectionCount = ns
    out.ok.push('chunk.getSections')
  } catch (e4) { out.err.push('getSections: ' + e4) }

  var sec = null
  if (sections !== null) {
    try { sec = sections[Math.floor(Number(sections.length) / 2)] } catch (e5) { out.err.push('sections[mid]: ' + e5) }
  }

  if (sec !== null && sec !== undefined) {
    try { out.api.hasOnlyAir = !!sec.hasOnlyAir(); out.ok.push('section.hasOnlyAir') } catch (e6) {
      out.err.push('hasOnlyAir: ' + e6)
    }
    try { out.api.statesStr = String(sec.getStates()).substring(0, 60); out.ok.push('section.getStates') } catch (e7) {
      out.err.push('getStates: ' + e7)
    }
    // 注意： PalettedContainer.count(Counter) 要传一个 Java 函数式接口 ——
    //    Rhino 不一定会替我们把 JS 函数转成 SAM。试一下，能通就是大杀器
    //    （一次调用拿到整段 16³ 的方块统计，等于零成本）。
    try {
      var acc = 0
      sec.getStates().count(function (st) { acc = acc + 1; return true })
      out.api.paletteCountWorked = true
      out.api.paletteCounted = acc
      out.ok.push('getStates().count(fn)  ← SAM 转换成功')
    } catch (e8) {
      out.api.paletteCountWorked = false
      out.api.paletteCountErr = String(e8).substring(0, 140)
    }
  }

  // heightmap
  try {
    var $HM = Java.loadClass('net.minecraft.world.level.levelgen.Heightmap$Types')
    var h = Number(chunk.getHeight($HM.MOTION_BLOCKING, 8, 8))
    out.api.heightMotionBlocking = h
    out.ok.push('chunk.getHeight(MOTION_BLOCKING)')
  } catch (e9) { out.err.push('getHeight: ' + e9) }

  // ---- 基准：每次方块读多贵 ----
  // 注意： N 要够大 —— 太小会量到解释器/JIT 预热阶段，把开销高估好几倍。
  //    mcb scanprobe 200000 可以手动加码。
  var N = 100000
  try {
    var nArg = parseInt(arg, 10)
    if (!isNaN(nArg) && nArg > 0) N = Math.min(nArg, 500000)
  } catch (eN) { }
  if (sec !== null && sec !== undefined) {
    var t0 = mcbNowMs()
    var sink = 0
    for (var i = 0; i < N; i++) {
      var st = null
      try { st = sec.getBlockState(i & 15, (i >> 4) & 15, (i >> 8) & 15) } catch (eB3) { break }
      sink = sink + (st === null ? 0 : 1)
    }
    var t1 = mcbNowMs()
    out.bench.sectionGetBlockState = {
      n: N, ms: Math.round((t1 - t0) * 10) / 10, nonzero: sink,
      perCallUs: Math.round((t1 - t0) * 1000 / N * 100) / 100
    }
  }

  var $BP2 = null
  try { $BP2 = Java.loadClass('net.minecraft.core.BlockPos') } catch (eBP) { out.err.push('BlockPos: ' + eBP) }
  if ($BP2 !== null) {
    var px = Math.floor(Number(player.x)), py = Math.floor(Number(player.y)), pz = Math.floor(Number(player.z))
    var t2 = mcbNowMs()
    var sink2 = 0
    for (var j = 0; j < N; j++) {
      var st2 = null
      try { st2 = level.getBlockState(new $BP2(px + (j & 15), py + ((j >> 4) & 7), pz + ((j >> 7) & 15))) } catch (eB4) { break }
      sink2 = sink2 + (st2 === null ? 0 : 1)
    }
    var t3 = mcbNowMs()
    out.bench.levelGetBlockState = {
      n: N, ms: Math.round((t3 - t2) * 10) / 10, nonzero: sink2,
      perCallUs: Math.round((t3 - t2) * 1000 / N * 100) / 100
    }
  }

  // ---- 反推：50ms 预算能扫多少格 ----
  var budget = {}
  var b1 = out.bench.sectionGetBlockState
  if (b1 && b1.perCallUs > 0) {
    budget.section_fullCube_per50ms = Math.floor(50000 / b1.perCallUs)
    // 每列只读地表上下 7 格的话，能覆盖多少列
    budget.section_columns_per50ms = Math.floor(50000 / (b1.perCallUs * 7))
  }
  var b2 = out.bench.levelGetBlockState
  if (b2 && b2.perCallUs > 0) {
    budget.level_fullCube_per50ms = Math.floor(50000 / b2.perCallUs)
  }
  out.budget = budget
  return out
}

// --- 周边概览（T1）--------------------------------------------------------
//
// "我周围有什么" —— 大半径、便宜。
//
// 注意：注意： 为什么不逐方块扫（2026-10-10 实测，别再来一遍）：
//    KubeJS 跑在 Rhino 上，每次 Java 调用要过一层 JS <-> Java 包装 ——
//    getBlockState 实测 8~12 微秒（原生 Java 约 0.1µs，差 50~100 倍）。
//     ->  50ms 预算只够读 ~6,000 格。
//      6 chunk 半径逐方块扫 = 43,264 列 × 4 次 ≈ 17 万次 ≈ 1.3 秒，不可行。
//     ->  所以这里只走两条便宜通道：
//        1 方块实体：chunk.getBlockEntities() 一次调用拿整块 chunk 的
//           箱子/熔炉/木桶/烟熏炉/漏斗/刷怪笼/告示牌/讲台/附魔台/信标/潜影盒/蜂箱
//        2 heightmap：每列一次 level.getHeight(...)，再抽样读地表那格
//
// 注意： 缺口（T2 要补的）：没有方块实体的 POI 这里拿不到 ——
//    工作台、铁砧、床、堆肥桶、石切机、织布机、制箭台、传送门框。
//    它和 mcb scan（定点找方块）是互补的，不是替代。
//
// 注意： 另一个实测发现：整合包里的 byepregen mod 换掉了 PalettedContainer
//    （com.moepus.byepregen.PaletteContainer.*）——
//    mcpfabric 那个"用 palette 一次聚合"的省法在这儿不成立（实测 count() 只数出 1）。
//    别再试那条路。
// 一个 chunk（相对玩家的原点 ox,oz，边长 16）整块都在半径 √RR 外吗？
// 用最近的那条边判 —— 保守（宁可多扫不可漏扫），不然会切掉贴着边界的 chunk。
function mcbRingOut(ox, oz, RR) {
  var nx = ox > 0 ? ox : (ox + 16 < 0 ? ox + 16 : 0)
  var nz = oz > 0 ? oz : (oz + 16 < 0 ? oz + 16 : 0)
  return nx * nx + nz * nz > RR
}

function mcbAround(player, radiusChunks, step) {
  var t0 = mcbNowMs()
  var out = { err: [], poi: [], poiKind: {}, ms: 0, radiusChunks: radiusChunks, step: step }
  var level = null
  try { level = player.level } catch (e0) { out.err.push('level: ' + e0); return out }

  var px = Number(player.x), py = Number(player.y), pz = Number(player.z)
  var pcx = Math.floor(px / 16), pcz = Math.floor(pz / 16)
  out.center = [Math.round(px * 10) / 10, Math.round(py * 10) / 10, Math.round(pz * 10) / 10]

  var $BP = null
  try { $BP = Java.loadClass('net.minecraft.core.BlockPos') } catch (eBP) { out.err.push('BlockPos: ' + eBP) }

  function pdist(x, y, z) {
    var dx = x + 0.5 - px, dy = y + 0.5 - py, dz = z + 0.5 - pz
    return Math.round(Math.sqrt(dx * dx + dy * dy + dz * dz) * 10) / 10
  }

  // ---- 1 POI：按 chunk 走方块实体 ----
  var chunks = []
  for (var dx = -radiusChunks; dx <= radiusChunks; dx++) {
    for (var dz = -radiusChunks; dz <= radiusChunks; dz++) {
      chunks.push([pcx + dx, pcz + dz, dx * dx + dz * dz])
    }
  }
  chunks.sort(function (a, b) { return a[2] - b[2] })     // 近的 chunk 先走

  var nChunk = 0, nBE = 0, i
  var live = []          // 已经取到的 chunk —— 第2段直接复用，别再 getChunk 一遍
  for (i = 0; i < chunks.length; i++) {
    if (mcbNowMs() - t0 > MCB_AROUND_MS_CHUNK) { out.truncatedChunks = chunks.length - i; break }
    var cx = chunks[i][0], cz = chunks[i][1]
    var ch = null
    try { ch = level.getChunk(cx, cz) } catch (eC) { continue }
    if (ch === null || ch === undefined) continue
    try { if (ch.isEmpty()) continue } catch (eEmp) { }
    nChunk++
    live.push({ cx: cx, cz: cz, ch: ch })

    var bes = null
    try { bes = ch.getBlockEntities() } catch (eBE) { continue }
    if (bes === null || bes === undefined) continue

    // 注意： LevelChunk.getBlockEntities() 回的是 Map（不是 Collection），
    //    所以要先 .values()。用 iterator 走 —— 比 Java.from() 稳。
    var it = null
    try { it = bes.values().iterator() } catch (eV) {
      try { it = bes.iterator() } catch (eV2) { it = null }
    }
    if (it === null) { if (out.beErr === undefined) out.beErr = '拿不到方块实体的迭代器'; continue }

    var guard = 0
    while (guard < 4000) {
      guard++
      var more = false
      try { more = !!it.hasNext() } catch (eH) { break }
      if (!more) break
      var be = null
      try { be = it.next() } catch (eN) { break }
      if (be === null || be === undefined) continue
      nBE++
      var bp = null
      try { bp = be.getBlockPos() } catch (eP) { continue }
      var bxyz = mcbPosXYZ(bp)
      if (bxyz === null) continue
      var bid = null
      try { bid = mcbBlockId(level.getBlockState(bp)) } catch (eS2) { }
      if (bid === null) continue
      out.poiKind[bid] = (out.poiKind[bid] || 0) + 1
      out.poi.push({ id: bid, pos: bxyz, dist: pdist(bxyz[0], bxyz[1], bxyz[2]) })
    }
  }
  out.chunksSeen = nChunk
  out.blockEntities = nBE
  out.msChunk = Math.round((mcbNowMs() - t0) * 10) / 10
  out.poi.sort(function (a, b) { return a.dist - b.dist })
  out.poiTotal = out.poi.length
  out.poi = out.poi.slice(0, MCB_AROUND_POI_MAX)

  // ---- 2 地表：heightmap 抽样 ----
  //
  // 注意： 必须按 chunk 走，不能用 level.getHeight(x,z) ——
  //    后者每次都要在世界里查一遍 chunk。实测地表抽样是总耗时的大头
  //    （每列约 62µs，比"两次 Java 调用"的估计高 3 倍 —— Rhino 连循环和算术都慢）。
  var $HM = null
  try { $HM = Java.loadClass('net.minecraft.world.level.levelgen.Heightmap$Types') } catch (eHM) {
    out.err.push('Heightmap: ' + eHM)
  }

  if ($HM !== null && $BP !== null) {
    var R = radiusChunks * 16
    var RR = R * R
    var counts = {}
    var yMin = null, yMax = null, water = 0, cols = 0, broke = false
    var ipx = Math.floor(px), ipz = Math.floor(pz)

    for (i = 0; i < live.length; i++) {
      if (mcbNowMs() - t0 > MCB_AROUND_MS_SURFACE) { broke = true; break }
      var lcx = live[i].cx, lcz = live[i].cz, ch2 = live[i].ch
      // 圆柱裁剪：这个 chunk 整块都在半径外就跳过
      var ox = lcx * 16 - ipx
      var oz = lcz * 16 - ipz
      if (mcbRingOut(ox, oz, RR)) continue

      for (var lx = 0; lx < 16; lx += step) {
        var bx0 = lcx * 16 + lx
        for (var lz = 0; lz < 16; lz += step) {
          var bz0 = lcz * 16 + lz
          var ddx = bx0 - ipx, ddz = bz0 - ipz
          if (ddx * ddx + ddz * ddz > RR) continue
          var h = null
          try { h = Number(ch2.getHeight($HM.MOTION_BLOCKING, lx, lz)) } catch (eH3) { continue }
          if (h === null || isNaN(h)) continue
          cols++
          if (yMin === null || h < yMin) yMin = h
          if (yMax === null || h > yMax) yMax = h
          // 注意： 地表那格是 h - 1 —— MOTION_BLOCKING 给的是"最高挡路方块上面那格"
          var sid = null
          try { sid = mcbBlockId(ch2.getBlockState(new $BP(bx0, h - 1, bz0))) } catch (eB2) { }
          if (sid !== null) {
            if (sid.indexOf('water') >= 0) water++
            counts[sid] = (counts[sid] || 0) + 1
          }
        }
      }
    }
    var arr = []
    for (var k in counts) {
      if (Object.prototype.hasOwnProperty.call(counts, k)) arr.push({ id: k, n: counts[k] })
    }
    arr.sort(function (a, b) { return b.n - a.n })
    out.surface = {
      cols: cols,
      step: step,
      yMin: yMin, yMax: yMax,
      // 相对玩家脚底的偏移 —— 比绝对 y 有用得多（"地势起伏多大"）
      relMin: yMin === null ? null : yMin - Math.floor(py),
      relMax: yMax === null ? null : yMax - Math.floor(py),
      waterCols: water,
      distinct: arr.length,
      counts: arr.slice(0, MCB_AROUND_SURFACE_MAX),
      truncated: broke
    }
  }

  var kindKeys = 0
  for (var kk in out.poiKind) { if (Object.prototype.hasOwnProperty.call(out.poiKind, kk)) kindKeys++ }
  if (kindKeys === 0) delete out.poiKind
  out.msSurface = Math.round((mcbNowMs() - t0 - out.msChunk) * 10) / 10
  out.ms = Math.round((mcbNowMs() - t0) * 10) / 10
  return out
}

// 读一个坐标的方块"视觉属性"（诊断用）。
//
// 注意： 存在的理由：验证"射线能不能穿玻璃"不需要让她瞄准 ——
//    瞄准会引入她走动、朝向滞后、擦边采样一堆干扰（2026-10-10 折腾了很久）。
//    直接读这一格的属性，命题就一句话：canOcclude() 是 true 还是 false。
function mcbBlockInfo(player, x, y, z) {
  var out = { pos: [x, y, z], err: [] }
  var level = null
  try { level = player.level } catch (e0) { out.err.push('level: ' + e0); return out }
  var $BP = null
  try { $BP = Java.loadClass('net.minecraft.core.BlockPos') } catch (e1) {
    out.err.push('BlockPos: ' + e1)
    return out
  }
  var st = null
  try { st = level.getBlockState(new $BP(x, y, z)) } catch (e2) { out.err.push('getBlockState: ' + e2); return out }
  out.id = mcbBlockId(st)

  // 三个视觉语义的判据，各读各的 —— 哪个能用是探出来的
  // 注意： isSolidRender() 这个映射里没有 —— Rhino 报
  //    "Can't find method BlockBehaviour$BlockStateBase.isSolidRender()"，别再试。
  //    可用的视觉判据就是 canOcclude 和 getLightBlock。
  try { out.canOcclude = !!st.canOcclude() } catch (e3) { out.err.push('canOcclude: ' + e3) }
  try { out.lightBlock = Number(st.getLightBlock()) } catch (e5) {
    try { out.lightBlock = Number(st.getLightBlock(level, new $BP(x, y, z))) } catch (e6) { }
  }
  // 对照：碰撞语义（mcpfabric 的 exposed 用的就是它）—— 实测玻璃
  // collisionEmpty=false 而 canOcclude=false，所以拿碰撞箱当"看不看得见"会判错。
  // 放这儿就是为了让两种语义的差别肉眼可见。
  try { out.collisionEmpty = !!st.getCollisionShape(level, new $BP(x, y, z)).isEmpty() } catch (e7) { }
  return out
}

function mcbPlayerState(player) {
  var out = { online: true }
  try { out.name = String(player.username) } catch (e) { out.name = MCB_TARGET }
  try { out.x = mcbNumOrNull(player.x) } catch (e) { out.x = null }
  try { out.y = mcbNumOrNull(player.y) } catch (e) { out.y = null }
  try { out.z = mcbNumOrNull(player.z) } catch (e) { out.z = null }
  try { out.hp = mcbNumOrNull(player.health) } catch (e) { out.hp = null }
  try { out.dim = String(player.level.dimension) } catch (e) { out.dim = null }
  // 时间 / 天气 —— 她该知道"现在几点、下没下雨、天快黑了吗"
  try { out.env = mcbEnv(player.level) } catch (eEnv) { out.env = { err: String(eEnv) } }
  // 饥饿度 —— 生存必需，服务端可读
  try {
    var fd = player.foodData
    out.food = { level: mcbNumOrNull(fd.foodLevel), saturation: mcbNumOrNull(fd.saturationLevel) }
  } catch (e2) {
    out.food = null
  }
  //  吃东西探针：服务端到底认不认"她正在使用物品"？
  // 客户端日志已经证明 isUsingItem=true 了，但那只是客户端的自我预测
  // （MultiPlayerGameMode.useItem 会在本地先跑一遍 Item.use）。
  // 真正把面包吃掉的是服务端的 LivingEntity.updateUsingItem ——
  // 它在 remaining 归零时调 completeUsingItem()，而那行有 !isClientSide 的守卫。
  //   服务端 isUsing=false         ->  包根本没到 / 被拒  ->  问题在下行
  //   服务端 isUsing=true 且 remain 在掉  ->  服务端在吃，问题在别处
  //   服务端 isUsing=true 但 remain 不动  ->  服务端也卡住了
  try {
    var ui = null
    try { ui = player.useItem } catch (eU1) { }
    var uiName = null
    try { uiName = String(ui.hoverName.string) } catch (eU2) { uiName = null }
    out.using = {
      isUsing: !!player.isUsingItem(),
      remain: mcbNumOrNull(player.useItemRemainingTicks),
      item: uiName
    }
  } catch (e3) {
    out.using = { err: String(e3) }
  }
  // 睡觉状态 —— 测"走到床边右键"这条链路要用它客观判定（光看坐标看不出睡没睡）
  try {
    var sl = null
    try { sl = !!player.isSleeping() } catch (eS1) {
      try { sl = !!player.sleeping } catch (eS2) { sl = null }
    }
    out.sleeping = sl
  } catch (e4) {
    out.sleeping = null
  }
  //  氧气 / 在水里 —— 2026-10-10 加，起因是她淹在水里出不来。
  //
  //    为什么必须单独报这一维：溺水是"会慢慢死、但血量不一定掉"的情形。
  //    实测现场：她卡在水里 253 秒，事件流里 drown 每秒一条，
  //    但 dmg: 0（身上挂着抗性 V） ->  反射只看 hp，于是永远不触发，她就一直泡着。
  //    ⭐ 教训：凡是要"保命"的判据，都得从"状态"判，不能只从"伤害"判。
  try {
    var air = null
    try { air = mcbNumOrNull(player.airSupply) } catch (eA1) {
      try { air = mcbNumOrNull(player.getAirSupply()) } catch (eA2) { air = null }
    }
    out.air = air
  } catch (eA) {
    out.air = null
  }
  try {
    out.inWater = !!player.isInWater()
  } catch (eW1) {
    try { out.inWater = !!player.isInWater } catch (eW2) { out.inWater = null }
  }
  try {
    out.underWater = !!player.isUnderWater()
  } catch (eU) {
    out.underWater = null
  }
  // 她自己那一列的地表高度 —— 一次 heightmap 调用，几乎免费。
  // 用途：溺水反射要一个"往上浮到哪"的目标（baritone goto <x> <地表y> <z>）。
  // 注意： 只查她脚下这一列，不做邻域 —— 反射是每秒跑的，不能扫一片。
  // 注意： mcbT2Height 要 mcbT2Setup() 跑过才有值（它俩是共用的缓存），
  //    不然传进去的是 null，getHeight 会抛 —— 这条路上必须显式补一次。
  try {
    mcbT2Setup()
    var lv = player.level
    var bx = Math.floor(Number(player.x)), bz = Math.floor(Number(player.z))
    out.surfY = Number(lv.getHeight(mcbT2Height, bx, bz))
  } catch (eSY) {
    out.surfY = null
  }

  //  朝向（她自己的头朝哪） —— 2026-10-10 加。
  //
  // 起因（用户实报）："她声称自己没低头，但一直是低头状态"。
  // 现场实测：mcb lookat 读到 pitch:81.3（几乎垂直朝下），而她嘴里说
  // "我哪有低头，是靴子沉啦。抬头了抬头了" —— 她连"自己在低头"都读不到，
  // 手里是零数据，只能编。
  //
  // 落回我们那条律：能力自我认知 = 工具表 + 状态包 ——
  // 这条信息以前只有 mc_lookat 一个地方吐过（还包装成"你面朝X（基本在往下看）"），
  // 而她不会去问那个问题。状态包里有，才算她"知道"。
  //
  // 注意： 读法复用 mcbYawPitch（字段  ->  getter  ->  从视线向量反推），
  //    实测走的就是最后那条（rotVia:"look"），一定通。
  try {
    var lookV = null
    try { lookV = player.getViewVector(1.0) } catch (eLV) { lookV = null }
    var angP = mcbYawPitch(player, lookV)
    out.rotVia = angP.via
    if (angP.yaw !== null) {
      out.yaw = mcbR1(angP.yaw)
      out.facing = mcbCompass(angP.yaw)
    }
    if (angP.pitch !== null) out.pitch = mcbR1(angP.pitch)
  } catch (eVP) {
    out.yaw = null
    out.pitch = null
  }

  out.task = mcbTaskSnapshot()
  return out
}
function mcbChatData(since, me) {
  var lines = []
  for (var i = 0; i < mcbChatBuf.length; i++) {
    if (mcbChatBuf[i].seq > since) lines.push(mcbChatBuf[i])
  }
  if (lines.length > MCB_CHAT_PAGE) lines = lines.slice(lines.length - MCB_CHAT_PAGE)
  // 注意： 距离在拉取时算，不在说话时算 —— 她要的是"现在他离我多远"，
  //    不是"他说话那一刻离我多远"。
  // 注意：注意： me 必须由调用方传进来（调用方才有 event）——
  //    在这里自己调 mcbServer() 是错的：它要 event 参数，
  //    不传会返回 null 并每次调用都刷两条 ERROR 日志（2026-10-10 踩过）。
  if (me !== null && me !== undefined) {
    var mx = Number(me.x), my = Number(me.y), mz = Number(me.z)
    var mdim = ''
    try { mdim = String(me.level.dimension) } catch (eMd) { }
    for (var j = 0; j < lines.length; j++) {
      var p = lines[j].pos
      if (p === null || p === undefined || p.length < 3) continue
      if (mdim && lines[j].dim && lines[j].dim !== mdim) continue
      var dx = Number(p[0]) - mx, dy = Number(p[1]) - my, dz = Number(p[2]) - mz
      lines[j].dist = Math.round(Math.sqrt(dx * dx + dy * dy + dz * dz) * 10) / 10
    }
  }
  return { max: mcbChatSeq, count: lines.length, lines: lines }
}

// --- 命令 -----------------------------------------------------------------

ServerEvents.basicCommand('mcb', event => {
  var parts = mcbSplit(event.input)
  var action = parts[0]
  var arg = parts[1]
  console.info('[mcb] 收到命令: action=' + action + ' arg="' + arg + '"')

  if (action === 'placed') {
    var placedArgs = arg.split(/\s+/)
    if (placedArgs.length !== 3 || !placedArgs.every(function (v) { return /^-?\d+$/.test(v) })) {
      mcbErr(event, action, '用法: mcb placed <x> <y> <z>')
      return
    }
    var placedServer = mcbServer(event)
    if (placedServer === null) { mcbErr(event, action, '服务端不可用'); return }
    var placedPlayer = mcbFindPlayer(placedServer, MCB_TARGET)
    if (placedPlayer === null) { mcbErr(event, action, 'Nanako 不在线，无法确定维度'); return }
    try {
      var $PlacedPos = Java.loadClass('net.minecraft.core.BlockPos')
      var placedPos = new $PlacedPos(Number(placedArgs[0]), Number(placedArgs[1]), Number(placedArgs[2]))
      var fact = mcbPlacedFact(placedPlayer.level, placedPos)
      // actor = 她自己 —— 插件侧靠它把"我放的"和"别人放的"分成互不相交的两半
      // （permission.py 的 self_placed / placed 两个信号）。
      fact.actor = String(placedPlayer.uuid)
      fact.verdict = mcbBreakVerdict(fact, String(placedPlayer.uuid))
      mcbOk(event, action, fact)
    } catch (errPlaced) { mcbErr(event, action, String(errPlaced)) }
    return
  }

  if (action === 'ping') {
    mcbOk(event, 'ping', { pong: true })
    return
  }

  if (action === 'diag') {
    var probe = {}
    probe.clock = mcbClockWinner
    probe.nowMs = mcbNowMs()
    probe.chatSeq = mcbChatSeq
    probe.upSeq = mcbUpSeq
    probe.upAgeMs = (mcbUpAtMs > 0 ? mcbNowMs() - mcbUpAtMs : -1)
    probe.upHasMenu = mcbUpHasMenu
    probe.upMenuLen = mcbUpMenuLen
    var ds = mcbServer(event)
    probe.serverEntry = (ds !== null)
    if (ds !== null) {
      var dp = mcbFindPlayer(ds, MCB_TARGET)
      probe.targetOnline = (dp !== null)
      if (dp !== null) probe.target = mcbPlayerState(dp)
    }
    console.info('[mcb] diag ' + JSON.stringify(probe))
    mcbOk(event, 'diag', probe)
    return
  }

  // 聊天增量 —— 不依赖 Nanako 在线，也不依赖客户端
  if (action === 'chat') {
    var since = 0
    try { since = parseInt(arg, 10) } catch (e) { since = 0 }
    if (isNaN(since) || since < 0) since = 0
    // 注意： me 在这里取 —— 只有这一层有 event。mcb chat 不依赖白在线，
    //    所以她不在时 me 为 null，只是算不出距离，消息照样回。
    var meForChat = null
    try {
      var dsChat = mcbServer(event)
      if (dsChat !== null && dsChat !== undefined) meForChat = mcbFindPlayer(dsChat, MCB_TARGET)
    } catch (eMeChat) { meForChat = null }
    mcbOk(event, 'chat', mcbChatData(since, meForChat))
    return
  }

  // 事件增量 —— 「刚才都发生了什么」（挨打/死亡/进出服）。
  // 和 chat 分开：聊天已经在聊天缓冲里带 seq 了，再录一份就是记两遍。
  // 注意： 背包变动不在这里 —— 服务端的 inventoryChanged 实测不 fire，
  //    改由插件侧的拉取式 diff 负责（见上方那段注释）。
  if (action === 'events') {
    var evSince = 0
    try { evSince = parseInt(arg, 10) } catch (eEv) { }
    if (isNaN(evSince) || evSince < 0) evSince = 0
    mcbOk(event, 'events', mcbEvSince(evSince))
    return
  }

  // 查任意玩家的位置 —— 脱战要能"往主人方向跑"，就得知道主人在哪。
  // 不依赖 Nanako 在线，所以放在她那条判断之前。
  if (action === 'where') {
    var ds2 = mcbServer(event)
    if (ds2 === null) { mcbErr(event, 'where', '服务端拿不到 server 入口'); return }
    var who = arg
    if (!who) { mcbErr(event, 'where', '没给玩家名'); return }
    var tp = mcbFindPlayer(ds2, who)
    if (tp === null) {
      // 不在线是状态不是故障 —— 和 state 一个规矩
      mcbOk(event, 'where', { online: false, name: who })
      return
    }
    var tout = { online: true, name: String(tp.username) }
    try { tout.x = mcbNumOrNull(tp.x) } catch (eW1) { tout.x = null }
    try { tout.y = mcbNumOrNull(tp.y) } catch (eW2) { tout.y = null }
    try { tout.z = mcbNumOrNull(tp.z) } catch (eW3) { tout.z = null }
    try { tout.dim = String(tp.level.dimension) } catch (eW4) { tout.dim = null }
    try { tout.hp = mcbNumOrNull(tp.health) } catch (eW5) { tout.hp = null }
    // 顺带算出离白多远 —— 问坐标的目的十有八九是"过去找他"，
    // 让服务端一次算完，省掉插件再查一次 state 的往返。
    try {
      var me = mcbFindPlayer(ds2, MCB_TARGET)
      if (me !== null && me !== undefined) {
        var sameDim = String(me.level.dimension) === String(tp.level.dimension)
        tout.sameDim = sameDim
        if (sameDim) {
          var ddx = Number(tp.x) - Number(me.x), ddy = Number(tp.y) - Number(me.y), ddz = Number(tp.z) - Number(me.z)
          tout.distToMe = Math.round(Math.sqrt(ddx * ddx + ddy * ddy + ddz * ddz) * 10) / 10
        }
      }
    } catch (eW6) { tout.distErr = String(eW6) }
    mcbOk(event, 'where', tout)
    return
  }

  var server = mcbServer(event)
  if (server === null) {
    mcbErr(event, action, '服务端拿不到 server 入口')
    return
  }

  var player = mcbFindPlayer(server, MCB_TARGET)
  if (player === null) {
    // 「不在线」是状态不是故障 —— 用 ok:true 包住，让调用方分得清
    // "管子断了" 和 "她人不在"
    if (action === 'state') {
      mcbOk(event, 'state', { online: false, name: MCB_TARGET })
    } else {
      mcbErr(event, action, MCB_TARGET + ' 不在线，动作未下发')
    }
    return
  }

  // state 纯服务端可答（坐标/血量/维度/饥饿是服务端真值），再并上客户端上报的任务状态
  if (action === 'state') {
    mcbOk(event, 'state', mcbPlayerState(player))
    return
  }

  // 背包 —— 服务端直接读，不需要客户端。白"知道自己有什么"才能做计划。
  if (action === 'inventory') {
    mcbOk(event, 'inventory', mcbInventory(player))
    return
  }

  // 配方查询 —— "这东西怎么做"。不依赖客户端，也不依赖她在哪。
  // 注意： 不依赖 Nanako 在线也更好，但放在这里够用了（她不在线时上面就 return 了）。
  if (action === 'recipe') {
    mcbOk(event, 'recipe', mcbRecipe(server, arg))
    return
  }

  // 周围扫描 —— 白"知道周围有什么"才能做计划
  if (action === 'scan') {
    // 注意：注意： 半径按预算反推，不写死（2026-10-10 重写）。
    //    这里原来是 if (r > 16) r = 16，注释写"16^3 = 4096 次读取" ——
    //    那个算法是错的：半径 16 是 33³ = 35,937 次，少算 8.8 倍。
    //    按实测 10µs/格，那是 ~360ms 的 tick 卡顿（默认值 8 也已经是 ~40ms）。
    //    现在按球体积反推：(4/3)πr³ ≤ MCB_SCAN_BUDGET。
    var r = 8
    try { r = parseInt(arg, 10) } catch (e) { r = 8 }
    if (isNaN(r) || r < 1) r = 8
    var rMax = Math.floor(Math.cbrt(MCB_SCAN_BUDGET * 3 / (4 * Math.PI)))
    if (rMax < 1) rMax = 1
    var asked = r
    if (r > rMax) r = rMax
    var sc = mcbScan(player, r)
    if (r !== asked) {
      sc.clampedFrom = asked
      sc.clampedTo = r
      sc.budget = MCB_SCAN_BUDGET
    }
    mcbOk(event, 'scan', sc)
    return
  }

  // 周边概览 —— 大半径、便宜的那一路（POI 走方块实体 + 地表走 heightmap）。
  // 注意： 它和 mcb scan 是互补的，不是替代：
  //    scan  = 定点找某个方块（工作台/矿），半径小（受预算限）
  //    around= "周围有什么"（箱子/熔炉/刷怪笼 + 地形起伏），半径到 6-8 chunk
  //    around 拿不到没有方块实体的 POI（工作台/铁砧/床/堆肥桶）—— 那是 T2 的活
  if (action === 'around') {
    // 注意： 默认 6 chunk / 步长 8 是实测调出来的（2026-10-10）：
    //    6/8  ->  49ms、451 列、跑完；6/4  ->  56ms 但只采到 320 列就被预算截断。
    //    被截断的细步长不如跑完的粗步长 —— 别以为步长越小越好。
    var rc = 6
    var stp = 8
    var parts = String(arg || '').split(' ')
    try {
      var a1 = parseInt(parts[0], 10)
      if (!isNaN(a1) && a1 > 0) rc = a1
    } catch (eA1) { }
    try {
      var a2 = parseInt(parts[1], 10)
      if (!isNaN(a2) && a2 > 0) stp = a2
    } catch (eA2) { }
    if (rc > 12) rc = 12          // 再大就不是"视距内"了
    if (stp < 1) stp = 1
    if (stp > 16) stp = 16        // 抽样步长，越小越细也越贵
    mcbOk(event, 'around', mcbAround(player, rc, stp))
    return
  }

  // T2：跨 tick 全分辨率扫描 —— 补 T1 的缺口（没有方块实体的 POI：工作台/铁砧/
  //     石切机/堆肥桶/传送门框…）。它是个后台任务，不阻塞 tick，结果进缓存。
  //     用法：
  //       mcb t2 start [chunk数]   开一个任务（默认半径 6 chunk），以发起者为圆心
  //       mcb t2 status            进度（跑没跑完、扫了多少、花了多少 tick）
  //       mcb t2 get               结果（方块计数 + POI 列表）
  //       mcb t2 stop              取消
  if (action === 't2') {
    var parts2 = String(arg || '').trim().split(' ')
    var verb2 = parts2[0]
    if (verb2 === 'start') {
      var rc2 = 6
      try {
        var a2 = parseInt(parts2[1], 10)
        if (!isNaN(a2) && a2 > 0) rc2 = a2
      } catch (eR2) { }
      if (rc2 > 12) rc2 = 12          // 再大就不是"视距内"了，而且后台要跑太久
      mcbOk(event, 't2', mcbT2Start(player, rc2))
      return
    }
    if (verb2 === 'status') { mcbOk(event, 't2', mcbT2Status()); return }
    if (verb2 === 'get') { mcbOk(event, 't2', mcbT2Get()); return }
    if (verb2 === 'stop') { mcbOk(event, 't2', mcbT2Stop()); return }
    mcbOk(event, 't2', { err: '用法: mcb t2 start [chunk数] | status | get | stop' })
    return
  }

  // 索敌 —— 附近有什么实体、谁离得最近
  if (action === 'threats') {    var tr = 24
    try { tr = parseInt(arg, 10) } catch (e) { tr = 24 }
    if (isNaN(tr) || tr < 2) tr = 24
    if (tr > 64) tr = 64
    mcbOk(event, 'threats', mcbThreats(player, tr))
    return
  }

  // 准星指着的那只实体（只读，不动手）—— mc_attack 过权限闸要用它
  if (action === 'attackTarget') {
    mcbOk(event, 'attackTarget', mcbAttackTarget(player))
    return
  }

  // 读一个方块的视觉属性（诊断用，不改世界）
  if (action === 'blockinfo') {
    var ba = String(arg || '').split(' ')
    var bx3 = parseInt(ba[0], 10), by3 = parseInt(ba[1], 10), bz3 = parseInt(ba[2], 10)
    if (isNaN(bx3) || isNaN(by3) || isNaN(bz3)) { mcbErr(event, 'blockinfo', '要三个整数坐标'); return }
    mcbOk(event, 'blockinfo', mcbBlockInfo(player, bx3, by3, bz3))
    return
  }

  // 扫描能力探针 —— 只读、不改世界。用来量"Rhino 下每次方块读多贵"。
  if (action === 'scanprobe') {
    mcbOk(event, 'scanprobe', mcbScanProbe(player, arg))
    return
  }

  // 准星注视 —— "我正看着什么"。服务端射线，不需要客户端。
  // 不给参数就用原版的方块交互距离（约 4.5 格），给了就用给的。
  if (action === 'lookat') {
    var lr = 4.5
    try {
      var bir = Number(player.blockInteractionRange)
      if (!isNaN(bir) && bir > 0) lr = bir
    } catch (eLR) { }
    try {
      var la = parseInt(arg, 10)
      if (!isNaN(la) && la > 0) lr = la
    } catch (eLA) { }
    if (lr > 64) lr = 64
    mcbOk(event, 'lookat', mcbLookAt(player, lr))
    return
  }

  // 注意：注意： closeGui 必须由服务端动手，不能整个转发给客户端
  //    （2026-10-09 深夜定位，完整分析见 main/docs/04-必读坑.md 坑 10s）
  //
  // 原来这一条也走下面的通用转发，客户端兜底走到了
  // player.clientSideCloseContainer() —— 它只做本地那一半
  // （containerMenu = inventoryMenu），不发 ServerboundContainerClosePacket。
  // 原版是"先发包、再本地收尾"，我们只拿了后半截。
  //
  // 后果：客户端以为 id=0（背包），服务端还开着 id=1（那个箱子）。
  // 之后每一次 handleInventoryMouseClick 带的 containerId 都对不上  -> 
  // 服务端静默丢弃。症状最坑人：返回 sent:true、客户端日志也打了"已点格子"，
  // 但世界毫无变化 —— 极易误判成"格子号算错了"。
  // 脱节之后 合成 / 开箱子 / 换快捷栏全都失效。
  //
  // 正解：服务端权威地关，两边一起归位。这还能修复已经脱节的状态
  // （客户端侧修法是修不了现状的，得先重启客户端）。
  if (action === 'closeGui') {
    var closedId = -1
    var how = 'none'
    try { closedId = Number(player.containerMenu.containerId) } catch (eId) { }

    // 1 先告诉客户端：ClientboundContainerClosePacket  ->  客户端
    //    ClientPacketListener.handleContainerClose  ->  clientSideCloseContainer()
    //     ->  客户端的 containerMenu 回到 inventoryMenu。
    //
    //    注意：注意： 这一步不能省。 踩过（2026-10-09）：只做服务端 doCloseContainer()
    //    的话客户端不知道，它会一直以为自己还开着那个容器 ——
    //    于是又变成反方向的脱节（服务端 id=0 / 客户端 id=1），点格子照样被丢。
    //    关容器必须两边都通知到，缺一边就是脱节。
    //
    //    注意： 这是服务端 -> 客户端的包，没有 sequence 校验问题
    //    （那条铁律只管客户端 -> 服务端）。参数在 1.20.2 之后被废弃过，
    //    所以两种构造都试一遍。
    var told = 'no'
    try {
      var $CCCP = Java.loadClass('net.minecraft.network.protocol.game.ClientboundContainerClosePacket')
      var pkt = null
      try {
        pkt = new $CCCP(closedId)
      } catch (eCtor1) {
        pkt = new $CCCP()
      }
      player.connection.send(pkt)
      told = 'packet#' + closedId
    } catch (ePkt) {
      console.error('[mcb] 通知客户端关容器失败: ' + ePkt)
    }

    // 2 服务端权威地关自己那一半。
    //    注意： 服务端上 player.closeContainer()不存在（实测），只能 doCloseContainer()。
    try {
      player.doCloseContainer()
      how = 'doCloseContainer'
    } catch (eC2) {
      console.error('[mcb] 服务端关闭容器失败: ' + eC2)
    }

    // 3 再让客户端脚本把画面也撤掉（setScreen(null)）。
    //    连接断过、或客户端脚本版本旧时，前两步可能没覆盖到。
    try {
      player.sendData(MCB_CHANNEL_DOWN, { action: 'closeGui', arg: '' })
    } catch (eFwd) { }

    console.info('[mcb] 关容器 id=' + closedId + ' 客户端通知=' + told + ' 服务端=' + how)
    mcbOk(event, 'closeGui', {
      closed: closedId, told: told, by: 'server', how: how, target: MCB_TARGET
    })
    return
  }

  if (action === 'walk') {
    try {
      var intent = JSON.parse(arg)
      if (typeof intent.cmd !== 'string' || !intent.cmd.trim()
          || typeof intent.token !== 'string' || !/^[a-f0-9]{32}$/.test(intent.token)
          || typeof intent.owner !== 'string') {
        mcbErr(event, action, '寻路意图格式错误')
        return
      }
      player.sendData(MCB_CHANNEL_DOWN, { action: 'baritone', arg: intent.cmd })
      mcbWalkIntent = { token: intent.token, owner: intent.owner }
      mcbOk(event, action, { sent: true, token: intent.token })
    } catch (eWalk) { mcbErr(event, action, '寻路下发失败: ' + eWalk) }
    return
  }

  try {
    player.sendData(MCB_CHANNEL_DOWN, { action: action, arg: arg })
    if (action === 'baritone' || action === 'stop') mcbWalkIntent = null
    console.info('[mcb] 已下发 action=' + action + ' -> ' + MCB_TARGET)
    // 注意： 这里只代表"已下发到她的客户端"，不代表客户端真的执行了。
    //    动作有没有生效，靠 mc_state 看坐标 / task 状态复核。
    mcbOk(event, action, { sent: true, target: MCB_TARGET })
  } catch (e) {
    console.error('[mcb] sendData 失败: ' + e)
    mcbErr(event, action, '下发失败: ' + e)
  }
})

console.info('[mcb] 服务端脚本已加载（统一信封 v5 + 状态上行）')
