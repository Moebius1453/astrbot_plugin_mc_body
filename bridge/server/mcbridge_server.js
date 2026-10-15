// Rhino 函数内只用 var；禁用 cls 和 getClass，回执走 event.respond。

const MCB_TARGET = 'Nanako'
const MCB_CHANNEL_DOWN = 'mcbridge'      
const MCB_CHANNEL_UP = 'mcbridge_up'     

// Rhino 方块访问实测约 8~12 微秒，扫描预算不能按原生 Java 推算。

const MCB_SCAN_BUDGET = 6000

// 首次扫描可能加载 chunk；预算已接近单 tick，不宜频繁调用。
const MCB_AROUND_MS_CHUNK = 45      
const MCB_AROUND_MS_SURFACE = 55    
const MCB_AROUND_POI_MAX = 60       
const MCB_AROUND_SURFACE_MAX = 8    


function mcbSplit(raw) {
  var s = ''
  if (raw !== null && raw !== undefined) s = String(raw)
  s = s.trim()
  if (s.length === 0) return ['ping', '']
  var i = s.indexOf(' ')
  if (i < 0) return [s, '']
  return [s.substring(0, i), s.substring(i + 1).trim()]
}

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
    }
  }
  console.error('[mcb] 四种 server 入口都拿不到')
  return null
}

function mcbFindPlayer(server, name) {
  // server.getPlayer 的该重载接收 UUID，名字需遍历在线列表。
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

// 计时热路径缓存已探明的函数，避免测量开销污染结果。
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
        mcbClockFn = ways[i][1]      
        return v
      }
    } catch (e) {
    }
  }
  return 0
}


function mcbComponent(text) {
  try {
    return Java.loadClass('net.minecraft.network.chat.Component').literal(String(text))
  } catch (e) {
    console.error('[mcb] Component.literal 不可用: ' + e)
    return null
  }
}

// event.source 不可靠，回执使用 event.respond(Component)。
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

var mcbReplyToken = null

