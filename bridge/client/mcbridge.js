// 命中候选与槽位分类存在 Numen 实现对应；来源与许可待核项见仓库根 THIRD_PARTY_NOTICES.md。
// 交互走 mc.gameMode，由原版维护窗口、stateId 和 sequence。
// player.gameMode 在此映射为 null，使用 mc.gameMode。
// Rhino 禁用 cls 变量名、函数内 const/let 和 Java getClass 反射。

const MCB_CHANNEL_DOWN = 'mcbridge'
const MCB_CHANNEL_UP = 'mcbridge_up'
const MCB_UPLINK_EVERY_TICKS = 10   
const USE_HOLD_TICKS = 60           
const USE_HOLD_MARGIN = 20          

const MCB_TEMP_LOOK_TICKS = 30      
const MCB_LOOK_PITCH_MAX = 45       
const MCB_IDLE_YAW_SPAN = 35        
const MCB_IDLE_PITCH_SPAN = 15      
const MCB_IDLE_HOLD_MIN = 80        
const MCB_IDLE_HOLD_MAX = 140       
const MCB_IDLE_STEP_YAW = 5         
const MCB_IDLE_STEP_PITCH = 3
const MCB_IDLE_NEAR_PLAYER = 8      
const MCB_IDLE_LOOK_AT_PLAYER = 0.6 

// aim 带最长 TTL，防止调用方忘记 release 后永久锁头。
const MCB_AIM_MAX_HOLD_TICKS = 2400

let $BaritoneAPI = null
try {
  $BaritoneAPI = Java.loadClass('baritone.api.BaritoneAPI')
  console.info('[mcbridge] BaritoneAPI 类可见: ' + $BaritoneAPI)
} catch (e) {
  console.error('[mcbridge] BaritoneAPI 类不可见: ' + e)
}

let $Minecraft = null

// updateTarget 仅生效一个 tick，持续注视必须逐 tick 重设。
// updateTarget 的 true 参数驱动本地镜头，false 可能仅静默转服务端朝向。
let $RotationCls = null
try {
  $RotationCls = Java.loadClass('baritone.api.utils.Rotation')
} catch (eRot) {
  console.error('[mcbridge] Rotation 类不可见: ' + eRot)
}

// LOOK_TARGET 与 WALK_TARGET 分通道处理。

var mcbSlots = {
  // temp 到期自动释放，防止交互俯角残留。
  // 无声明时需空闲朝向兜底，避免残留上次交互方向。
  aim: null,
  temp: null,        
  idle: {            
    on: true,
    tyaw: null, tpitch: 0,   
    cyaw: 0, cpitch: 0,      
    nextAt: 0                
  },
  use: { ticks: 0, sawUsing: false },
  // 手持槽只维持选中，不搬物品，避免干扰界面操作。
  hotbar: { want: null }
}

// 朝向优先级 temp > aim > idle；无声明则交还 Baritone。
function mcbLookClaim() {
  if (mcbSlots.temp !== null) {
    if ($mcbTick < mcbSlots.temp.until) return mcbSlots.temp
    mcbSlots.temp = null
  }
  if (mcbSlots.aim !== null) {
    // aim 到期释放，不能完全依赖调用方清理。
    if ($mcbTick < mcbSlots.aim.until) return mcbSlots.aim
    mcbSlots.aim = null
    console.info('[mcbridge] 持续注视到最长保持时间，自动松开（调用方没 releaseAim）')
  }
  if (mcbSlots.idle.on) return mcbIdleClaim()
  return null
}

function mcbRotOf(claim) {
  if (claim === null) return null
  if (claim.mode === 'angle') return { yaw: claim.yaw, pitch: claim.pitch }
  var mc = mcbMc()
  if (mc === null || mc.player === null) return null
  var p = mc.player
  var ex = Number(p.x), ey = Number(p.y) + Number(p.eyeHeight), ez = Number(p.z)
  var dx = claim.x - ex, dy = claim.y - ey, dz = claim.z - ez
  var horiz = Math.sqrt(dx * dx + dz * dz)
  var pitch = -Math.atan2(dy, horiz) * 180 / Math.PI
  var cap = claim.pitchMax
  if (typeof cap === 'number') {
    if (pitch > cap) pitch = cap
    if (pitch < -cap) pitch = -cap
  }
  // MC yaw: 0=+Z；用 atan2(-dx,dz) 换算。
  return { yaw: Math.atan2(-dx, dz) * 180 / Math.PI, pitch: pitch }
}

function mcbAimRotation() {
  return mcbRotOf(mcbLookClaim())
}

// yaw 按最短角差插值，跨 ±180 时不能直接相减。
function mcbTurnToward(cur, target, step) {
  var d = target - cur
  while (d > 180) d = d - 360
  while (d < -180) d = d + 360
  if (d > step) d = step
  if (d < -step) d = -step
  return cur + d
}

// 跟随原地等待算空闲，移动中的跟随已由 isPathing 覆盖。
function mcbBaritoneBusy() {
  if ($BaritoneAPI === null) return false
  var bar = mcbPrimary()
  if (bar === null) return false
  try { if (bar.getPathingBehavior().isPathing()) return true } catch (e1) { }
  try { if (bar.getCustomGoalProcess().isActive()) return true } catch (e2) { }
  try { if (bar.getMineProcess().isActive()) return true } catch (e3) { }
  try { if (bar.getFarmProcess().isActive()) return true } catch (e4) { }
  try { if (bar.getBuilderProcess().isActive()) return true } catch (e5) { }
  try { if (bar.getExploreProcess().isActive()) return true } catch (e6) { }
  return false
}

function mcbNearbyPlayer() {
  try {
    var mc = mcbMc()
    if (mc === null || mc.player === null || mc.level === null) return null
    var list = mc.level.players()
    if (list === null || list === undefined) return null
    var me = mc.player
    var best = null, bd = MCB_IDLE_NEAR_PLAYER * MCB_IDLE_NEAR_PLAYER
    var n = list.size()
    for (var i = 0; i < n; i++) {
      var e = list.get(i)
      if (e === null) continue
      try { if (String(e.uuid) === String(me.uuid)) continue } catch (eUuid) { }
      var dx = Number(e.x) - Number(me.x)
      var dy = Number(e.y) - Number(me.y)
      var dz = Number(e.z) - Number(me.z)
      var d = dx * dx + dy * dy + dz * dz
      if (d < bd) { bd = d; best = e }
    }
    return best
  } catch (e) { return null }
}

function mcbIdleClaim() {
  var id = mcbSlots.idle
  if (mcbBaritoneBusy()) { id.tyaw = null; return null }

  var me = mcbPlayerYawPitch()
  var curYaw = me === null ? 0 : me.yaw
  var curPitch = me === null ? 0 : me.pitch

  if (id.tyaw === null) {
    // 进入空闲从当前朝向起算，避免跳变。
    id.cyaw = curYaw
    id.cpitch = curPitch
  }
  if (id.tyaw === null || $mcbTick >= id.nextAt) {
    var who = mcbNearbyPlayer()
    if (who !== null && Math.random() < MCB_IDLE_LOOK_AT_PLAYER) {
      var t = mcbRotOf({
        mode: 'point',
        x: Number(who.x), y: Number(who.y) + 1.6, z: Number(who.z),
        pitchMax: MCB_IDLE_PITCH_SPAN
      })
      if (t !== null) { id.tyaw = t.yaw; id.tpitch = t.pitch }
    } else {
      id.tyaw = curYaw + (Math.random() * 2 - 1) * MCB_IDLE_YAW_SPAN
      id.tpitch = (Math.random() * 2 - 1) * MCB_IDLE_PITCH_SPAN
    }
    // 角度计算失败时兜底，不能让 null 进入插值变成 NaN。
    if (id.tyaw === null) { id.tyaw = curYaw; id.tpitch = 0 }
    id.nextAt = $mcbTick + MCB_IDLE_HOLD_MIN +
      Math.floor(Math.random() * (MCB_IDLE_HOLD_MAX - MCB_IDLE_HOLD_MIN))
  }
  id.cyaw = mcbTurnToward(id.cyaw, id.tyaw, MCB_IDLE_STEP_YAW)
  id.cpitch = mcbTurnToward(id.cpitch, id.tpitch, MCB_IDLE_STEP_PITCH)
  return { mode: 'angle', yaw: id.cyaw, pitch: id.cpitch }
}

function mcbLookTemp(claim) {
  claim.until = $mcbTick + MCB_TEMP_LOOK_TICKS
  mcbSlots.temp = claim
  mcbSlots.idle.tyaw = null      
  mcbSetAntiCheat(false)
  mcbAimApply()
}

function mcbLookTempAt(x, y, z, pitchMax) {
  mcbLookTemp({ mode: 'point', x: x, y: y, z: z, pitchMax: pitchMax })
}

var mcbLastUsePos = null

// 交互落点采用三档候选。
// 不规则形状和侧面交互不能固定点顶面中心。
// point 必须落在 face 上，否则服务端可能拒绝交互。
function mcbPickHit(mc, bp, state) {
  var $V3 = null, $DIR = null
  try {
    $V3 = Java.loadClass('net.minecraft.world.phys.Vec3')
    $DIR = Java.loadClass('net.minecraft.core.Direction')
  } catch (e0) {
    return { point: null, face: null, via: 'unavailable', blocked: null }
  }
  if ($V3 === null || $DIR === null) {
    return { point: null, face: null, via: 'unavailable', blocked: null }
  }

  var p = mc.player
  var ex = Number(p.x), ey = Number(p.y) + Number(p.eyeHeight), ez = Number(p.z)
  var reach = 4.5
  try { reach = Number(p.blockInteractionRange) } catch (eR) { }

  // shape.bounds() 是局部坐标，必须加方块原点。
  var bx = Number(bp.getX()), by = Number(bp.getY()), bz = Number(bp.getZ())
  var minX = bx, minY = by, minZ = bz
  var maxX = bx + 1, maxY = by + 1, maxZ = bz + 1
  try {
    var shape = state.getShape(mc.level, bp)
    var bb = shape.bounds()
    if (bb !== null && bb !== undefined) {
      minX = bx + Number(bb.minX); minY = by + Number(bb.minY); minZ = bz + Number(bb.minZ)
      maxX = bx + Number(bb.maxX); maxY = by + Number(bb.maxY); maxZ = bz + Number(bb.maxZ)
    }
  } catch (eShape) { }

  var cxm = (minX + maxX) / 2, cym = (minY + maxY) / 2, czm = (minZ + maxZ) / 2

  var faces = []
  try {
    var all = [$DIR.UP, $DIR.DOWN, $DIR.NORTH, $DIR.SOUTH, $DIR.EAST, $DIR.WEST]
    for (var i = 0; i < all.length; i++) faces.push(all[i])
  } catch (eD) { faces = [] }
  var dx = ex - cxm, dy = ey - cym, dz = ez - czm
  faces.sort(function (a, b) {
    function score(d) {
      var n = d.getNormal()
      return -(Number(n.x) * dx + Number(n.y) * dy + Number(n.z) * dz)
    }
    return score(a) - score(b)
  })

  function ok(pt) {
    // NaN > reach 为 false，距离必须先验证为数值。
    var px = Number(pt.x), py = Number(pt.y), pz = Number(pt.z)
    if (isNaN(px) || isNaN(py) || isNaN(pz)) return false
    var ddx = px - ex, ddy = py - ey, ddz = pz - ez
    if (Math.sqrt(ddx * ddx + ddy * ddy + ddz * ddz) > reach) return false
    return mcbVisible(mc, ex, ey, ez, pt, bp)
  }

  var cand = $V3.atCenterOf(bp)
  try { cand = new $V3(cxm, cym, czm) } catch (eC) { }
  // 中心点配 UP 面自相矛盾；命中点必须在所选面上。
  var facing = faces.length > 0 ? faces[0] : $DIR.UP
  var c1 = mcbFacePoint(facing, minX, minY, minZ, maxX, maxY, maxZ, 0.5)
  if (ok(c1)) return { point: c1, face: facing, via: 'facing-center', blocked: null }

  for (var k = 1; k < faces.length; k++) {
    var fc = mcbFacePoint(faces[k], minX, minY, minZ, maxX, maxY, maxZ, 0.5)
    if (ok(fc)) return { point: fc, face: faces[k], via: 'face-center', blocked: null }
  }

  var c3 = mcbFacePoint(facing, minX, minY, minZ, maxX, maxY, maxZ, null, ex, ey, ez)
  if (ok(c3)) return { point: c3, face: facing, via: 'facing-nearest', blocked: null }

  // 兜底仍须保证命中点落在面上。
  var fb = mcbFacePoint($DIR.UP, minX, minY, minZ, maxX, maxY, maxZ, 0.5)
  return { point: fb, face: $DIR.UP, via: 'fallback-top', blocked: null }
}

function mcbFacePoint(face, minX, minY, minZ, maxX, maxY, maxZ, ratio, ex, ey, ez) {
  var $V3 = Java.loadClass('net.minecraft.world.phys.Vec3')
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v) }
  function axis(d) {
    var n = d.getNormal()
    if (Number(n.x) > 0) return 'east'
    if (Number(n.x) < 0) return 'west'
    if (Number(n.y) > 0) return 'up'
    if (Number(n.y) < 0) return 'down'
    if (Number(n.z) > 0) return 'south'
    return 'north'
  }
  var a = axis(face)
  var x, y, z
  if (a === 'east') { x = maxX; y = ratio === null ? clamp(ey, minY, maxY) : (minY + maxY) / 2; z = ratio === null ? clamp(ez, minZ, maxZ) : (minZ + maxZ) / 2 }
  else if (a === 'west') { x = minX; y = ratio === null ? clamp(ey, minY, maxY) : (minY + maxY) / 2; z = ratio === null ? clamp(ez, minZ, maxZ) : (minZ + maxZ) / 2 }
  else if (a === 'up') { y = maxY; x = ratio === null ? clamp(ex, minX, maxX) : (minX + maxX) / 2; z = ratio === null ? clamp(ez, minZ, maxZ) : (minZ + maxZ) / 2 }
  else if (a === 'down') { y = minY; x = ratio === null ? clamp(ex, minX, maxX) : (minX + maxX) / 2; z = ratio === null ? clamp(ez, minZ, maxZ) : (minZ + maxZ) / 2 }
  else if (a === 'south') { z = maxZ; x = ratio === null ? clamp(ex, minX, maxX) : (minX + maxX) / 2; y = ratio === null ? clamp(ey, minY, maxY) : (minY + maxY) / 2 }
  else { z = minZ; x = ratio === null ? clamp(ex, minX, maxX) : (minX + maxX) / 2; y = ratio === null ? clamp(ey, minY, maxY) : (minY + maxY) / 2 }
  return new $V3(x, y, z)
}

// 槽位角色整表发送，包含空槽，否则无法识别可摆料位置。
// 按槽位对象类型而非界面类名推断布局。
// 先判 ResultSlot，再判 CraftingContainer，避免产物误归输入。
var mcbRoleClasses = null

function mcbSlotRole(sl, player) {
  if (sl === null || sl === undefined) return '?'
  if (mcbRoleClasses === null) {
    var c = { ok: false }
    try {
      c.ResultSlot = Java.loadClass('net.minecraft.world.inventory.ResultSlot')
      c.Crafting = Java.loadClass('net.minecraft.world.inventory.CraftingContainer')
      c.Inventory = Java.loadClass('net.minecraft.world.entity.player.Inventory')
      c.Stack = Java.loadClass('net.minecraft.world.item.ItemStack')
      c.ok = true
    } catch (eLoad) { c.ok = false }
    mcbRoleClasses = c
  }
  var C = mcbRoleClasses
  if (!C.ok) return '?'
  try {
    if (sl instanceof C.ResultSlot) return 'R'
    var cont = null
    try { cont = sl.container } catch (e1) { try { cont = sl.getContainer() } catch (e2) { cont = null } }
    if (cont !== null && cont !== undefined) {
      if (cont instanceof C.Inventory) return 'Y'
      if (cont instanceof C.Crafting) return 'G'
    }
    var canPlace = true
    try { canPlace = !!sl.mayPlace(C.Stack.EMPTY) } catch (e3) { canPlace = true }
    if (!canPlace) return 'O'
    return 'C'
  } catch (e4) {
    return '?'
  }
}