// 命令在服务端线程同步执行，每次入口重置回执标识。
function mcbEnvelope(event, obj) {
  if (mcbReplyToken !== null) obj.request = mcbReplyToken
  var body = ''
  try {
    body = JSON.stringify(obj)
  } catch (e) {
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

// 陈旧快照不等于当前状态，缺失不等于空闲。

const MCB_UP_STALE_MS = 4000

var mcbUpSeq = 0        
var mcbUpData = null    
var mcbUpAtMs = 0
var mcbUpHasMenu = false   
var mcbUpMenuLen = 0       
var mcbSayReceipts = {}
var mcbSayOrder = []

NetworkEvents.dataReceived(MCB_CHANNEL_UP, event => {
  try {
    var tag = event.data
    if (mcbTagStr(tag, 'receipt') === 'say') {
      var sender = event.player
      if (sender === null || String(sender.username) !== MCB_TARGET) return
      var receiptId = mcbTagStr(tag, 'receiptId')
      var receipt = mcbSayReceipts[receiptId]
      if (receipt && receipt.state === 'sent') {
        var receiptState = mcbTagStr(tag, 'result')
        if (receiptState === 'executed' || receiptState === 'failed') {
          receipt.state = receiptState
          receipt.error = mcbTagStr(tag, 'error')
        }
      }
      return
    }
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
  // 先 contains 再 getDouble；缺键返回 0.0，会伪造已知零值。
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
    // 客户端镜头与服务端朝向可能不同，必须分别保留。
    cliYaw: mcbUpData.yaw,
    cliPitch: mcbUpData.pitch,
    menu: mcbParseJsonField(mcbUpData.menu)
  }
}

// 保留自身聊天供上下文使用，是否唤醒由插件策略判定。

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

// 聊天独立缓冲，不能再录事件流造成重复。
// 机制抄 mcpfabric 的 EventBus：环形缓冲 + 单调 seq + 按 sinceId 增量拉取。
// 未知事件注册会使脚本加载失败，每个钩子独立保护。
const MCB_EV_MAX = 600

// 只合并连续同类事件，并刷新末次时间；跨事件合并会打乱时序。
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

// 已废弃 inventoryChanged：Inventory.add 绕过槽位监听；改由插件快照 diff。


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

// 字段可能缺失；String(undefined) 非空，不能只判空串。
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
  // cancel 通过 EventExit 退出，不能被错误处理吞掉。
  if (deny !== null) event.cancel()
})

// DamageSource 实体访问器不可靠，只报告实际读到的伤害类型。
function mcbSourceLabel(src) {
  if (src === null || src === undefined) return null
  var ent = null
  try { if (typeof src.getEntity === 'function') ent = src.getEntity() } catch (e1) { }
  if (ent !== null && ent !== undefined) {
    var nm = mcbEntityName(ent)
    if (mcbIsUsableName(nm)) return nm
  }
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

// 名字须排除 undefined/null 字符串，不能只检查空串。
function mcbIsUsableName(v) {
  if (v === null || v === undefined) return false
  if (typeof v === 'function') return false
  var s = String(v)
  if (s === '' || s === 'undefined' || s === 'null' || s === 'Function') return false
  if (s.indexOf('function ') === 0) return false
  return true
}

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
      // 优先 getSource()；event.source 可能读成 Function。
      var src = null
      try { src = event.getSource() } catch (eS2) {
        try { src = event.source } catch (eS1) { }
      }
      var vname = mcbEntityLabel(victim)
      var attacker = mcbSourceLabel(src)

      var iAmVictim = (vname === MCB_TARGET)
      var iAmAttacker = (attacker === MCB_TARGET)
      if (!iAmVictim && !iAmAttacker && !mcbIsPlayer(victim)) return

      var hp = null
      try { hp = mcbNumOrNull(victim.health) } catch (eH) { }
      var extra = {}
      if (dmg !== null && !isNaN(dmg)) extra.dmg = Math.round(dmg * 10) / 10
      if (attacker !== null) extra.by = attacker
      if (hp !== null) extra.hp = hp
      try { extra.srcDbg = mcbSourceDebug(src) } catch (eSd) { }

      // victim 是受害者，who 是引发者，熟度不能混用。
      try { extra.victim = vname } catch (eV) { }

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
      if (!isPlayer && killer !== MCB_TARGET) return   
      mcbEvAdd('death', killer === null ? '环境' : killer, who + ' 死了', { victim: who })
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

// 扫描是每 tick 固定预算，不能一次跑满全部任务。
// 空闲 tick 先纯 JS 判空，避免无任务时仍调用 Java。

const MCB_T2_READS_PER_TICK = 600        
const MCB_T2_BAND_BELOW = 4              
const MCB_T2_BAND_ABOVE = 4              
const MCB_T2_POI_MAX = 300               
const MCB_T2_KEEP_MS = 10 * 60 * 1000    

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

var mcbT2 = null        
var mcbT2BP = null      
var mcbT2Height = null  

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

// 热扫描缓存已验证的访问器，避免逐格重复探测。
function mcbBlockIdFast(state) {
  if (mcbBlockIdVia === 'state.id') { try { return String(state.id) } catch (e1) { } }
  else if (mcbBlockIdVia === 'state.block.id') { try { return String(state.block.id) } catch (e2) { } }
  else if (mcbBlockIdVia === 'state.block') { try { return String(state.block) } catch (e3) { } }
  else if (mcbBlockIdVia === 'state.toString') { try { return String(state) } catch (e4) { } }
  return mcbBlockId(state)     
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
  chunks.sort(function (a, b) { return a[2] - b[2] })   

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

function mcbT2Step() {
  var t = mcbT2
  if (t === null || t.doneAt > 0) return
  t.ticks++

  var level = t.level
  if (level === null || level === undefined) { t.err.push('level 丢了'); mcbT2 = null; return }

  var perCol = MCB_T2_BAND_ABOVE + MCB_T2_BAND_BELOW + 1   
  var colCost = perCol + 1                                  
  var budget = MCB_T2_READS_PER_TICK
  // used 是本 tick 预算，t.reads 是累计值，不能混用。
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
    if (skip) { t.ci++; t.col = 0; t.skippedChunks++; continue }   

    var colsLeft = 256 - t.col
    var colsNow = Math.floor((budget - used) / colCost)
    if (colsNow < 1) colsNow = 1
    if (colsNow > colsLeft) colsNow = colsLeft

    for (var k = 0; k < colsNow; k++, t.col++) {
      var wx = c[0] * 16 + (t.col & 15)
      var wz = c[1] * 16 + ((t.col >> 4) & 15)

      // getHeight 接收世界坐标，不是 chunk 内坐标。
      var sy = -1
      try { sy = Number(ch.getHeight(mcbT2Height, wx, wz)) } catch (eH) { sy = -1 }
      used += colCost
      t.reads += colCost
      if (!(sy > -100)) continue          // 高度未知跳过，不能当 0。
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

// 过期惰性清理，避免无效结果每 tick 调 Java 时钟。
function mcbT2Reap() {
  if (mcbT2 === null) return
  if (mcbT2.doneAt <= 0) return
  if (mcbNowMs() - mcbT2.doneAt > MCB_T2_KEEP_MS) mcbT2 = null
}

try {
  ServerEvents.tick(event => {
    // 无任务先纯 JS 返回，避免空闲 tick 调 Java。
    if (mcbT2 === null || mcbT2.doneAt > 0) return
    try { mcbT2Step() } catch (e) {
      if (mcbT2 !== null) mcbT2.err.push('tick: ' + e)
    }
  })
} catch (eT2Reg) {
  console.error('[mcb] ServerEvents.tick 注册失败: ' + eT2Reg)
}


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

  // modifiers 为空表示无攻击加成，不等于读取失败。
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
        // 属性存的是加成，需加徒手基础 1 才是真实伤害。
        if (!isNaN(v)) atk = v + 1
        break
      }
    }
  } catch (eAtk) {
    console.warn('[mcb] 读攻击伤害失败: ' + eAtk)
  }

  // 标签限制数量，避免大响应挤满 RCON。
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
  // 匹配必须用注册名，显示名随语言变化。
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


var mcbBlockIdVia = null

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
  out.estimateMs = Math.round(scanned * 10 / 1000)   
  out.via = mcbBlockIdVia

  var arr = []
  for (var k in counts) {
    if (Object.prototype.hasOwnProperty.call(counts, k)) {
      arr.push({ id: k, n: counts[k], nearest: nearest[k] })
    }
  }
  arr.sort(function (a, b) { return b.n - a.n })
  // 保留稀有类型，按数量截断可能恰好丢掉唯一工作台。
  out.types = arr
  out.totalTypes = arr.length
  return out
}

// 抄的是原版那套（女仆的 MaidHostilesSensor 也是抄原版）：

// 注意： 只按类判"是不是怪"是不够的 —— 抄 Kindred 的坑 #1：
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
  var order = []     

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

  try {
    var $M = Java.loadClass('net.minecraft.world.entity.monster.Monster')
    collect(level.getEntitiesOfClass($M, box), true)
    out.via.push('Monster')
  } catch (e1) {
    out.err.push('Monster: ' + e1)
  }

  //    注意：注意： 这一步不能省 —— 抄 Kindred 的坑 #1：他们的同伴被狼咬死，
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
    en.hostile = !!en._m || en.targeting || MCB_HOSTILE_RE.test(en.type)
    // 主动 hunt 只认 monster；hostile 并集可能包含宠物。
    en.monster = !!en._m
    delete en._m
    arr.push(en)
  }

  arr.sort(function (a, b) { return a.dist - b.dist })
  out.count = arr.length
  out.threats = arr.slice(0, 20)
  out.hostiles = arr.filter(function (x) { return x.hostile }).slice(0, 10)
  return out
}

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
      // 无法读取判定箱时保守算命中，以免权限漏判。
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

      try { out.custom = !!e.hasCustomName() } catch (eC1) {
        try { out.custom = (e.customName !== null && e.customName !== undefined) } catch (eC2) {
          out.custom = null
          out.err.push('custom: 读不到')
        }
      }

      // 区分没有主人概念与主人未知，避免权限错误放行。
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
        out.ownerVia = 'absent'          
      }

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