// Rhino Java 方法需字面量调用，动态属性名不可靠。
function mcbProbe1(f) {
  try {
    var v = f()
    if (v === null || v === undefined) return 'null'
    return typeof v + ':' + String(v).substring(0, 60)
  } catch (e) {
    return 'throw:' + String(e).substring(0, 60)
  }
}

function mcbVisible(mc, ex, ey, ez, pt, bp) {
  try {
    var $V3 = Java.loadClass('net.minecraft.world.phys.Vec3')
    var $CC = Java.loadClass('net.minecraft.world.phys.ClipContext')
    var $Blk = Java.loadClass('net.minecraft.world.level.ClipContext$Block')
    var $Flu = Java.loadClass('net.minecraft.world.level.ClipContext$Fluid')
    var from = new $V3(ex, ey, ez)
    var ctx = new $CC(from, pt, $Blk.OUTLINE, $Flu.NONE, mc.player)
    var res = mc.level.clip(ctx)
    if (res === null || res === undefined) return true
    var hitPos = null
    try { hitPos = res.getBlockPos() } catch (eP) { try { hitPos = res.blockPos } catch (eP2) { } }
    if (hitPos === null || hitPos === undefined) return true        
    return (Number(hitPos.getX()) === Number(bp.getX())
         && Number(hitPos.getY()) === Number(bp.getY())
         && Number(hitPos.getZ()) === Number(bp.getZ()))
  } catch (eV) {
    return true
  }
}

function mcbAimApply() {
  var rot = mcbAimRotation()
  if (rot === null) return false
  mcbAimPush(rot)
  return true
}

// 朝向声明生效时关闭 antiCheatCompatibility，无声明时恢复，避免与 Baritone 抢头。
let $mcbAntiCheat = true      
function mcbSetAntiCheat(on) {
  if ($BaritoneAPI === null) return
  on = !!on
  if ($mcbAntiCheat === on) return
  $mcbAntiCheat = on
  try {
    $BaritoneAPI.getSettings().antiCheatCompatibility.value = on
  } catch (e) {
    console.error('[mcbridge] antiCheatCompatibility 设不了: ' + e)
  }
}

let $mcbAimErrReported = false

// 字段可能返回 undefined 而不抛异常，需数值校验及视线向量兜底。
function mcbPlayerYawPitch() {
  var p = null
  try {
    var mc = mcbMc()
    if (mc !== null && mc.player !== null) p = mc.player
  } catch (e0) { }
  if (p === null) return null
  var yaw = null, pitch = null
  try { var v1 = Number(p.yRot); if (!isNaN(v1)) yaw = v1 } catch (e1) { }
  try { var v2 = Number(p.xRot); if (!isNaN(v2)) pitch = v2 } catch (e2) { }
  if (yaw === null || pitch === null) {
    try { var v3 = Number(p.getYRot()); if (!isNaN(v3)) yaw = v3 } catch (e3) { }
    try { var v4 = Number(p.getXRot()); if (!isNaN(v4)) pitch = v4 } catch (e4) { }
  }
  if (yaw === null || pitch === null) {
    try {
      var look = p.getViewVector(1.0)
      var lx = Number(look.x), ly = Number(look.y), lz = Number(look.z)
      if (!isNaN(lx) && !isNaN(ly) && !isNaN(lz)) {
        if (pitch === null) pitch = -Math.asin(Math.max(-1, Math.min(1, ly))) * 180 / Math.PI
        if (yaw === null) yaw = Math.atan2(-lx, lz) * 180 / Math.PI
      }
    } catch (e5) { }
  }
  if (yaw === null || pitch === null) return null
  return { yaw: yaw, pitch: pitch }
}

var mcbBaritoneTuned = false
function mcbTuneBaritone() {
  if (mcbBaritoneTuned || $BaritoneAPI === null) return
  mcbBaritoneTuned = true
  try {
    var s = $BaritoneAPI.getSettings()
    // 关闭 Baritone 随机抖动，避免污染目标朝向。
    try { s.randomLooking.value = 0 } catch (e1) { console.error('[mcbridge] randomLooking 设不了: ' + e1) }
    try { s.randomLooking113.value = 0 } catch (e2) { console.error('[mcbridge] randomLooking113 设不了: ' + e2) }
    try { s.remainWithExistingLookDirection.value = false } catch (e3) { console.error('[mcbridge] remainWithExisting... 设不了: ' + e3) }
    // antiCheatCompatibility 由槽状态逐 tick 控制，不能固定关闭。

    var got = []
    try { got.push('freeLook=' + s.freeLook.value) } catch (e5) { }
    try { got.push('blockFreeLook=' + s.blockFreeLook.value) } catch (e6) { }
    try { got.push('antiCheat=' + s.antiCheatCompatibility.value) } catch (e7) { }
    try { got.push('randomLooking=' + s.randomLooking.value) } catch (e8) { }
    try { got.push('randomLooking113=' + s.randomLooking113.value) } catch (e9) { }
    try { got.push('remainWithExisting=' + s.remainWithExistingLookDirection.value) } catch (e10) { }
    console.info('[mcbridge] Baritone 朝向相关设置已调 -> ' + got.join(' '))
  } catch (e) {
    console.error('[mcbridge] 调 Baritone 设置失败: ' + e)
  }
}
mcbTuneBaritone()
try {
  $Minecraft = Java.loadClass('net.minecraft.client.Minecraft')
} catch (e) {
  console.error('[mcbridge] Minecraft 类不可见: ' + e)
}

// 交互使用 mc.gameMode，player.gameMode 在此映射不可用。
let $Hand = null
try {
  $Hand = Java.loadClass('net.minecraft.world.InteractionHand')
} catch (e) {
  console.error('[mcbridge] InteractionHand 不可见: ' + e)
}


function mcbStr(tag, key) {
  if (tag === null || tag === undefined) return ''
  try {
    var v = tag.getString(key)
    if (v !== null && v !== undefined) return String(v)
  } catch (e) {
  }
  try {
    var w = tag[key]
    if (w !== null && w !== undefined) return String(w)
  } catch (e2) {
  }
  return ''
}

function mcbPrimary() {
  if ($BaritoneAPI === null) {
    console.error('[mcbridge] BaritoneAPI 不可用')
    return null
  }
  try {
    return $BaritoneAPI.getProvider().getPrimaryBaritone()
  } catch (e) {
    console.error('[mcbridge] 拿 primaryBaritone 失败: ' + e)
    return null
  }
}

function mcbMc() {
  if ($Minecraft === null) return null
  try {
    return $Minecraft.getInstance()
  } catch (e) {
    return null
  }
}

// 未知数值返回 null，不能用 0 冒充。
function mcbNum(v) {
  try {
    if (v === null || v === undefined) return null
    var n = Number(v)
    return isNaN(n) ? null : Math.round(n * 100) / 100
  } catch (e) {
    return null
  }
}


function mcbSnapshot() {
  var snap = {}

  var b = mcbPrimary()
  if (b === null) {
    snap.status = 'unknown'
    snap.note = 'BaritoneAPI 拿不到'
    return snap
  }

  var pathing = false
  try {
    pathing = !!b.getPathingBehavior().isPathing()
  } catch (e) {
    snap.note = 'isPathing 失败: ' + e
  }

  var following = false
  try {
    var fol = b.getFollowProcess().following()
    following = (fol !== null && fol !== undefined && Number(fol.size()) > 0)
  } catch (e2) {
  }

  if (pathing) {
    snap.status = 'moving'
  } else if (following) {
    snap.status = 'following-idle'   
  } else {
    snap.status = 'idle'
  }

  try {
    var g = b.getCustomGoalProcess().getGoal()
    if (g !== null && g !== undefined) snap.goal = String(g)
  } catch (e3) {
  }

  try {
    var opt = b.getPathingBehavior().estimatedTicksToGoal()
    if (opt !== null && opt !== undefined) {
      var present = false
      try {
        present = !!opt.isPresent()
      } catch (e4) {
        present = false
      }
      if (present) {
        var ticks = Number(opt.get())
        if (!isNaN(ticks)) snap.eta = Math.round(ticks / 20)   
      }
    }
  } catch (e5) {
  }

  var mc = mcbMc()
  try {
    if (mc !== null && mc.player !== null) {
      snap.posX = mcbNum(mc.player.x)
      snap.posY = mcbNum(mc.player.y)
      snap.posZ = mcbNum(mc.player.z)
      // 客户端镜头与服务端朝向分开上报；SERVER 模式可能仅静默转头。
      snap.yaw = null
      snap.pitch = null
      var yp = mcbPlayerYawPitch()
      if (yp !== null) { snap.yaw = mcbNum(yp.yaw); snap.pitch = mcbNum(yp.pitch) }
    }
  } catch (e6) {
  }

  // 默认菜单仅报合成格，避免每次发送整个背包。
  try {
    if (mc !== null && mc.player !== null) {
      var menu = mc.player.containerMenu
      if (menu !== null && menu !== undefined) {
        var mId = -1, mSlots = -1
        try { mId = Number(menu.containerId) } catch (eM1) { }
        try { mSlots = Number(menu.slots.size()) } catch (eM2) { }
        var isDefault = (mId === 0)
        var cells = []
        var itemRegistry = Java.loadClass('net.minecraft.core.registries.BuiltInRegistries').ITEM
        try {
          var lo = 0
          var hi = isDefault ? 5 : mSlots
          for (var si = lo; si < hi; si++) {
            var sl = menu.slots.get(si)
            var st = sl.getItem()
            var cc = 0
            try { cc = Number(st.getCount()) } catch (eC1) { cc = 0 }
            if (!(cc > 0)) continue
            var nn = null
            try { nn = String(st.hoverName.string) } catch (eN1) { nn = null }
            cells.push({ i: si, n: nn, c: cc, id: String(itemRegistry.getKey(st.getItem())) })
          }
        } catch (eL) { }
        // 菜单必须带类型；仅凭格数分不出背包和工作台。
        var mcls = null
        try { mcls = String(menu) } catch (eCls) { mcls = null }
        var mroles = ''
        try {
          var rbuf = []
          for (var ri = 0; ri < mSlots; ri++) {
            rbuf.push(mcbSlotRole(menu.slots.get(ri), mc.player))
          }
          mroles = rbuf.join('')
        } catch (eRole) { mroles = '' }
        // 菜单先序列化 JSON，避免 CompoundTag 嵌套列表转换歧义。
        snap.menu = JSON.stringify({ id: mId, slots: mSlots, def: isDefault, cls: mcls,
                                     items: cells, roles: mroles })
      }
    }
  } catch (eMenu) { }

  return snap
}

function mcbSendUplink(reason) {
  var mc = mcbMc()
  if (mc === null || mc.player === null) return false
  var snap = mcbSnapshot()
  snap.reason = reason
  try {
    mc.player.sendData(MCB_CHANNEL_UP, snap)
    return true
  } catch (e) {
    console.error('[mcbridge] 上行失败: ' + e)
    return false
  }
}


function mcbProbe() {
  var out = []

  var mc = mcbMc()
  out.push('mc=' + (mc !== null && mc !== undefined))
  if (mc === null || mc === undefined) {
    out.push('$Minecraft 加载失败或 getInstance 拿不到')
    return out.join(' | ')
  }

  var p = null
  try {
    p = mc.player
  } catch (e) {
    out.push('mc.player ERR ' + e)
  }
  out.push('player=' + (p !== null && p !== undefined))
  if (p === null || p === undefined) {
    out.push('没进世界')
    return out.join(' | ')
  }

  try {
    var gm1 = p.gameMode
    out.push('player.gameMode=' + (gm1 === null || gm1 === undefined ? 'null' : 'OK'))
  } catch (e1) {
    out.push('player.gameMode ERR ' + e1)
  }
  try {
    var gm2 = mc.gameMode
    out.push('mc.gameMode=' + (gm2 === null || gm2 === undefined ? 'null' : 'OK'))
  } catch (e2) {
    out.push('mc.gameMode ERR ' + e2)
  }
  try {
    var gm = (p.gameMode !== null && p.gameMode !== undefined) ? p.gameMode : mc.gameMode
    if (gm !== null && gm !== undefined) {
      var names = ['useItem', 'useItemOn', 'interact', 'handleInventoryMouseClick']
      var hit = []
      for (var i = 0; i < names.length; i++) {
        var f = null
        try {
          f = gm[names[i]]
        } catch (e3) {
          f = null
        }
        hit.push(names[i] + '=' + (typeof f === 'function' ? 'fn' : String(f)))
      }
      out.push('gameMode 方法: ' + hit.join(','))
    }
  } catch (e4) {
    out.push('gameMode 方法探测 ERR ' + e4)
  }

  try {
    var r = p.pick(4.5, 0.0, false)
    out.push('player.pick=' + (r === null || r === undefined ? 'null' : String(r)))
  } catch (e5) {
    out.push('player.pick ERR ' + e5)
  }

  try {
    out.push('player.sendData=' + typeof p.sendData)
  } catch (e6) {
    out.push('player.sendData ERR ' + e6)
  }

  var b = mcbPrimary()
  if (b === null) {
    out.push('baritone=null')
    return out.join(' | ')
  }
  try {
    out.push('isPathing=' + b.getPathingBehavior().isPathing())
  } catch (e7) {
    out.push('isPathing ERR ' + e7)
  }
  try {
    var g = b.getCustomGoalProcess().getGoal()
    out.push('goal=' + (g === null || g === undefined ? 'null' : String(g)))
  } catch (e8) {
    out.push('goal ERR ' + e8)
  }
  try {
    var opt = b.getPathingBehavior().estimatedTicksToGoal()
    if (opt === null || opt === undefined) {
      out.push('eta=null')
    } else {
      var present = false
      try {
        present = !!opt.isPresent()
      } catch (e9) {
        present = false
      }
      out.push('eta.present=' + present + (present ? ' val=' + opt.get() : ''))
    }
  } catch (e10) {
    out.push('eta ERR ' + e10)
  }

  return out.join(' | ')
}


var mcbWalkBreakBefore = null

function mcbRestoreWalkBreak() {
  if (mcbWalkBreakBefore === null) return
  $BaritoneAPI.getSettings().allowBreak.value = mcbWalkBreakBefore
  mcbWalkBreakBefore = null
}