// 服务端朝向约延迟一 tick；此处是近似，不是客户端 hitResult。

function mcbFaceFromDelta(dx, dy, dz) {
  if (dy === 1) return 'up'
  if (dy === -1) return 'down'
  if (dz === 1) return 'south'
  if (dz === -1) return 'north'
  if (dx === 1) return 'east'
  if (dx === -1) return 'west'
  return null
}

// OUTLINE 会挡住玻璃等可透视方块，这里改用 canOcclude 表达视觉遮挡。
//    这是视觉语义，跟碰撞箱（getCollisionShape，mcpfabric 用的那个）不是一回事。
// seen 是首个可见非空气，occluder 是真正挡住视线的方块。
function mcbRayBlock(level, ex, ey, ez, lx, ly, lz, maxDist) {
  var out = { seen: null, occluder: null, steps: 0 }
  var $BP = null
  try { $BP = Java.loadClass('net.minecraft.core.BlockPos') } catch (e0) {
    out.err = 'BlockPos: ' + e0
    return out
  }
  var STEP = 0.05                 
  var prev = null
  var t = 0.0
  while (t <= maxDist && out.steps < 3000) {
    out.steps++
    // 取采样步长中点，避免浮点边界落入上一格。
    var ts = t + STEP * 0.5
    var bx = Math.floor(ex + lx * ts)
    var by = Math.floor(ey + ly * ts)
    var bz = Math.floor(ez + lz * ts)
    if (prev === null || bx !== prev[0] || by !== prev[1] || bz !== prev[2]) {
      var id = null
      var occl = true             
      try {
        var st = level.getBlockState(new $BP(bx, by, bz))
        id = mcbBlockId(st)
        try {
          occl = !!st.canOcclude()
        } catch (eO) {
          occl = true
          if (out.occlErr === undefined) out.occlErr = String(eO)
        }
      } catch (eR) { id = null }
      if (id !== null && id.indexOf('air') < 0) {
        var here = {
          id: id,
          pos: [bx, by, bz],
          face: prev === null ? null : mcbFaceFromDelta(prev[0] - bx, prev[1] - by, prev[2] - bz),
          dist: mcbR1(ts),
          occl: occl                       
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

// BlockPos 与 MutableBlockPos 的坐标可能分别是方法和字段，需双路径校验。
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

function mcbR1(v) {
  var n = Number(v)
  return isNaN(n) ? null : Math.round(n * 10) / 10
}

// MC yaw: 0=南，90=西，180=北，270=东。
function mcbCompass(yaw) {
  if (isNaN(yaw)) return null
  var names = ['south', 'southwest', 'west', 'northwest', 'north', 'northeast', 'east', 'southeast']
  var d = ((yaw % 360) + 360) % 360
  return names[Math.round(d / 45) % 8]
}

// 字段 undefined 不抛异常，必须判 NaN；Java getter 必须字面量调用。
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

  var ang = mcbYawPitch(player, look)
  out.rotVia = ang.via
  if (ang.yaw !== null) {
    out.yaw = mcbR1(ang.yaw)
    out.facing = mcbCompass(ang.yaw)
  }
  if (ang.pitch !== null) out.pitch = mcbR1(ang.pitch)
  if (ang.yaw === null) out.err.push('拿不到 yaw（字段/ getter / 视线反推 三条路都不通）')

  var blockDist = reach
  out.block = null
  try {
    var ray = mcbRayBlock(level, ex, ey, ez, lx, ly, lz, reach)
    if (ray.err) out.err.push('ray: ' + ray.err)
    out.raySteps = ray.steps
    var hitBlock = ray.occluder !== null ? ray.occluder : ray.seen
    if (hitBlock !== null) {
      out.block = hitBlock
      blockDist = hitBlock.dist
      if (ray.occluder !== null && ray.seen !== null && ray.seen !== ray.occluder) {
        out.through = ray.seen
      }
    } else if (ray.seen !== null) {
      out.block = ray.seen
      out.blockDist = ray.seen.dist
      out.onlySeen = true
    }
    try {
      var vp = player.pick(reach, 1.0, false)
      if (vp !== null && vp !== undefined) {
        var vt = String(vp.getType())
        var vpv = { type: vt }
        if (vt === 'BLOCK') {
          var vbp = vp.getBlockPos()
          vpv.id = mcbBlockId(level.getBlockState(vbp))
          vpv.pos = mcbPosXYZ(vbp)
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
      var t = dx * lx + dy * ly + dz * lz              
      if (t < -0.5 || t > reach + 1.0) continue         
      var qx = ex + lx * t, qy = ey + ly * t, qz = ez + lz * t
      var perp = Math.sqrt(
        Math.pow(cp[0] - qx, 2) + Math.pow(cp[1] - qy, 2) + Math.pow(cp[2] - qz, 2)
      )
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
      // 实体必须比遮挡方块近，补上锥形命中的遮挡校验。
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

//    种类、数量、摆法、要不要工作台，全在里面。抄它就不会漏 mod。

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

// 此版 Ingredient 无 getItems；从 values 或 KubeJS 扩展取候选。
// Java 数组用 length 与下标，不支持 iterator。
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

  try {
    var vals = ing.values
    if (vals !== null && vals !== undefined) {
      for (var i = 0; i < Number(vals.length); i++) {
        try { addArr(vals[i].getItems()) } catch (e1) { errs.push('v.getItems:' + e1) }
      }
    }
  } catch (e0) { errs.push('values:' + e0) }

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

function mcbRecipeInfo(recipe, holder, kind) {
  var info = { id: null, kind: kind || 'crafting', type: 'unknown',
    w: 0, h: 0, grid: [], shapeless: [], out: null, outN: 1 }
  try { info.id = String(holder.id()) } catch (e0) {
    try { info.id = String(recipe.getId()) } catch (e0b) { }
  }

  try {
    var res = recipe.getResultItem(null)
    info.out = mcbItemId(res)
    try { info.outN = Number(res.getCount()) } catch (eN) { info.outN = 1 }
  } catch (e1) { }

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


function mcbNumOrNull(v) {
  if (v === null || v === undefined) return null
  var n = Number(v)
  return isNaN(n) ? null : Math.round(n * 100) / 100
}

// 字段需先判 NaN，再用字面量 getter 兜底；不能只靠 catch。
function mcbEnv(level) {
  var out = { err: [] }
  if (level === null || level === undefined) { out.err.push('level 为空'); return out }

  var tod = null
  try { var v1 = Number(level.dayTime); if (!isNaN(v1)) tod = v1 } catch (e1) { }
  if (tod === null) { try { var v2 = Number(level.getDayTime()); if (!isNaN(v2)) tod = v2 } catch (e2) { } }
  if (tod !== null) {
    var ticks = ((tod % 24000) + 24000) % 24000
    // 0 tick 对应 06:00，先加 6 小时。
    var mins = Math.floor((((ticks / 1000) + 6) % 24) * 60)
    out.tod = Math.floor(ticks)
    out.day = Math.floor(tod / 24000)
    out.hhmm = ('0' + Math.floor(mins / 60)).slice(-2) + ':' + ('0' + (mins % 60)).slice(-2)
  }

  try { var b1 = level.isDay(); if (b1 !== null && b1 !== undefined) out.daytime = !!b1 } catch (e3) { }
  try { var b2 = level.isRaining(); if (b2 !== null && b2 !== undefined) out.raining = !!b2 } catch (e4) { }
  try { var b3 = level.isThundering(); if (b3 !== null && b3 !== undefined) out.thundering = !!b3 } catch (e5) { }

  try { var p1 = Number(level.moonPhase); if (!isNaN(p1)) out.moon = p1 } catch (e6) {
    try { var p2 = Number(level.getMoonPhase()); if (!isNaN(p2)) out.moon = p2 } catch (e7) { }
  }
  return out
}

// 注意：注意： 为什么不照抄 mcpfabric 的半径：
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

  try {
    var $HM = Java.loadClass('net.minecraft.world.level.levelgen.Heightmap$Types')
    var h = Number(chunk.getHeight($HM.MOTION_BLOCKING, 8, 8))
    out.api.heightMotionBlocking = h
    out.ok.push('chunk.getHeight(MOTION_BLOCKING)')
  } catch (e9) { out.err.push('getHeight: ' + e9) }

  // 采样数过小会混入解释器/JIT 预热开销。
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

  var budget = {}
  var b1 = out.bench.sectionGetBlockState
  if (b1 && b1.perCallUs > 0) {
    budget.section_fullCube_per50ms = Math.floor(50000 / b1.perCallUs)
    budget.section_columns_per50ms = Math.floor(50000 / (b1.perCallUs * 7))
  }
  var b2 = out.bench.levelGetBlockState
  if (b2 && b2.perCallUs > 0) {
    budget.level_fullCube_per50ms = Math.floor(50000 / b2.perCallUs)
  }
  out.budget = budget
  return out
}

// byepregen 替换 PalettedContainer，此映射 count() 不能可靠聚合。
//    mcpfabric 那个"用 palette 一次聚合"的省法在这儿不成立（实测 count() 只数出 1）。
// 按最近边判 chunk 是否越界，保守保留边缘 chunk。
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

  var chunks = []
  for (var dx = -radiusChunks; dx <= radiusChunks; dx++) {
    for (var dz = -radiusChunks; dz <= radiusChunks; dz++) {
      chunks.push([pcx + dx, pcz + dz, dx * dx + dz * dz])
    }
  }
  chunks.sort(function (a, b) { return a[2] - b[2] })     

  var nChunk = 0, nBE = 0, i
  var live = []          
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

    // getBlockEntities 返回 Map，先 values 再 iterator。
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

  // 按 chunk 复用高度查询，避免每列重复查找 chunk。
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
          // MOTION_BLOCKING 返回最高阻挡方块上一格，地表取 h-1。
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

  // 此映射无 isSolidRender；使用可用的 canOcclude/getLightBlock。
  try { out.canOcclude = !!st.canOcclude() } catch (e3) { out.err.push('canOcclude: ' + e3) }
  try { out.lightBlock = Number(st.getLightBlock()) } catch (e5) {
    try { out.lightBlock = Number(st.getLightBlock(level, new $BP(x, y, z))) } catch (e6) { }
  }
  // 对照：碰撞语义（mcpfabric 的 exposed 用的就是它）—— 实测玻璃
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
  try { out.env = mcbEnv(player.level) } catch (eEnv) { out.env = { err: String(eEnv) } }
  try {
    var fd = player.foodData
    out.food = { level: mcbNumOrNull(fd.foodLevel), saturation: mcbNumOrNull(fd.saturationLevel) }
  } catch (e2) {
    out.food = null
  }
  // 客户端使用状态只是预测，完成消费由服务端确认。
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
  try {
    var sl = null
    try { sl = !!player.isSleeping() } catch (eS1) {
      try { sl = !!player.sleeping } catch (eS2) { sl = null }
    }
    out.sleeping = sl
  } catch (e4) {
    out.sleeping = null
  }
  // 溺水可能不掉血，需独立报告氧气。
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
  // getHeight 前确保 mcbT2Setup 已初始化共享 Heightmap 缓存。
  try {
    mcbT2Setup()
    var lv = player.level
    var bx = Math.floor(Number(player.x)), bz = Math.floor(Number(player.z))
    out.surfY = Number(lv.getHeight(mcbT2Height, bx, bz))
  } catch (eSY) {
    out.surfY = null
  }

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
  // 距离按拉取时计算，不是说话时。
  // me 由持有 event 的调用方传入，不能在此无参调用 mcbServer。
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


ServerEvents.basicCommand('mcb', event => {
  var input = String(event.input)
  mcbReplyToken = null
  var tagged = /^@([a-f0-9]{32})\s+([\s\S]+)$/.exec(input)
  if (tagged !== null) {
    mcbReplyToken = tagged[1]
    input = tagged[2]
  }
  var parts = mcbSplit(input)
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
      fact.actor = String(placedPlayer.uuid)
      fact.verdict = mcbBreakVerdict(fact, String(placedPlayer.uuid))
      mcbOk(event, action, fact)
    } catch (errPlaced) { mcbErr(event, action, String(errPlaced)) }
    return
  }

  if (action === 'claudecode') {
    if (!arg || arg.length > 256) {
      mcbErr(event, action, '测试消息需要 1 至 256 字符')
      return
    }
    mcbChatSeq = mcbChatSeq + 1
    mcbChatBuf.push({ seq: mcbChatSeq, t: mcbNowMs(), who: 'ClaudeCode', text: arg })
    while (mcbChatBuf.length > MCB_CHAT_MAX) mcbChatBuf.shift()
    console.info('[mcb] chat #' + mcbChatSeq + ' <ClaudeCode> ' + arg)
    mcbOk(event, action, { seq: mcbChatSeq, who: 'ClaudeCode' })
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

  if (action === 'chat') {
    var since = 0
    try { since = parseInt(arg, 10) } catch (e) { since = 0 }
    if (isNaN(since) || since < 0) since = 0
    // 聊天不依赖角色在线；缺 me 只影响距离。
    var meForChat = null
    try {
      var dsChat = mcbServer(event)
      if (dsChat !== null && dsChat !== undefined) meForChat = mcbFindPlayer(dsChat, MCB_TARGET)
    } catch (eMeChat) { meForChat = null }
    mcbOk(event, 'chat', mcbChatData(since, meForChat))
    return
  }

  // 服务端 inventoryChanged 不可靠，背包变动由插件快照 diff。
  if (action === 'events') {
    var evSince = 0
    try { evSince = parseInt(arg, 10) } catch (eEv) { }
    if (isNaN(evSince) || evSince < 0) evSince = 0
    mcbOk(event, 'events', mcbEvSince(evSince))
    return
  }

  if (action === 'where') {
    var ds2 = mcbServer(event)
    if (ds2 === null) { mcbErr(event, 'where', '服务端拿不到 server 入口'); return }
    var who = arg
    if (!who) { mcbErr(event, 'where', '没给玩家名'); return }
    var tp = mcbFindPlayer(ds2, who)
    if (tp === null) {
      mcbOk(event, 'where', { online: false, name: who })
      return
    }
    var tout = { online: true, name: String(tp.username) }
    try { tout.x = mcbNumOrNull(tp.x) } catch (eW1) { tout.x = null }
    try { tout.y = mcbNumOrNull(tp.y) } catch (eW2) { tout.y = null }
    try { tout.z = mcbNumOrNull(tp.z) } catch (eW3) { tout.z = null }
    try { tout.dim = String(tp.level.dimension) } catch (eW4) { tout.dim = null }
    try { tout.hp = mcbNumOrNull(tp.health) } catch (eW5) { tout.hp = null }
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
    // 离线是正常状态，使用 ok:true 与桥故障区分。
    if (action === 'state') {
      mcbOk(event, 'state', { online: false, name: MCB_TARGET })
    } else {
      mcbErr(event, action, MCB_TARGET + ' 不在线，动作未下发')
    }
    return
  }

  if (action === 'state') {
    mcbOk(event, 'state', mcbPlayerState(player))
    return
  }

  if (action === 'inventory') {
    mcbOk(event, 'inventory', mcbInventory(player))
    return
  }

  if (action === 'recipe') {
    mcbOk(event, 'recipe', mcbRecipe(server, arg))
    return
  }

  if (action === 'scan') {
    // 扫描半径由球体积预算反推，不能误把半径当边长。
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

  if (action === 'around') {
    // 更细步长可能先耗尽预算，未必比完整粗采样好。
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
    if (rc > 12) rc = 12          
    if (stp < 1) stp = 1
    if (stp > 16) stp = 16        
    mcbOk(event, 'around', mcbAround(player, rc, stp))
    return
  }

  if (action === 't2') {
    var parts2 = String(arg || '').trim().split(' ')
    var verb2 = parts2[0]
    if (verb2 === 'start') {
      var rc2 = 6
      try {
        var a2 = parseInt(parts2[1], 10)
        if (!isNaN(a2) && a2 > 0) rc2 = a2
      } catch (eR2) { }
      if (rc2 > 12) rc2 = 12          
      mcbOk(event, 't2', mcbT2Start(player, rc2))
      return
    }
    if (verb2 === 'status') { mcbOk(event, 't2', mcbT2Status()); return }
    if (verb2 === 'get') { mcbOk(event, 't2', mcbT2Get()); return }
    if (verb2 === 'stop') { mcbOk(event, 't2', mcbT2Stop()); return }
    mcbOk(event, 't2', { err: '用法: mcb t2 start [chunk数] | status | get | stop' })
    return
  }

  if (action === 'threats') {    var tr = 24
    try { tr = parseInt(arg, 10) } catch (e) { tr = 24 }
    if (isNaN(tr) || tr < 2) tr = 24
    if (tr > 64) tr = 64
    mcbOk(event, 'threats', mcbThreats(player, tr))
    return
  }

  if (action === 'attackTarget') {
    mcbOk(event, 'attackTarget', mcbAttackTarget(player))
    return
  }

  if (action === 'blockinfo') {
    var ba = String(arg || '').split(' ')
    var bx3 = parseInt(ba[0], 10), by3 = parseInt(ba[1], 10), bz3 = parseInt(ba[2], 10)
    if (isNaN(bx3) || isNaN(by3) || isNaN(bz3)) { mcbErr(event, 'blockinfo', '要三个整数坐标'); return }
    mcbOk(event, 'blockinfo', mcbBlockInfo(player, bx3, by3, bz3))
    return
  }

  if (action === 'scanprobe') {
    mcbOk(event, 'scanprobe', mcbScanProbe(player, arg))
    return
  }

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

  // closeGui 必须服务端和客户端同时关闭，否则窗口 ID 脱节。
  if (action === 'closeGui') {
    var closedId = -1
    var how = 'none'
    try { closedId = Number(player.containerMenu.containerId) } catch (eId) { }

    // 仅关闭服务端不会通知客户端，两边必须归位。
    // 跨版本关闭包存在构造差异，保留两种签名兜底。
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

    // 此映射服务端使用 doCloseContainer，closeContainer 不存在。
    try {
      player.doCloseContainer()
      how = 'doCloseContainer'
    } catch (eC2) {
      console.error('[mcb] 服务端关闭容器失败: ' + eC2)
    }

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
      player.sendData(MCB_CHANNEL_DOWN, { action: 'walk', arg: JSON.stringify(intent) })
      mcbWalkIntent = { token: intent.token, owner: intent.owner }
      mcbOk(event, action, { sent: true, token: intent.token })
    } catch (eWalk) { mcbErr(event, action, '寻路下发失败: ' + eWalk) }
    return
  }

  if (action === 'receipt') {
    var foundReceipt = mcbSayReceipts[arg]
    mcbOk(event, action, foundReceipt || { state: 'unknown' })
    return
  }

  if (action === 'say') {
    if (mcbReplyToken === null) {
      mcbErr(event, action, '说话需要关联请求标识，未下发')
      return
    }
    var sayId = mcbReplyToken
    mcbSayReceipts[sayId] = { state: 'sent', action: 'say', error: '' }
    mcbSayOrder.push(sayId)
    while (mcbSayOrder.length > 128) delete mcbSayReceipts[mcbSayOrder.shift()]
    try {
      player.sendData(MCB_CHANNEL_DOWN, { action: 'say', arg: arg, receiptId: sayId })
      mcbOk(event, action, { sent: true, receiptId: sayId })
    } catch (sayError) {
      mcbSayReceipts[sayId].state = 'failed'
      mcbErr(event, action, '说话下发失败: ' + sayError)
    }
    return
  }

  try {
    player.sendData(MCB_CHANNEL_DOWN, { action: action, arg: arg })
    if (action === 'baritone' || action === 'stop') mcbWalkIntent = null
    console.info('[mcb] 已下发 action=' + action + ' -> ' + MCB_TARGET)
    // sent 仅代表下发；成功须复核世界状态。
    mcbOk(event, action, { sent: true, target: MCB_TARGET })
  } catch (e) {
    console.error('[mcb] sendData 失败: ' + e)
    mcbErr(event, action, '下发失败: ' + e)
  }
})

console.info('[mcb] 服务端脚本已加载（统一信封 v5 + 状态上行）')