function mcbHandle(action, arg) {
  if (action === 'walk') {
    var walkIntent = JSON.parse(arg)
    var walkBaritone = mcbPrimary()
    if (walkBaritone === null) throw new Error('walk: Baritone不可用')
    walkBaritone.getPathingBehavior().cancelEverything()
    mcbRestoreWalkBreak()
    if (walkIntent.noBreak === true) {
      mcbWalkBreakBefore = $BaritoneAPI.getSettings().allowBreak.value
      $BaritoneAPI.getSettings().allowBreak.value = false
    }
    try {
      walkBaritone.getCommandManager().execute(walkIntent.cmd)
    } catch (walkError) {
      mcbRestoreWalkBreak()
      throw walkError
    }
    console.info('[mcbridge] walk noBreak=' + (walkIntent.noBreak === true)
      + ' allowBreak=' + $BaritoneAPI.getSettings().allowBreak.value)
    return
  }
  if (action === 'ping') {
    console.info('[mcbridge] PONG（客户端活着）')
    return
  }

  if (action === 'say') {
    var mc = mcbMc()
    if (mc === null || mc.player === null) {
      throw new Error('say: 没进世界')
    }
    mc.player.connection.sendChat(arg)
    console.info('[mcbridge] 已说话: ' + arg)
    return
  }

  if (action === 'stop') {
    var b = mcbPrimary()
    if (b === null) return
    b.getPathingBehavior().cancelEverything()
    mcbRestoreWalkBreak()
    console.info('[mcbridge] 已取消所有寻路')
    return
  }

  if (action === 'baritone') {
    var b2 = mcbPrimary()
    if (b2 === null) return
    if (mcbWalkBreakBefore !== null) {
      b2.getPathingBehavior().cancelEverything()
      mcbRestoreWalkBreak()
    }
    b2.getCommandManager().execute(arg)
    console.info('[mcbridge] 已执行 baritone: "' + arg + '"')
    return
  }

  // 交互一律走 gameMode，不能裸发需要 stateId/sequence 的包。

  if (action === 'hotbar') {
    var mcH = mcbMc()
    if (mcH === null || mcH.player === null) { console.error('[mcbridge] hotbar: 没进世界'); return }
    var slot = parseInt(arg, 10)
    if (isNaN(slot) || slot < 0 || slot > 8) { console.error('[mcbridge] hotbar 槽位非法: ' + arg); return }
    mcbSlots.hotbar.want = { slot: slot }
    try { mcH.player.inventory.selected = slot } catch (eH2) { }
    console.info('[mcbridge] 快捷栏 -> ' + slot + '（已进 hotbar 槽，会维持）')
    return
  }

  if (action === 'hotbarItem') {
    // 客户端不逐 tick 搬物品，避免扰乱容器操作。
    var mcHI = mcbMc()
    if (mcHI === null || mcHI.player === null) { console.error('[mcbridge] hotbarItem: 没进世界'); return }
    var wantId = String(arg || '').trim()
    if (!wantId) { console.error('[mcbridge] hotbarItem: 没给物品注册名'); return }
    mcbSlots.hotbar.want = { item: wantId }
    console.info('[mcbridge] hotbar 槽 -> 要手上是 ' + wantId + '（找不到就不动，等插件侧搬）')
    return
  }

  if (action === 'releaseHotbar') {
    mcbSlots.hotbar.want = null
    console.info('[mcbridge] hotbar 槽已松开')
    return
  }

  if (action === 'use') {
    // 禁止循环重发 useItem，否则服务端倒计时不断重置。
    // 按住原版使用键维持动作，交给原版节流。
    var mcU = mcbMc()
    if (mcU === null || mcU.player === null || $Hand === null) { console.error('[mcbridge] use: 不可用'); return }

    var dur = 0
    try {
      var held = mcU.player.getMainHandItem()
      try { dur = Number(held.getUseDuration(mcU.player)) } catch (eD1) {
        try { dur = Number(held.getUseDuration()) } catch (eD2) { dur = 0 }
      }
    } catch (eHd) { dur = 0 }
    if (!(dur > 0)) dur = 0

    try { mcU.gameMode.useItem(mcU.player, $Hand.MAIN_HAND) } catch (eUi) {
      console.error('[mcbridge] useItem 失败: ' + eUi)
      return
    }

    var keyOk = 'no'
    if (dur > 0) {
      try {
        mcU.options.keyUse.setDown(true)
        keyOk = 'yes'
        // 安全上限跟随物品 getUseDuration，不能写死短时长。
        mcbSlots.use.ticks = Math.max(USE_HOLD_TICKS, dur + USE_HOLD_MARGIN)
      } catch (eK) {
        console.error('[mcbridge] keyUse.setDown(true) 失败: ' + eK)
      }
    }
    var nowUsing = false
    try { nowUsing = !!mcU.player.isUsingItem() } catch (eU) { }
    console.info(
      '[mcbridge] 已 useItem（dur=' + dur + ' 按住上限=' + mcbSlots.use.ticks
      + ' isUsingItem=' + nowUsing + ' 按键按下=' + keyOk + '）'
    )
    return
  }

  if (action === 'release') {
    try {
      var mcRel = mcbMc()
      if (mcRel !== null) { mcRel.options.keyUse.setDown(false); console.info('[mcbridge] 使用键已松开（release）') }
    } catch (eRel) { console.error('[mcbridge] release 失败: ' + eRel) }
    mcbSlots.use.ticks = 0
    return
  }

  if (action === 'attackAt') {
    // 低帧率下准星可能 MISS，按服务端索敌坐标定位目标。
    var mcT = mcbMc()
    if (mcT === null || mcT.player === null) { console.error('[mcbridge] attackAt: 没进世界'); return }
    var q = arg.split(' ')
    var tx2 = Number(q[0]), ty2 = Number(q[1]), tz2 = Number(q[2])
    if (isNaN(tx2) || isNaN(ty2) || isNaN(tz2)) { console.error('[mcbridge] attackAt 参数非法: ' + arg); return }

    // 攻击需满足冷却条件。
    var scale = 1.0
    try { scale = Number(mcT.player.getAttackStrengthScale(0.5)) } catch (eC) { scale = 1.0 }
    if (!(scale >= 0.9)) {
      console.info('[mcbridge] attackAt: 冷却没好（' + Math.round(scale * 100) + '%），这次不挥')
      return
    }

    var target = null
    var best = 1e9
    try {
      var $B2 = Java.loadClass('net.minecraft.world.phys.AABB')
      var box = new $B2(tx2 - 4, ty2 - 4, tz2 - 4, tx2 + 4, ty2 + 4, tz2 + 4)
      var list2 = mcT.level.getEntities(mcT.player, box)
      var n2 = Number(list2.size())
      for (var i2 = 0; i2 < n2; i2++) {
        var e2 = list2.get(i2)
        try {
          if (String(e2.uuid) === String(mcT.player.uuid)) continue
        } catch (eU2) { }
        var ex = Number(e2.x), ey = Number(e2.y), ez = Number(e2.z)
        var dd = (ex - tx2) * (ex - tx2) + (ey - ty2) * (ey - ty2) + (ez - tz2) * (ez - tz2)
        if (dd < best) { best = dd; target = e2 }
      }
    } catch (eL) {
      console.error('[mcbridge] attackAt 找目标失败: ' + eL)
      return
    }
    if (target === null) { console.error('[mcbridge] attackAt: 目标点附近没实体'); return }

    try {
      // 交互朝向走带 TTL 的 temp，避免永久残留。
      var pl3 = mcT.player
      mcbLookTempAt(Number(target.x), Number(target.y) + 0.9, Number(target.z), MCB_LOOK_PITCH_MAX)
      mcT.gameMode.attack(pl3, target)
      console.info('[mcbridge] 已攻击 -> ' + target)
    } catch (eA2) {
      console.error('[mcbridge] attackAt 挥击失败: ' + eA2)
    }
    return
  }

  if (action === 'useOnAt') {
    // 准星更新可能滞后，坐标交互直接构造命中，仍由 gameMode 校验。
    var mcX = mcbMc()
    if (mcX === null || mcX.player === null || $Hand === null) { console.error('[mcbridge] useOnAt: 不可用'); return }
    var q = arg.split(' ')
    var qx = Math.floor(Number(q[0])), qy = Math.floor(Number(q[1])), qz = Math.floor(Number(q[2]))
    if (isNaN(qx) || isNaN(qy) || isNaN(qz)) { console.error('[mcbridge] useOnAt 参数非法: ' + arg); return }
    try {
      var $BP = Java.loadClass('net.minecraft.core.BlockPos')
      var $V3 = Java.loadClass('net.minecraft.world.phys.Vec3')
      var $BHR = Java.loadClass('net.minecraft.world.phys.BlockHitResult')
      var $DIR = Java.loadClass('net.minecraft.core.Direction')
      var tbp = new $BP(qx, qy, qz)
      var tstate = null
      try { tstate = mcX.level.getBlockState(tbp) } catch (eSt) { tstate = null }
      // 交互落点采用三档候选。
      var pick = mcbPickHit(mcX, tbp, tstate)
      var thit = new $BHR(pick.point, pick.face, tbp, false)
      // 临时注视必须到期释放，避免交互后长期低头。
      mcbLookTempAt(pick.point.x, pick.point.y, pick.point.z, MCB_LOOK_PITCH_MAX)
      mcbLastUsePos = { x: qx, y: qy, z: qz }      
      // InteractionResult 可在发包前拒绝；下发不等于执行。
      var res = mcX.gameMode.useItemOn(mcX.player, $Hand.MAIN_HAND, thit)
      console.info('[mcbridge] 已 useItemOn -> (' + qx + ',' + qy + ',' + qz + ')'
        + ' 面=' + pick.face + ' 取点=' + pick.via
        + ' 点=' + Math.round(pick.point.x * 100) / 100 + ','
        + Math.round(pick.point.y * 100) / 100 + ','
        + Math.round(pick.point.z * 100) / 100
        + ' 结果=' + String(res))
    } catch (eU) {
      console.error('[mcbridge] useOnAt 失败: ' + eU)
    }
    return
  }

  if (action === 'recipebook') {
    var mcRB = mcbMc()
    if (mcRB === null || mcRB.player === null) { console.error('[mcbridge] recipebook: 没进世界'); return }
    var rbWant = String(arg || '').trim()
    var rbOut = { want: rbWant || null, err: [], via: [] }
    var rb = null
    try { rb = mcRB.player.getRecipeBook() } catch (eRB1) { rbOut.err.push('getRecipeBook: ' + eRB1) }
    if (rb === null || rb === undefined) {
      console.info('[mcbridge] RECIPEBOOK ' + JSON.stringify(rbOut))
      return
    }
    var cols = null
    try { cols = rb.getCollections(); rbOut.via.push('getCollections') } catch (eRB2) { rbOut.err.push('getCollections: ' + eRB2) }
    try {
      if (cols !== null && cols !== undefined) {
        var nCols = Number(cols.size())
        rbOut.collections = nCols
        var total = 0
        var found = 0
        var iter = cols.iterator()
        while (iter.hasNext()) {
          var col = iter.next()
          var recs = null
          try { recs = col.getRecipes() } catch (eR3) { continue }
          if (recs === null || recs === undefined) continue
          var n = Number(recs.size())
          total += n
          if (rbWant && !found) {
            var it2 = recs.iterator()
            while (it2.hasNext()) {
              var entry = it2.next()
              var idtxt = ''
              try { idtxt = String(entry.id()) } catch (eId) { }
              try {
                var res = entry.recipe().value().getResultItem(null)
                if (res !== null && res !== undefined && !res.isEmpty()) {
                  rbOut.sampleResult = String(res.getItem())
                }
              } catch (eRes) { }
              if (idtxt.indexOf(rbWant) >= 0) { found++; break }
            }
          }
        }
        rbOut.entries = total
        rbOut.matched = found
      }
    } catch (eWalk) { rbOut.err.push('walk: ' + eWalk) }
    try { rbOut.hasPlaceRecipe = (typeof mcRB.gameMode.handlePlaceRecipe === 'function') } catch (ePR) { rbOut.err.push('handlePlaceRecipe: ' + ePR) }
    try { rbOut.hasContains = (typeof rb.contains === 'function') } catch (eCt) { }

    // Rhino Java 访问器必须字面量调用，不能动态取方法。
    try {
      var dp0 = rb.getCollections().iterator()
      if (dp0.hasNext()) {
        var dc0 = dp0.next()
        rbOut.col0 = String(dc0).substring(0, 200)
        var drecs = null
        try { drecs = dc0.getRecipes() } catch (eDR) { rbOut.err.push('getRecipes: ' + eDR) }
        if (drecs !== null && drecs !== undefined && Number(drecs.size()) > 0) {
          var de = drecs.get(0)
          rbOut.entry0 = String(de).substring(0, 300)
          rbOut.probe = {}
          rbOut.probe.id = mcbProbe1(function () { return de.id() })
          rbOut.probe.recipe = mcbProbe1(function () { return de.recipe() })
          rbOut.probe.display = mcbProbe1(function () { return de.display() })
          rbOut.probe.craftingRequirements = mcbProbe1(function () { return de.craftingRequirements() })
          rbOut.probe.result = mcbProbe1(function () { return de.result() })
          rbOut.probe.colGetRecipes = mcbProbe1(function () { return dc0.getRecipes() })
          try {
            var dd = de.display()
            if (dd !== null && dd !== undefined) {
              rbOut.probe.displayStr = String(dd).substring(0, 200)
              rbOut.probe.displayResult = mcbProbe1(function () { return dd.result() })
              var dr = dd.result()
              if (dr !== null && dr !== undefined) {
                rbOut.probe.resultStr = String(dr).substring(0, 200)
                rbOut.probe.resultItem = mcbProbe1(function () { return dr.item() })
                rbOut.probe.resultStack = mcbProbe1(function () { return dr.stack() })
              }
            }
          } catch (eDD) { rbOut.probe.displayWalkErr = String(eDD).substring(0, 80) }
        }
      }
    } catch (eDump) { rbOut.dumpErr = String(eDump).substring(0, 120) }

    console.info('[mcbridge] RECIPEBOOK ' + JSON.stringify(rbOut))
    return
  }

  if (action === 'placeRecipe') {
    // 配方摆放交由服务端处理。
    // 此版本旧 getResultItem 不可用，按稳定 RecipeHolder.id() 匹配。
    var mcPR = mcbMc()
    if (mcPR === null || mcPR.player === null || mcPR.gameMode === null) {
      console.error('[mcbridge] placeRecipe: 不可用'); return
    }
    var prWant = String(arg || '').trim()
    if (!prWant) { console.error('[mcbridge] placeRecipe: 没给配方 id 或物品名'); return }

    var prMenuId = -1
    try { prMenuId = Number(mcPR.player.containerMenu.containerId) } catch (eMI) { }
    if (prMenuId < 0) { console.error('[mcbridge] placeRecipe: 读不到界面 id'); return }

    var prFirst = null, prSeen = 0, prTotal = 0
    var prDiag = []
    try {
      var prCols = mcPR.player.getRecipeBook().getCollections()
      var prIter = prCols.iterator()
      while (prIter.hasNext()) {
        var prCol = prIter.next()
        var prRecs = null
        try { prRecs = prCol.getRecipes() } catch (eRC) { continue }
        if (prRecs === null || prRecs === undefined) continue
        var prIt = prRecs.iterator()
        while (prIt.hasNext()) {
          var prEntry = prIt.next()
          prTotal++
          // 此映射 entry 只可靠暴露 id()，不能读取 recipe/display/result。
          var prRid = null
          try { prRid = String(prEntry.id()) } catch (eId) {
            try { prRid = String(prEntry) } catch (eS) { }
          }
          if (!prRid) {
            if (prDiag.length < 3) prDiag.push('entry 连 id() 都没有')
            continue
          }
          var prHit = (prRid === prWant)
          if (!prHit) continue
          prSeen++
          if (prFirst === null) prFirst = { entry: prEntry, rid: prRid }
        }
      }
    } catch (eWalk) { prDiag.push('翻配方书出错: ' + eWalk) }

    if (prFirst === null) {
      console.info('[mcbridge] PLACERECIPE {"want":"' + prWant + '","placed":false,'
        + '"reason":"配方书里没有这条配方","scanned":' + prTotal
        + ',"diag":' + JSON.stringify(prDiag) + '}')
      return
    }
    var prHow = ''
    try {
      mcPR.gameMode.handlePlaceRecipe(prMenuId, prFirst.entry.id(), false)
      prHow = 'id'
    } catch (eCall1) {
      try {
        mcPR.gameMode.handlePlaceRecipe(prMenuId, prFirst.entry, false)
        prHow = 'entry'
      } catch (eCall2) {
        console.error('[mcbridge] placeRecipe 两副签名都调不动: ' + eCall1 + ' / ' + eCall2)
        return
      }
    }
    console.info('[mcbridge] PLACERECIPE {"want":"' + prWant + '","placed":true,'
      + '"recipe":"' + prFirst.rid + '","candidates":' + prSeen
      + ',"menu":' + prMenuId + ',"how":"' + prHow + '"}')
    return
  }

  if (action === 'useOn') {
    var mcB = mcbMc()
    if (mcB === null || mcB.player === null || $Hand === null) { console.error('[mcbridge] useOn: 不可用'); return }

    var hit = null
    var src = null
    try { hit = mcB.hitResult; src = 'mc.hitResult' } catch (eH) { hit = null }
    if (hit === null || hit === undefined) {
      // 射线 tickDelta=1.0，0.0 会插值回上一 tick 朝向。
      var reach = 4.5
      try { reach = Number(mcB.player.blockInteractionRange) } catch (eR) { }
      hit = mcB.player.pick(reach, 1.0, false)
      src = 'player.pick'
    }

    var diag = src
    try { diag += ' type=' + String(hit.getType()) } catch (eT) { diag += ' type=ERR' }
    try {
      diag += ' pos=' + hit.getBlockPos()
    } catch (eP) {
      console.error('[mcbridge] useOn: 不是方块命中（' + diag + '）-> ' + hit)
      return
    }
    try { diag += ' face=' + String(hit.getDirection()) } catch (eD) { }
    try {
      diag += ' eye=' + Math.round(Number(mcB.player.x) * 10) / 10 + ',' +
        Math.round((Number(mcB.player.y) + Number(mcB.player.eyeHeight)) * 10) / 10 + ',' +
        Math.round(Number(mcB.player.z) * 10) / 10
    } catch (eE) { }
    try { diag += ' rot=' + Math.round(Number(mcB.player.yRot)) + '/' + Math.round(Number(mcB.player.xRot)) } catch (eR2) { }
    console.info('[mcbridge] useOn 诊断: ' + diag)

    var resB = mcB.gameMode.useItemOn(mcB.player, $Hand.MAIN_HAND, hit)
    console.info('[mcbridge] 已 useItemOn 结果=' + String(resB))
    return
  }

  if (action === 'closeGui') {
    // setScreen(null) 只藏画面，未必关闭服务端容器。
    var mcG = mcbMc()
    if (mcG === null) { console.error('[mcbridge] closeGui: 不可用'); return }
    var had = null
    try { had = String(mcG.screen) } catch (eS) { had = '?' }
    var really = 'no'

    // Rhino 中 closeContainer 可能不存在；优先由 screen.removed() 触发原版关闭。
    try {
      var sc = mcG.screen
      if (sc !== null && sc !== undefined) {
        sc.removed()
        really = 'removed()'
      }
    } catch (eR) {
      console.error('[mcbridge] screen.removed() 失败: ' + eR)
    }

    if (really === 'no') {
      var names = ['closeContainer', 'clientSideCloseContainer', 'doCloseContainer']
      for (var ci = 0; ci < names.length; ci++) {
        try {
          var fn = mcG.player[names[ci]]
          if (typeof fn === 'function') { fn.call(mcG.player); really = names[ci]; break }
        } catch (eN) { }
      }
    }

    try {
      mcG.setScreen(null)
    } catch (eC) {
      try { mcG.screen = null } catch (eC2) { console.error('[mcbridge] closeGui 失败: ' + eC + ' | ' + eC2); return }
    }
    console.info('[mcbridge] 已关闭界面（原本是 ' + had + '，关法=' + really + '）')
    return
  }

  if (action === 'swap') {
    // 槽位点击由 gameMode 维护 stateId，裸包会被回滚。
    var mcS = mcbMc()
    if (mcS === null || mcS.player === null) { console.error('[mcbridge] swap: 没进世界'); return }
    var sp = arg.split(' ')
    var from = parseInt(sp[0], 10), to = parseInt(sp[1], 10)
    if (isNaN(from) || isNaN(to) || to < 0 || to > 8) {
      console.error('[mcbridge] swap 参数非法: ' + arg + '（要 "背包槽位 快捷栏槽位0-8"）')
      return
    }
    try {
      var CT = Java.loadClass('net.minecraft.world.inventory.ClickType')
      mcS.gameMode.handleInventoryMouseClick(0, from, to, CT.SWAP, mcS.player)
      console.info('[mcbridge] 已交换槽位 ' + from + ' <-> 快捷栏 ' + to)
    } catch (eSw) {
      console.error('[mcbridge] swap 失败: ' + eSw)
    }
    return
  }

  if (action === 'attack') {
    var mcA = mcbMc()
    if (mcA === null || mcA.player === null) { console.error('[mcbridge] attack: 没进世界'); return }
    var reachA = 3.0
    try { reachA = Number(mcA.player.entityInteractionRange) } catch (eA) { }
    var hitA = mcA.player.pick(reachA, 0.0, false)
    var ent = null
    try { ent = hitA.getEntity() } catch (eE) {
      try { ent = hitA.entity } catch (eE2) { ent = null }
    }
    if (ent === null || ent === undefined) { console.error('[mcbridge] attack: 准星没指着实体'); return }
    mcA.gameMode.attack(mcA.player, ent)
    console.info('[mcbridge] 已攻击 -> ' + ent)
    return
  }

  if (action === 'look') {
    // 调试转头也走 temp，避免留下永久俯角。
    //    正是"低头不残留"那个 bug 的来源（调试动作也算）。
    var mcL = mcbMc()
    if (mcL === null || mcL.player === null) { console.error('[mcbridge] look: 没进世界'); return }
    var parts = arg.split(' ')
    var yaw = Number(parts[0])
    var pitch = parts.length > 1 ? Number(parts[1]) : 0
    if (isNaN(yaw) || isNaN(pitch)) { console.error('[mcbridge] look 参数非法: ' + arg); return }
    mcbLookTemp({ mode: 'angle', yaw: yaw, pitch: pitch })
    console.info('[mcbridge] 转头 -> yaw=' + yaw + ' pitch=' + pitch + '（临时，' + MCB_TEMP_LOOK_TICKS + ' tick 后自动释放）')
    return
  }

  if (action === 'lookAt') {
    var mcLA = mcbMc()
    if (mcLA === null || mcLA.player === null) { console.error('[mcbridge] lookAt: 没进世界'); return }
    var p3 = arg.split(' ')
    var tx = Number(p3[0]), ty = Number(p3[1]), tz = Number(p3[2])
    if (isNaN(tx) || isNaN(ty) || isNaN(tz)) { console.error('[mcbridge] lookAt 参数非法: ' + arg); return }
    mcbLookTempAt(tx, ty, tz, MCB_LOOK_PITCH_MAX)
    console.info('[mcbridge] 转向 (' + tx + ',' + ty + ',' + tz + ')（临时）')
    return
  }

  if (action === 'aim') {
    // updateTarget 每 tick 清空，持续状态保存在 aim 槽。
    var mcAi = mcbMc()
    if (mcAi === null || mcAi.player === null) { console.error('[mcbridge] aim: 没进世界'); return }
    var aiParts = arg.split(' ')
    var aiYaw = Number(aiParts[0])
    var aiPitch = aiParts.length > 1 ? Number(aiParts[1]) : 0
    if (isNaN(aiYaw) || isNaN(aiPitch)) { console.error('[mcbridge] aim 参数非法: ' + arg); return }
    mcbTuneBaritone()
    mcbSlots.aim = { mode: 'angle', yaw: aiYaw, pitch: aiPitch,
                     until: $mcbTick + MCB_AIM_MAX_HOLD_TICKS }
    mcbSetAntiCheat(false)     
    mcbAimApply()          
    console.info('[mcbridge] aim -> yaw=' + aiYaw + ' pitch=' + aiPitch + '（每 tick 保持）')
    return
  }

  if (action === 'aimAt') {
    var mcAt = mcbMc()
    if (mcAt === null || mcAt.player === null) { console.error('[mcbridge] aimAt: 没进世界'); return }
    var atParts = arg.split(' ')
    var atx = Number(atParts[0]), aty = Number(atParts[1]), atz = Number(atParts[2])
    if (isNaN(atx) || isNaN(aty) || isNaN(atz)) { console.error('[mcbridge] aimAt 参数非法: ' + arg); return }
    mcbTuneBaritone()
    mcbSlots.aim = { mode: 'point', x: atx, y: aty, z: atz,
                     until: $mcbTick + MCB_AIM_MAX_HOLD_TICKS }
    mcbSetAntiCheat(false)     
    mcbAimApply()
    console.info('[mcbridge] aimAt (' + atx + ',' + aty + ',' + atz + ')（每 tick 重算，保持）')
    return
  }

  if (action === 'releaseAim') {
    // 停止重设即可让 Baritone target 自行过期，无 clear API。
    // 释放 aim 后可能仍由 idle 接管，不要强制切回 antiCheat。
    mcbSlots.aim = null
    console.info('[mcbridge] releaseAim: 持续注视已松开（接下来交给空闲层 / Baritone）')
    return
  }

  if (action === 'clickSlot') {
    // 容器点击交给 gameMode，窗口与 stateId 不能自行构造。
    
    var mcC = mcbMc()
    if (mcC === null || mcC.player === null || mcC.gameMode === null) {
      console.error('[mcbridge] clickSlot: 不可用'); return
    }
    var ps = String(arg || '').trim().split(/\s+/)
    var cSlot = parseInt(ps[0], 10)
    var cBtn = (ps.length > 1) ? parseInt(ps[1], 10) : 0
    var cMode = (ps.length > 2) ? parseInt(ps[2], 10) : 0
    if (isNaN(cSlot)) { console.error('[mcbridge] clickSlot: 没给格子号'); return }
    if (isNaN(cBtn)) cBtn = 0
    if (isNaN(cMode)) cMode = 0

    var cid = -1, nSlots = -1
    try {
      cid = Number(mcC.player.containerMenu.containerId)
      nSlots = Number(mcC.player.containerMenu.slots.size())
    } catch (eM) { }
    if (cSlot < 0 || (nSlots >= 0 && cSlot >= nSlots)) {
      console.error('[mcbridge] clickSlot: 格子号 ' + cSlot + ' 越界（当前界面只有 ' + nSlots + ' 格）')
      return
    }

    // 点格子朝向复用最近 useOnAt 位置，缺失时不转。
    if (mcbLastUsePos !== null) {
      mcbLookTempAt(mcbLastUsePos.x + 0.5, mcbLastUsePos.y + 0.5, mcbLastUsePos.z + 0.5,
                    MCB_LOOK_PITCH_MAX)
    }

    try {
      var $CT = Java.loadClass('net.minecraft.world.inventory.ClickType')
      var ctypes = [$CT.PICKUP, $CT.QUICK_MOVE, $CT.SWAP, $CT.CLONE, $CT.THROW, $CT.QUICK_CRAFT, $CT.PICKUP_ALL]
      if (cMode < 0 || cMode >= ctypes.length) cMode = 0
      mcC.gameMode.handleInventoryMouseClick(cid, cSlot, cBtn, ctypes[cMode], mcC.player)
      console.info('[mcbridge] 已点格子 slot=' + cSlot + ' button=' + cBtn + ' mode=' + cMode +
        '（界面 id=' + cid + ' 共 ' + nSlots + ' 格）')
    } catch (eClk) {
      console.error('[mcbridge] clickSlot 失败: ' + eClk)
    }
    return
  }

  if (action === 'probe') {
    var report = mcbProbe()
    console.info('[mcbridge] PROBE ' + report)
    var mc2 = mcbMc()
    if (mc2 !== null && mc2.player !== null) {
      try {
        mc2.player.sendData(MCB_CHANNEL_UP, { status: 'probe', note: report })
        console.info('[mcbridge] PROBE 已上行')
      } catch (e) {
        console.error('[mcbridge] PROBE 上行失败: ' + e)
      }
    }
    return
  }

  if (action === 'uplink') {
    var ok = mcbSendUplink('manual')
    console.info('[mcbridge] 手动上行: ' + (ok ? 'OK' : 'FAILED'))
    return
  }

  console.error('[mcbridge] 未知 action: "' + action + '"（arg="' + arg + '"）')
}

NetworkEvents.dataReceived(MCB_CHANNEL_DOWN, event => {
  var tag = event.data
  var action = mcbStr(tag, 'action')
  var arg = mcbStr(tag, 'arg')
  console.info('[mcbridge] 收到服务端数据: action=' + action + ' arg="' + arg + '"')
  var receiptId = mcbStr(tag, 'receiptId')
  var receiptResult = 'executed'
  var receiptError = ''
  try {
    mcbHandle(action, arg)
    console.info('[mcbridge] NETWORK_RECV_OK')
  } catch (e) {
    receiptResult = 'failed'
    receiptError = String(e)
    console.error('[mcbridge] 处理动作失败: ' + e)
  }
  if (action === 'say' && receiptId) {
    var receiptMc = mcbMc()
    if (receiptMc !== null && receiptMc.player !== null) {
      try {
        receiptMc.player.sendData(MCB_CHANNEL_UP, {
          receipt: 'say', receiptId: receiptId, result: receiptResult, error: receiptError
        })
      } catch (receiptSendError) {
        console.error('[mcbridge] 说话回执上行失败: ' + receiptSendError)
      }
    }
  }
})

// Rhino 高频回调内只用 var，const/let 会重复声明。

let $uplinkTick = 0
let $mcbTick = 0            


ClientEvents.tick(() => {
  $mcbTick = $mcbTick + 1

  // 逐通道更新声明。
  mcbTickSlots()

  $uplinkTick = $uplinkTick + 1
  if ($uplinkTick < MCB_UPLINK_EVERY_TICKS) return
  $uplinkTick = 0
  try {
    mcbSendUplink('tick')
  } catch (e) {
    console.error('[mcbridge] 定时上行异常: ' + e)
  }
})


function mcbTickSlots() {
  mcbSlotAim()
  mcbSlotUse()
  mcbSlotHotbar()
}

function mcbSlotHotbar() {
  var w = mcbSlots.hotbar.want
  if (w === null || w === undefined) return
  var mc = mcbMc()
  if (mc === null || mc.player === null) return
  var inv = null
  try { inv = mc.player.inventory } catch (eI) { return }
  if (inv === null || inv === undefined) return
  var want = null
  try {
    if (typeof w.slot === 'number') want = w.slot
    else if (w.item) want = mcbFindHotbarItem(inv, String(w.item))
  } catch (eW) { return }
  if (want === null || want < 0 || want > 8) return
  try {
    if (Number(inv.selected) !== want) {
      inv.selected = want
      console.info('[mcbridge] hotbar 槽：维持手上 = ' + want
        + '（' + (w.item ? w.item : '指定格') + '）')
    }
  } catch (eS) { }
}

function mcbFindHotbarItem(inv, id) {
  var reg = null
  try { reg = Java.loadClass('net.minecraft.core.registries.BuiltInRegistries') } catch (eR) { return null }
  try {
    for (var i = 0; i < 9; i++) {
      var st = inv.getItem(i)
      if (st === null || st === undefined) continue
      try { if (st.isEmpty()) continue } catch (eE) { }
      var nm = null
      try { nm = String(reg.ITEM.getKey(st.getItem())) } catch (eK) { nm = null }
      if (nm === id) return i
    }
  } catch (eF) { }
  return null
}

// 无朝向声明时恢复 Baritone 管理，避免横着走。
function mcbSlotAim() {
  var rot = mcbAimRotation()
  if (rot === null) {
    mcbSetAntiCheat(true)
    return
  }
  mcbSetAntiCheat(false)
  mcbAimPush(rot)
}

function mcbAimPush(rot) {
  try {
    var p = mcbMc().player
    if (p !== null) { p.setYRot(rot.yaw); p.setXRot(rot.pitch) }
  } catch (e) { }
  if ($RotationCls === null) return
  var bar = mcbPrimary()
  if (bar === null) return
  try {
    bar.getLookBehavior().updateTarget(new $RotationCls(rot.yaw, rot.pitch), true)
  } catch (e2) {
    if ($mcbAimErrReported !== true) {
      $mcbAimErrReported = true
      console.error('[mcbridge] updateTarget 失败（后续不再重复报）: ' + e2)
    }
  }
}

// 只维持使用键，不重发 useItem，否则重置倒计时。
function mcbSlotUse() {
  var u = mcbSlots.use
  if (u.ticks <= 0) return
  u.ticks = u.ticks - 1
  var holdMc = mcbMc()
  var holdUsing = false
  try { if (holdMc !== null && holdMc.player !== null) holdUsing = !!holdMc.player.isUsingItem() } catch (eHU) { }
  if (holdUsing) u.sawUsing = true
  var done = (u.ticks === 0) || (u.sawUsing && !holdUsing)
  if (done) {
    try { if (holdMc !== null) holdMc.options.keyUse.setDown(false) } catch (eHR) { }
    console.info('[mcbridge] 使用键已松开（' + (u.ticks === 0 ? '到上限' : '她已结束') + '）')
    u.ticks = 0
    u.sawUsing = false
  }
}

console.info('[mcbridge] 脚本已加载（含状态上行 + probe，每 ' + MCB_UPLINK_EVERY_TICKS + ' tick 上行一次）')
