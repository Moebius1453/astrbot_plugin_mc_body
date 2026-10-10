// Nanako 客户端桥 —— 收服务端下发的动作，自己执行；并定时把状态上行回去
//
// 下行：RCON / AstrBot  ->  服务端 mcbridge_server.js  ->  sendData('mcbridge')
//        ->  这里 NetworkEvents.dataReceived  ->  Baritone / 说话
// 上行：这里 player.sendData('mcbridge_up', {...})  ->  服务端 NetworkEvents.dataReceived
//
// 支持的动作（服务端 /mcb <action> <arg>）：
//   ping              连通性探针，只在客户端日志里回一声
//   say <text>        让 Nanako 在游戏里说话
//   baritone <cmd>    原样执行一条 Baritone 命令，例：follow player Steve
//   stop              取消一切寻路
//   probe             自检：gameMode / sendData / pick / Baritone 状态能不能拿到
//   uplink            手动触发一次状态上行（测试用）
//   hotbar <0-8>      切换快捷栏槽位
//   look <yaw> <pitch> 转头
//   use               使用手上的东西（吃/喝/放）
//   useOn             对准星指着的方块右键（开箱子/工作台、放方块）
//   attack            攻击准星指着的实体
//
// 注意： 交互一律走 mc.gameMode，绝不裸构造 Packet —— 服务端维护着窗口 ID、
//    槽位 stateId、视线校验、以及 1.21.1 的 sequence 序列号（伪造/漏发会让服务端
//    撤销你所有放置操作）。这四套 MultiPlayerGameMode 全替你管了。
//    注意： player.gameMode 实测恒为 null，要用 mc.gameMode（mc = Minecraft.getInstance()）。
// 注意： 交互有距离限制（约 4.5 格）—— 够不着就是够不着，先让 Baritone 走过去。
//
// 注意： 客户端脚本改完必须重启客户端（服务端脚本才能 /reload 热重载）。
// 注意： Rhino 三个坑（都踩过）：
//   1. 变量名别用 cls —— TypeError: redeclaration of var cls
//   2. 函数体内别用 const —— 函数每 tick 调用一次，Rhino 报 redeclaration of var <名>。用 var。
//   3. 不能调 Java 对象的 .getClass() —— Cannot find function getClass in object ...
//      注意： 这个报错不代表 MC 没有对应接口，只是 Rhino 不给用反射。
//
// 日志位置：<客户端目录>/logs/kubejs/client.log（也会出现在 launch.log，前缀 [KubeJS Client/]）

const MCB_CHANNEL_DOWN = 'mcbridge'
const MCB_CHANNEL_UP = 'mcbridge_up'
const MCB_UPLINK_EVERY_TICKS = 10   // 客户端 tick ≈20/s  ->  约 2Hz
const USE_HOLD_TICKS = 60           // "按住使用"的保底上限（见下面按 dur 自适应的逻辑）
const USE_HOLD_MARGIN = 20          // 在物品自身 dur 之上多留的余量

// ---- 朝向（三层槽）的参数 —— 见 mcbSlots 的注释 -------------------------
const MCB_TEMP_LOOK_TICKS = 30      // 临时注视活多久（1.5 秒）—— 到点自己释放
const MCB_LOOK_PITCH_MAX = 45       // 看向一个点时 pitch 的绝对值上限（人不会把脖子折成 90°）
const MCB_IDLE_YAW_SPAN = 35        // 空闲扫视：左右各多少度
const MCB_IDLE_PITCH_SPAN = 15      // 空闲扫视：抬头 / 低头各多少度
const MCB_IDLE_HOLD_MIN = 80        // 一个注视点至少保持 4 秒
const MCB_IDLE_HOLD_MAX = 140       // 最多 7 秒
const MCB_IDLE_STEP_YAW = 5         // 每 tick 最多转 5°（≈100°/秒，接近人扭头的速度）
const MCB_IDLE_STEP_PITCH = 3
const MCB_IDLE_NEAR_PLAYER = 8      // 这么近有人  ->  有机会看他
const MCB_IDLE_LOOK_AT_PLAYER = 0.6 // 每次抽签"看人"的概率（1.0 = 一直盯着，那又成石像了）

// 持续注视（aim 槽）最长保持多久 —— 2 分钟。
// 理由见 mcbLookClaim 里那段：调用方忘了 releaseAim 时，她不该永远僵着。
// 20 tick/秒 × 120 秒。到点自动放手，交给空闲扫视 / Baritone。
const MCB_AIM_MAX_HOLD_TICKS = 2400

let $BaritoneAPI = null
try {
  $BaritoneAPI = Java.loadClass('baritone.api.BaritoneAPI')
  console.info('[mcbridge] BaritoneAPI 类可见: ' + $BaritoneAPI)
} catch (e) {
  console.error('[mcbridge] BaritoneAPI 类不可见: ' + e)
}

let $Minecraft = null

// ---- 朝向（三层槽：temp / aim / idle）----------------------------------------
//
// 2026-10-10 重做。原来是一个 aim 槽，空着时谁都不写朝向 —— 结果是
// 1 交互动作裸 setXRot 留下的残值永远没人收（她低头低到天荒地老）
// 2 站着时是一尊石像。三层见 mcbSlots 的注释。
//
// 注意：注意： 必须每 tick 重设，设一次只生效一个 tick（2026-10-10 读 Baritone 源码后修正）。
//    原来我以为调一次 updateTarget 就一直生效 —— 错了。
//    LookBehavior 在 PlayerUpdateEvent.POST 的末尾无条件清空：
//        // The target is done being used for this game tick, so it can be invalidated
//        this.target = null;
//    所以"设一次"只能锁住那一个 tick，下一个 tick 就被 Baritone 抢回去了。
//
// 第二个 boolean 参数的真实语义（javadoc 是误导的，它叫 blockInteract，
// 但 Baritone 内部传的是 hasToForceRotations()）—— 它喂给 Target.Mode.resolve：
//    freeLook=true（默认）+ blockFreeLook=false（默认）时：
//        传 true   ->  CLIENT 模式：本地镜头真的转（她看得见）
//        传 false  ->  SERVER 模式：静默转 —— 只有发给服务器的包带朝向，本地画面不动
//    所以"走过去看看那是什么"要传 true。
let $RotationCls = null
try {
  $RotationCls = Java.loadClass('baritone.api.utils.Rotation')
} catch (eRot) {
  console.error('[mcbridge] Rotation 类不可见: ' + eRot)
}

// ⭐ 行为槽 —— 客户端每 tick 把每个槽施加一次，各槽互不知道对方存在。
//
// 抄的是原版 Brain 的 memory 模型：LOOK_TARGET 和 WALK_TARGET 由不同的 sink
// 消费，天然并存、互不抢占。我们的老毛病正是"没有槽，把朝向和移动挤在同一个控制通道里"。
// （也见 AltoClef 的 chains/：多个带优先级的 chain 并行跑，高优先级抢占。）
//
// 注意： walk 槽不在这里 —— 那是 Baritone 自己的（mcb baritone / mcb goto）。
//    这里只管我们自己要施加的两个。
var mcbSlots = {
  // ---- 朝向：三层，每层有自己的所有者，优先级从高到低 ----
  //
  //   1 temp 临时注视 —— 交互用（右键方块 / 攻击），带 TTL，到点自动释放。
  //      ⭐ 为什么要它（2026-10-10 用户报的 bug）：交互原来是裸 setXRot、
  //         用完不还原。而瞄准一个脚边的方块 = pitch 80°，于是她低头低到天荒地老。
  //         实测现场：mcb lookat 读到 pitch:81.3，命中脚边 1.7 格处的石砖；
  //         而客户端日志里最后一次写朝向是 10 分钟前的一次 useOnAt。
  //         决定性实验：mcb look 0 0 掰平后等 12 秒仍是 0.0
  //          ->  没有任何东西在压她，就是残值没人收。
  //   2 aim  持续注视 —— mc_aim 用，要显式 release（语义不变）。
  //   3 idle 空闲行为 —— 新增。没有别的事时她自己会东张西望。
  //      为什么要有：aim 为空时谁都不写朝向，Baritone 又只在干活时管朝向，
  //      所以她站着时是一尊石像（还冻在上一次的俯角上）。
  //
  //   { mode:'angle', yaw, pitch }   —— 固定朝向
  //   { mode:'point', x, y, z, pitchMax } —— 固定世界坐标，每 tick 用当前位置重算
  aim: null,
  temp: null,        // { mode, …, until:<tick> }
  idle: {            // 空闲扫视 —— 目标 + 缓动（不是硬切，硬切看着像鬼畜）
    on: true,
    tyaw: null, tpitch: 0,   // 目标朝向；tyaw=null 表示"还没起算"
    cyaw: 0, cpitch: 0,      // 缓动中的当前值
    nextAt: 0                // 下次换目标的 tick
  },
  // 「按住使用」槽。一直是对象，ticks > 0 表示使用键正被我们按着
  use: { ticks: 0, sawUsing: false },
  // 手上拿什么槽（第 3 批 3.3）。want = null 表示没人管，就随她去。
  //   want = { slot: 0-8 }        —— 维持选中那一格
  //   want = { item: '注册名' }   —— 在快捷栏里找它，找到就选它（找不到不动）
  // 只管维持，不搬东西 —— 搬是插件侧 containers.wear 的事，见 mcbSlotHotbar 的注释。
  hotbar: { want: null }
}

// ---- 朝向：挑"现在该由谁说话" ---------------------------------------------
//
// 优先级：temp（临时，带 TTL）> aim（持续，显式释放）> idle（空闲，默认行为）。
// 都没有  ->  返回 null = 把方向盘还给 Baritone。
//
// 注意： temp 的过期就在这里处理 —— 所以就算调用方再也没碰过它，它也会自己消失。
//    这就是"低头不残留"的全部秘密：残值有主人，主人会到期。
function mcbLookClaim() {
  if (mcbSlots.temp !== null) {
    if ($mcbTick < mcbSlots.temp.until) return mcbSlots.temp
    mcbSlots.temp = null
  }
  if (mcbSlots.aim !== null) {
    // 最长保持（第 2 批 2.5）—— 和 temp 一样到期自动放手，只是长得多。
    //
    // 为什么要有：aim 原来是"要显式 releaseAim 才松"。模型要是调了 mc_aim 之后
    // 忘了调 mc_aim(release=true)，她就永远僵在最后那个朝向上 ——
    // 实测踩过：2026-10-11 用 RCON 手发了几次 aimAt，她 yaw=0 / pitch=-65.1
    // 八秒纹丝不动，看着像"空闲扫视的代码没了"，其实是 aim 槽被占着没松。
    //
    // 这和 arbiter 那条"卡住 = 一次性永久锁死"是同一个病：
    // 释放依赖调用点。到期自己松手才是根治。
    if ($mcbTick < mcbSlots.aim.until) return mcbSlots.aim
    mcbSlots.aim = null
    console.info('[mcbridge] 持续注视到最长保持时间，自动松开（调用方没 releaseAim）')
  }
  if (mcbSlots.idle.on) return mcbIdleClaim()
  return null
}

// 把一个"声明"算成 yaw/pitch。pitchMax 是可选的角度上限（见 MCB_LOOK_PITCH_MAX）。
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
  // MC 的 yaw：0 = +Z，顺时针；atan2(-dx, dz) 是标准换算
  return { yaw: Math.atan2(-dx, dz) * 180 / Math.PI, pitch: pitch }
}

// 算出这一 tick 该用的朝向。
function mcbAimRotation() {
  return mcbRotOf(mcbLookClaim())
}

// 朝目标转一步。注意： 必须走最短的那边 —— yaw 在 ±180° 处会绕回来，
// 直接 cur + (target-cur) 会让"从 170 转到 -170"绕地球一圈。
function mcbTurnToward(cur, target, step) {
  var d = target - cur
  while (d > 180) d = d - 360
  while (d < -180) d = d + 360
  if (d > step) d = step
  if (d < -step) d = -step
  return cur + d
}

// Baritone 在干活吗？在干活就别抢朝向 —— 它自己会让她看向行进方向。
// 注意： 只看"在不在跑进程"，不看 follow —— 跟到人旁边站着不动时应该算"闲"，
//    不然她跟着你也是一尊石像。（isPathing() 已经覆盖了"走着跟"的情形。）
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

// 附近有没有别人（8 格内）？有就返回他。"看向你"是"陪伴"最直白的一条。
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

// 空闲扫视 —— 她"没事时"的脑袋长什么样。
//
//   · Baritone 在干活  ->  不抢（交给它，她会自然看向行进方向）
//   · 否则每隔 4~7 秒抽一次签：
//       - 多半（MCB_IDLE_LOOK_AT_PLAYER） ->  看向 8 格内的人（"陪伴"最直白的一条）
//       - 否则  ->  相对当前朝向 ±35°、俯仰 ±15° 随便挑一个
//     然后慢慢转过去（每 tick 最多 5°）
//
// 注意： 两个刻意的选择：
//   1 用缓动而不是硬切 —— 硬切看着像鬼畜，缓动才像人在扭头。
//   2 看人是抽签、不是"一直盯着" —— 一直盯就又是一尊石像了（换个方向而已）。
function mcbIdleClaim() {
  var id = mcbSlots.idle
  if (mcbBaritoneBusy()) { id.tyaw = null; return null }

  var me = mcbPlayerYawPitch()
  var curYaw = me === null ? 0 : me.yaw
  var curPitch = me === null ? 0 : me.pitch

  if (id.tyaw === null) {
    // 刚开始空闲（或刚从"忙"里退出来）—— 从当前朝向起算，别跳
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
    // 兜底：上面两条路都没算出角度（比如没进世界）时别留 null，
    // 否则下面 mcbTurnToward(cur, null, …) 会算出 NaN、朝向直接报废。
    if (id.tyaw === null) { id.tyaw = curYaw; id.tpitch = 0 }
    id.nextAt = $mcbTick + MCB_IDLE_HOLD_MIN +
      Math.floor(Math.random() * (MCB_IDLE_HOLD_MAX - MCB_IDLE_HOLD_MIN))
  }
  id.cyaw = mcbTurnToward(id.cyaw, id.tyaw, MCB_IDLE_STEP_YAW)
  id.cpitch = mcbTurnToward(id.cpitch, id.tpitch, MCB_IDLE_STEP_PITCH)
  return { mode: 'angle', yaw: id.cyaw, pitch: id.cpitch }
}

// 设一条临时注视并立刻生效一次。交互动作走这条，不再裸 setXRot。
function mcbLookTemp(claim) {
  claim.until = $mcbTick + MCB_TEMP_LOOK_TICKS
  mcbSlots.temp = claim
  mcbSlots.idle.tyaw = null      // 空闲那边重新起算，免得松手时跳一下
  mcbSetAntiCheat(false)
  mcbAimApply()
}

function mcbLookTempAt(x, y, z, pitchMax) {
  mcbLookTemp({ mode: 'point', x: x, y: y, z: z, pitchMax: pitchMax })
}

// 上一次 useOnAt 对着哪一格右键 —— clickSlot 靠它转头（第 3 批 3.5）。
// 开着箱子点格子时她原本是背对着的，看着不像在用那个箱子。
var mcbLastUsePos = null

// 给一格方块挑"手该点在哪一点"—— 抄 Numen Look.point 的三档候选（第 3 批 3.1）。
//
// 为什么不能写死上表面：隐藏方块（台阶、雪、耕地）、只能从侧面用的方块、
// 形状不规则的模组方块，拿"上表面中心"去点会放错面或者直接失败。
//
// 三档，取第一个"看得见且够得着"的：
//   1 轮廓中心 —— 整块轮廓盒的中心（大多数方块第一档就成了）
//   2 各面中心 —— 朝向她的那几个面的中心
//   3 面上离眼最近点 —— 把眼睛投影到那个面上
//
// 返回 { point: Vec3, face: Direction, via: 哪一档, blocked: 挡住它的方块还是 null }。
// 全都不行时退回"上表面中心" —— 保持老行为，不让它比以前更差。
function mcbPickHit(mc, bp, state) {
  var $V3 = null, $DIR = null
  try {
    $V3 = Java.loadClass('net.minecraft.world.phys.Vec3')
    $DIR = Java.loadClass('net.minecraft.core.Direction')
  } catch (e0) {
    return { point: null, face: null, via: 'unavailable', blocked: null }
  }
  var fallback = {
    point: $V3.atCenterOf(bp), face: $DIR.UP, via: 'fallback-top', blocked: null
  }
  if ($V3 === null || $DIR === null) return fallback

  var p = mc.player
  var ex = Number(p.x), ey = Number(p.y) + Number(p.eyeHeight), ez = Number(p.z)
  var reach = 4.5
  try { reach = Number(p.blockInteractionRange) } catch (eR) { }

  // 方块的轮廓盒；读不到就按整格算
  var minX = bp.getX(), minY = bp.getY(), minZ = bp.getZ()
  var maxX = minX + 1, maxY = minY + 1, maxZ = minZ + 1
  try {
    var shape = state.getShape(mc.level, bp)
    var bb = shape.bounds()
    if (bb !== null && bb !== undefined) {
      minX = Number(bb.minX); minY = Number(bb.minY); minZ = Number(bb.minZ)
      maxX = Number(bb.maxX); maxY = Number(bb.maxY); maxZ = Number(bb.maxZ)
    }
  } catch (eShape) { }

  var cxm = (minX + maxX) / 2, cym = (minY + maxY) / 2, czm = (minZ + maxZ) / 2

  // 面按"朝不朝她"排：朝她的先试
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
    var ddx = pt.x - ex, ddy = pt.y - ey, ddz = pt.z - ez
    if (Math.sqrt(ddx * ddx + ddy * ddy + ddz * ddz) > reach) return false
    return mcbVisible(mc, ex, ey, ez, pt, bp)
  }

  // 1 轮廓中心
  var cand = $V3.atCenterOf(bp)
  try { cand = new $V3(cxm, cym, czm) } catch (eC) { }
  if (ok(cand)) return { point: cand, face: $DIR.UP, via: 'outline-center', blocked: null }

  // 2 各面中心 / 3 面上离眼最近点
  for (var k = 0; k < faces.length; k++) {
    var f = faces[k]
    var fc = mcbFacePoint(f, minX, minY, minZ, maxX, maxY, maxZ, 0.5)
    if (ok(fc)) return { point: fc, face: f, via: 'face-center', blocked: null }
    var fp = mcbFacePoint(f, minX, minY, minZ, maxX, maxY, maxZ, null, ex, ey, ez)
    if (ok(fp)) return { point: fp, face: f, via: 'face-nearest', blocked: null }
  }
  return fallback
}

// 取一个面上的点。ratio 给了就是"按比例取中心"（0.5 = 正中心）；
// 给 null 时把眼睛投影上去，取面上离眼最近的那一点。
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

// 一格的"角色"，一个字母 —— 第 3 批 3.2。整表发（空槽也发），
// 否则判不出"哪一格是空的输入格"，摆料就没法做。
//
// 判据全部按槽位对象的类型推，不按界面类名（抄 Numen GuiOps.window）：
//   Y 她自己的背包格 —— container 是 net.minecraft.world.entity.player.Inventory
//   R 产物格         —— ResultSlot
//   G 合成输入格     —— container 是 CraftingContainer
//   O 只能拿不能放   —— mayPlace 回 false（模组机器的产出格吃这一条）
//   C 别的容器格
// 顺序要紧：产物格既是 ResultSlot 又在 CraftingContainer 里，先判 R。
// 认不出来的给 ?，调用方按"不知道"处理，别猜。
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

// 试调一个访问器，把它返回什么写成一行字（诊断用）。
// 注意： 动态属性名取不到 Java 方法，所以调用方必须写成字面量调用再包进来。
function mcbProbe1(f) {
  try {
    var v = f()
    if (v === null || v === undefined) return 'null'
    return typeof v + ':' + String(v).substring(0, 60)
  } catch (e) {
    return 'throw:' + String(e).substring(0, 60)
  }
}

// 从眼睛到这一点之间有没有别的方块挡着。读不到判定就说"看得见"（老行为，不更差）。
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
    if (hitPos === null || hitPos === undefined) return true        // MISS：路上没东西
    return (Number(hitPos.getX()) === Number(bp.getX())
         && Number(hitPos.getY()) === Number(bp.getY())
         && Number(hitPos.getZ()) === Number(bp.getZ()))
  } catch (eV) {
    return true
  }
}

// 把当前朝向算一遍并推出去一次（给"设完立刻生效"的调用方用）。
// 注意： 每 tick 的维持走 mcbSlotAim  ->  mcbAimPush，两条路共用一个推送函数。
function mcbAimApply() {
  var rot = mcbAimRotation()
  if (rot === null) return false
  mcbAimPush(rot)
  return true
}

// ⭐⭐ antiCheatCompatibility 就是"移动 vs 转头"的总开关（2026-10-10 实测出来）
//
// Target.Mode.resolve 里：freeLook=true + blockInteract=false 时返回
//     antiCheat ? SERVER : NONE
// 所以关掉它  ->  Baritone 自己的"走路朝向"目标解析成 NONE（什么都不做）
//  ->  它不再跟我们在朝向这件事上抢。
//
// 实测对比（寻路中 aim 90，观察服务端朝向 8 秒）：
//     antiCheat=true   ->  164.8  ->  168.7  ->  -175.1   （跟着走路方向漂，锁不住）
//     antiCheat=false  ->  90.0   ->  90.0   ->  90.0     （纹丝不动）
//
// 注意： 但不能一直关着 —— 关着的话她走路时也不会自然朝向行进方向，
//    会盯着上次设定的方向横着走。
//
// ⭐ 2026-10-10 起改成每 tick 由 mcbSlotAim 按"有没有人管朝向"自动切：
//    有声明（temp / aim / idle） ->  关；没人管（Baritone 在干活） ->  开。
//    这里做去重，值没变就不写 —— 不然每 tick 写一次设置太脏。
let $mcbAntiCheat = true      // 初始值跟 Baritone 默认一致
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

// mcbAimApply 的报错去重标记（每 tick 都跑，绝不能刷屏）
let $mcbAimErrReported = false

// 读客户端自己的朝向。三级兜底 —— 和 mcbridge_server.js 里 mcbYawPitch 同一个道理：
// 注意： Rhino 下 player.yRot 不抛异常只给 undefined，而且 getYRot() 也拿不到；
//    最后靠从视线向量反推（那条实测一定通）。
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

// 调 Baritone 那几个会干扰"我要的朝向"的设置。只调一次（脚本加载时也调）。
var mcbBaritoneTuned = false
function mcbTuneBaritone() {
  if (mcbBaritoneTuned || $BaritoneAPI === null) return
  mcbBaritoneTuned = true
  try {
    var s = $BaritoneAPI.getSettings()
    // 注意： 这两个会每 tick 往朝向里叠随机抖动（默认 0.01 度和 2 度）——
    //    会让"我要的朝向"和"实际朝向"差几度，很难查。
    //    ⭐ 2026-10-10："灵动"那一半我们自己实现（见 mcbIdleClaim：
    //    有人看人、没人就每 4~7 秒换个方向慢慢转过去），不靠这两个抖动 ——
    //    自己实现的好处是"她看哪"是可解释、可调试的，抖动不是。
    try { s.randomLooking.value = 0 } catch (e1) { console.error('[mcbridge] randomLooking 设不了: ' + e1) }
    try { s.randomLooking113.value = 0 } catch (e2) { console.error('[mcbridge] randomLooking113 设不了: ' + e2) }
    // 默认 true —— 源码注释自己说"某些情况下会让它卡住"，抱着旧朝向不放
    try { s.remainWithExistingLookDirection.value = false } catch (e3) { console.error('[mcbridge] remainWithExisting... 设不了: ' + e3) }
    // 注意： antiCheatCompatibility 不在这里设 —— 它是"移动 vs 转头"的总开关，
    //    但只在 aim 期间该关（见 mcbSetAntiCheat 的注释）。
    //    平常留着默认 true，让她走路时自然朝向行进方向。

    // 把实际值打出来 —— "设了"和"设上了"是两回事
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

// 交互要用到的原版枚举/类。注意： 交互一律走 mc.gameMode（不是 player.gameMode，那个恒为 null）
let $Hand = null
try {
  $Hand = Java.loadClass('net.minecraft.world.InteractionHand')
} catch (e) {
  console.error('[mcbridge] InteractionHand 不可见: ' + e)
}

// --- 小工具 ---------------------------------------------------------------

// event.data 是 CompoundTag；优先用 getString，取不到再退回属性访问
function mcbStr(tag, key) {
  if (tag === null || tag === undefined) return ''
  try {
    var v = tag.getString(key)
    if (v !== null && v !== undefined) return String(v)
  } catch (e) {
    // 换属性访问
  }
  try {
    var w = tag[key]
    if (w !== null && w !== undefined) return String(w)
  } catch (e2) {
    // 算了
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

// 数：拿不到就返回 null（别用 0 冒充 —— null 才有"未知"的语义）
function mcbNum(v) {
  try {
    if (v === null || v === undefined) return null
    var n = Number(v)
    return isNaN(n) ? null : Math.round(n * 100) / 100
  } catch (e) {
    return null
  }
}

// --- 上行：状态快照 -------------------------------------------------------

// 从 Baritone 抽一份状态。每个字段独立 try —— 坏哪个报哪个，别整体崩。
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
    // 跟随状态拿不到不影响主判断
  }

  if (pathing) {
    snap.status = 'moving'
  } else if (following) {
    snap.status = 'following-idle'   // 在跟随但没在走（目标就在旁边 = 正常）
  } else {
    snap.status = 'idle'
  }

  // 目标：优先 custom goal，其次 follow 过滤器的字符串
  try {
    var g = b.getCustomGoalProcess().getGoal()
    if (g !== null && g !== undefined) snap.goal = String(g)
  } catch (e3) {
    // 没有目标很正常
  }

  // 剩余 tick  ->  秒
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
        if (!isNaN(ticks)) snap.eta = Math.round(ticks / 20)   // tick  ->  秒
      }
    }
  } catch (e5) {
    // eta 拿不到就算了，不是关键
  }

  // 客户端视角坐标（跟服务端交叉校验用）
  var mc = mcbMc()
  try {
    if (mc !== null && mc.player !== null) {
      snap.posX = mcbNum(mc.player.x)
      snap.posY = mcbNum(mc.player.y)
      snap.posZ = mcbNum(mc.player.z)
      // 注意：注意： 客户端自己的朝向 —— 和服务端读到的可能不是一回事，必须分开看。
      //
      //    2026-10-10 加：调试"aim 到底有没有生效"时发现，服务端读到的朝向
      //    （mcb lookat 依赖它）和客户端本地视角可能不一致 ——
      //    Baritone 的 SERVER 模式就是"静默转"：只有发给服务器的包带正确朝向，
      //    本地画面不动。所以只看服务端的数，会误判成"aim 没生效"。
      //    这两个值放在一起报，一眼就能分辨是"真没转"还是"转了但没同步给服务端"。
      snap.yaw = null
      snap.pitch = null
      var yp = mcbPlayerYawPitch()
      if (yp !== null) { snap.yaw = mcbNum(yp.yaw); snap.pitch = mcbNum(yp.pitch) }
    }
  } catch (e6) {
    // 没进世界
  }

  // 当前打开的界面（容器/工作台/背包合成格）。
  // 程序必须知道这个 —— 开着界面时玩家动不了，而且合成/拿箱子全靠"点格子"。
  //
  // 注意： 默认背包界面的 id 是 0、46 格，它每时每刻都开着，所以不能整包上报
  //    （那等于每 2 秒把整个背包发一遍）。默认界面只报 0~4 号格
  //    （0=合成产物，1~4=2×2 合成格）—— 那才是我们要用的部分。
  //    真正的容器（箱子/工作台）id > 0，报全部非空格子。
  try {
    if (mc !== null && mc.player !== null) {
      var menu = mc.player.containerMenu
      if (menu !== null && menu !== undefined) {
        var mId = -1, mSlots = -1
        try { mId = Number(menu.containerId) } catch (eM1) { }
        try { mSlots = Number(menu.slots.size()) } catch (eM2) { }
        var isDefault = (mId === 0)
        var cells = []
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
            cells.push({ i: si, n: nn, c: cc })
          }
        } catch (eL) { }
        // 注意： 必须带上界面类型名：光看 id/格数分不出"工作台"和"箱子"，
        //    而合成只认 CraftingMenu（3×3）。把箱子当成工作台会点到箱子格子上。
        //    （不能调 .getClass() —— Rhino 禁；String(menu) 走 toString 就够）
        var mcls = null
        try { mcls = String(menu) } catch (eCls) { mcls = null }
        // 第 3 批 3.2：每一格的"角色"标记。一格一个字母，整表发（空槽也发）——
        //    判不出"哪一格是空的输入格"就没法摆料。
        //    有一条规则是按槽位对象的类型推的，不按界面类名 —— 所以任何模组的
        //    机器界面都能自动认，加新容器不用再加表行。
        var mroles = ''
        try {
          var rbuf = []
          for (var ri = 0; ri < mSlots; ri++) {
            rbuf.push(mcbSlotRole(menu.slots.get(ri), mc.player))
          }
          mroles = rbuf.join('')
        } catch (eRole) { mroles = '' }
        // 注意： 故意序列化成 JSON 字符串再上行：CompoundTag 里套 list 再套 compound，
        //    服务端那边要靠 getList/getCompound 一层层扒，容易错。一串 JSON 最省事。
        snap.menu = JSON.stringify({ id: mId, slots: mSlots, def: isDefault, cls: mcls,
                                     items: cells, roles: mroles })
      }
    }
  } catch (eMenu) { }

  return snap
}

// 上行一次。返回 true/false（失败打日志，别静默）。
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

// --- 自检 -----------------------------------------------------------------

// 探针：把"哪些能力真的拿得到"一条条打出来 + 上行送回服务端。
// 这是实测，不是推断 —— gameMode 能不能从 Rhino 访问，只有跑过才算数。
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

  // 1) gameMode —— 交互全靠它。两条路都试。
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
  // gameMode 上有没有那五个动作
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

  // 2) pick（射线）—— 交互前要看向目标
  try {
    var r = p.pick(4.5, 0.0, false)
    out.push('player.pick=' + (r === null || r === undefined ? 'null' : String(r)))
  } catch (e5) {
    out.push('player.pick ERR ' + e5)
  }

  // 3) sendData（上行通道）—— 这一个直接决定状态上行能不能做
  try {
    out.push('player.sendData=' + typeof p.sendData)
  } catch (e6) {
    out.push('player.sendData ERR ' + e6)
  }

  // 4) Baritone 状态
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

// --- 动作分发 -------------------------------------------------------------

function mcbHandle(action, arg) {
  if (action === 'ping') {
    console.info('[mcbridge] PONG（客户端活着）')
    return
  }

  if (action === 'say') {
    var mc = mcbMc()
    if (mc === null || mc.player === null) {
      console.error('[mcbridge] say 失败：没进世界')
      return
    }
    mc.player.connection.sendChat(arg)
    console.info('[mcbridge] 已说话: ' + arg)
    return
  }

  if (action === 'stop') {
    var b = mcbPrimary()
    if (b === null) return
    b.getPathingBehavior().cancelEverything()
    console.info('[mcbridge] 已取消所有寻路')
    return
  }

  if (action === 'baritone') {
    var b2 = mcbPrimary()
    if (b2 === null) return
    b2.getCommandManager().execute(arg)
    console.info('[mcbridge] 已执行 baritone: "' + arg + '"')
    return
  }

  // ---- 交互（全走原版 gameMode，绝不裸发包）--------------------------
  //
  // 注意： 别自己构造 Packet：服务端维护着窗口 ID / 槽位 stateId / 视线校验 /
  //    1.21.1 的 sequence 序列号 —— 这四套 MultiPlayerGameMode 全替你管了。

  if (action === 'hotbar') {
    // 切换快捷栏槽位（0-8）。吃东西/放方块前要先换到手的东西。
    //
    // 第 3 批 3.3：从这里开始它也进"手上拿什么"槽 —— 不再是一次性命令，
    // 而是每 tick 维持住，直到被 releaseHotbar 撤掉或被下一条顶掉。
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
    // 让"手上是某个东西"变成一个持续维持的性质：在快捷栏里找它，找到就选它。
    // 找不到就什么都不做 —— 搬东西是插件侧的事（containers.wear），
    // 客户端每 tick 去点格子太吵，还会搅乱正在跑的界面操作。
    var mcHI = mcbMc()
    if (mcHI === null || mcHI.player === null) { console.error('[mcbridge] hotbarItem: 没进世界'); return }
    var wantId = String(arg || '').trim()
    if (!wantId) { console.error('[mcbridge] hotbarItem: 没给物品注册名'); return }
    mcbSlots.hotbar.want = { item: wantId }
    console.info('[mcbridge] hotbar 槽 -> 要手上是 ' + wantId + '（找不到就不动，等插件侧搬）')
    return
  }

  if (action === 'releaseHotbar') {
    // 松手：不再管她手上是什么，随她自己 / 随别的动作。
    mcbSlots.hotbar.want = null
    console.info('[mcbridge] hotbar 槽已松开')
    return
  }

  if (action === 'use') {
    // 使用手上的东西：吃东西、喝药水（对着空气用）。
    //
    // 注意：注意： 血泪教训（2026-10-09，服务端探针拿到铁证）：
    //   绝对不要自己循环重发 useItem！
    //   每发一次，服务端的 useItemRemaining 就被重置回满值（面包 = 32），
    //   永远数不到 0 —— 也就永远吃不完。实测服务端 remain 在 32 <-> 31 之间反复跳，
    //   1.6 秒后直接掉成 0（被取消）。客户端日志显示我们1 秒内发了 3 次，
    //   就是这么一次次把进度打回去的。
    //   原因：MultiPlayerGameMode.useItem 每调一次就发一个 ServerboundUseItemPacket，
    //   而服务端的 startUsingItem 会把倒计时重置。
    //
    // 已完成： 正解：像真人按住右键那样 —— 把使用键真的按下去
    //   （mc.options.keyUse.setDown(true)），让原版自己的 tick 逻辑维持使用。
    //   原版自带 4 tick 节流，而且正在使用时不会再重发。我们只在旁边看着，到点松手。
    var mcU = mcbMc()
    if (mcU === null || mcU.player === null || $Hand === null) { console.error('[mcbridge] use: 不可用'); return }

    // 先看看手上这东西是不是"可持续使用"类（食物 32 / 药水 32 / 弓 …）。
    // getUseDuration 正是服务端 handleUseItem 用来判定的同一个判据。
    var dur = 0
    try {
      var held = mcU.player.getMainHandItem()
      try { dur = Number(held.getUseDuration(mcU.player)) } catch (eD1) {
        try { dur = Number(held.getUseDuration()) } catch (eD2) { dur = 0 }
      }
    } catch (eHd) { dur = 0 }
    if (!(dur > 0)) dur = 0

    // 立刻开始（不必等下一次 handleKeybinds）
    try { mcU.gameMode.useItem(mcU.player, $Hand.MAIN_HAND) } catch (eUi) {
      console.error('[mcbridge] useItem 失败: ' + eUi)
      return
    }

    var keyOk = 'no'
    if (dur > 0) {
      try {
        mcU.options.keyUse.setDown(true)
        keyOk = 'yes'
        // 注意： 上限必须跟着物品自身的使用时长走。
        //    踩过（2026-10-09）：墓碑钥匙 getUseDuration = 86，而我们写死 60  -> 
        //    蓄力到 60 tick 就被我们自己松手打断，永远开不了碑。
        //    （面包是 32，所以这个 bug 一直没暴露。）
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
    // 强制松开使用键（安全兜底：出任何岔子都能用它收场）
    try {
      var mcRel = mcbMc()
      if (mcRel !== null) { mcRel.options.keyUse.setDown(false); console.info('[mcbridge] 使用键已松开（release）') }
    } catch (eRel) { console.error('[mcbridge] release 失败: ' + eRel) }
    mcbSlots.use.ticks = 0
    return
  }

  if (action === 'attackAt') {
    // 打指定坐标附近的实体。arg 形如 "21 98 5"。
    //
    // 为什么要给坐标：准星射线实测经常 MISS（软件渲染帧率低 + 朝向难控），
    // 所以不用射线选目标 —— 直接按坐标找最近的实体。
    // 目标由服务端索敌给出（mcb threats），那边查得准。
    var mcT = mcbMc()
    if (mcT === null || mcT.player === null) { console.error('[mcbridge] attackAt: 没进世界'); return }
    var q = arg.split(' ')
    var tx2 = Number(q[0]), ty2 = Number(q[1]), tz2 = Number(q[2])
    if (isNaN(tx2) || isNaN(ty2) || isNaN(tz2)) { console.error('[mcbridge] attackAt 参数非法: ' + arg); return }

    // 攻击冷却 —— 抄原版 Minecraft.startAttack() 的逻辑：
    // 冷却没满就挥是白挥，伤害会被服务端按比例压低。
    var scale = 1.0
    try { scale = Number(mcT.player.getAttackStrengthScale(0.5)) } catch (eC) { scale = 1.0 }
    if (!(scale >= 0.9)) {
      console.info('[mcbridge] attackAt: 冷却没好（' + Math.round(scale * 100) + '%），这次不挥')
      return
    }

    // 在目标坐标附近找一个实体
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
      // 先转向它（视觉上像人，也让客户端的命中判定自然）
      // ⭐ 走 temp 槽（带 TTL）—— 不再裸 setXRot 留下残值（见 mcbSlots 的注释）
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
    // 对着指定坐标的方块右键。arg 形如 "21 98 5"。
    //
    // 为什么不用准星射线：实测 mc.hitResult 经常是 MISS（软件渲染帧率低 + 朝向难控），
    // 而且 setXRot/setYRot 之后准星不一定立刻跟。
    // 所以这里直接构造命中结果喂给 gameMode —— 仍然是原版那条路
    // （窗口 ID / stateId / sequence 全由 MultiPlayerGameMode 管），只是不用射线。
    // 服务端会校验距离（约 4.5 格），所以还是得先走过去。
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
      // 第 3 批 3.1：三档候选挑"点哪一点"（抄 Numen Look.point），
      // 不再写死上表面 —— 写死的话隐藏方块、侧面放置、形状不规则的方块会放错面或失败。
      var pick = mcbPickHit(mcX, tbp, tstate)
      var thit = new $BHR(pick.point, pick.face, tbp, false)
      // 先转向它（让人看着自然，也让服务端的视线校验好过）
      // ⭐ 走 temp 槽（带 TTL）—— 不再裸 setXRot 留下残值。
      //    实测过的病：瞄准脚边的方块 = pitch 80°，用完不还原  ->  低头低到天荒地老。
      //    MCB_LOOK_PITCH_MAX 再把角度夹住（人不会为看脚边的方块把脖子折成 90°）。
      mcbLookTempAt(pick.point.x, pick.point.y, pick.point.z, MCB_LOOK_PITCH_MAX)
      mcbLastUsePos = { x: qx, y: qy, z: qz }      // clickSlot 要用它转头（3.5）
      mcX.gameMode.useItemOn(mcX.player, $Hand.MAIN_HAND, thit)
      console.info('[mcbridge] 已 useItemOn -> (' + qx + ',' + qy + ',' + qz + ')'
        + ' 面=' + pick.face + ' 取点=' + pick.via)
    } catch (eU) {
      console.error('[mcbridge] useOnAt 失败: ' + eU)
    }
    return
  }

  if (action === 'recipebook') {
    // 只读探针 —— 第 3 批 3.4 的前置核实，不是实现。
    //
    // 为什么要先探：配方书一键摆料（handlePlaceRecipe）要求服务端认得这个配方、
    // 而且客户端的配方书里真有它。服务端的 NBT 查出来是空的
    // （data get entity Nanako recipeBook 回 {}），但那个空到底是
    // "她真的没解锁任何配方"还是"这条 NBT 不暴露"，从服务端看不出来。
    // 所以到客户端问一次：配方书里到底有多少条、能不能查到某个物品。
    // 结果只写客户端日志（launch.log）—— 那是我们既有的取证通道，
    // 不占上行协议的位置。
    //
    // 用法：mcb recipebook [物品注册名]
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
    // 顺便看一眼两个 API 在不在（实现要用它们）
    try { rbOut.hasPlaceRecipe = (typeof mcRB.gameMode.handlePlaceRecipe === 'function') } catch (ePR) { rbOut.err.push('handlePlaceRecipe: ' + ePR) }
    try { rbOut.hasContains = (typeof rb.contains === 'function') } catch (eCt) { }

    // 把第一条目整个摊开 —— 1.21.1 换了 RecipeDisplay 那套，光看文档猜不出来。
    // 手法照项目的惯例：候选访问器一次全试，跑一遍就知道哪条通，别一条条猜。
    // 注意： 动态属性名（obj['method']()）在 Rhino 下拿不到 Java 方法，只能字面量调。
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
          // 如果 display() 通了，再往里看一眼产物
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
    // 一键摆料（第 3 批 3.4）：让服务端把某条配方的材料摆进当前的合成格。
    //
    // 为什么值得要：手摆格子要按形状一个一个右键，还要自己处理替代材料；
    // 一个包就能让服务端按配方摆好。抄的是 MC_baritone 的 processPendingCraft。
    //
    // 前置（2026-10-11 实测过了才写的，不是猜的）：
    //   客户端的配方书是满的 —— RECIPEBOOK 探针报 collections=7319 / entries=8103，
    //   而且 mcb.gameMode.handlePlaceRecipe 存在。
    //   服务端那条 `data get entity Nanako recipeBook` 回 {} 只是 NBT 不暴露，
    //   不代表配方书是空的。
    //
    // 挑哪一条：arg 可以给配方 id（推荐，插件侧能从服务端 mcb recipe 拿到），
    // 也可以给物品注册名（退路，按 RecipeHolder.id() 的末段比）。
    //
    // 注意： 这里**不**去读配方的产物。1.21.1 换成了 RecipeDisplay 那套，
    //    老的 getResultItem 已经没了 —— 实测三条路（带 registryAccess / null /
    //    不带参）全部抛异常，扫完 8103 条一条都认不出来。
    //    RecipeHolder.id() 是稳定的标准 API，用它。
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
          // 注意： 实测（2026-10-11，把候选访问器一次全试过）——
          //    1.21.1 这个版本的 entry 只有 id()，它的 toString 就是配方 id
          //    （例：kaleidoscope_tavern:shaker/brass_heart）；
          //    recipe() / display() / result() / craftingRequirements() 四个全抛
          //    Cannot find function。所以别再去读"这条配方产出什么"了，按配方 id 认。
          var prRid = null
          try { prRid = String(prEntry.id()) } catch (eId) {
            try { prRid = String(prEntry) } catch (eS) { }
          }
          if (!prRid) {
            if (prDiag.length < 3) prDiag.push('entry 连 id() 都没有')
            continue
          }
          // 命中判据：完全相等，或者"物品名 == 配方 id 的末段"
          // （原版大部分配方就是这么命名的：minecraft:oak_planks 的配方 id 也叫这个）
          var prHit = (prRid === prWant)
          if (!prHit) {
            var prTail = prRid.indexOf(':') >= 0 ? prRid.substring(prRid.indexOf(':') + 1) : prRid
            var prWantTail = prWant.indexOf(':') >= 0 ? prWant.substring(prWant.indexOf(':') + 1) : prWant
            prHit = (prTail === prWantTail)
          }
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
    // 调哪一副签名：见到过两种写法（RecipeDisplayId 和 RecipeHolder），两条都试，记下哪条通。
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
    // 对着准星指着的方块右键：开箱子/工作台/熔炉、放置方块、按钮拉杆…
    // 距离限制约 4.5 格 —— 够不着就是够不着（先让 Baritone 走过去）。
    var mcB = mcbMc()
    if (mcB === null || mcB.player === null || $Hand === null) { console.error('[mcbridge] useOn: 不可用'); return }

    // 优先用游戏自己算好的准星结果（原版右键用的就是它，一定是对的）。
    var hit = null
    var src = null
    try { hit = mcB.hitResult; src = 'mc.hitResult' } catch (eH) { hit = null }
    if (hit === null || hit === undefined) {
      // 退回自己射线。注意： tickDelta 必须给 1.0 —— 给 0.0 会插值到上一 tick 的朝向，
      //    刚 setXRot/setYRot 完就用 0.0 会朝错方向。
      var reach = 4.5
      try { reach = Number(mcB.player.blockInteractionRange) } catch (eR) { }
      hit = mcB.player.pick(reach, 1.0, false)
      src = 'player.pick'
    }

    // 诊断：把看到的都摊开（一次就能定位问题，别再猜）
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

    mcB.gameMode.useItemOn(mcB.player, $Hand.MAIN_HAND, hit)
    console.info('[mcbridge] 已 useItemOn')
    return
  }

  if (action === 'closeGui') {
    // 关掉当前打开的界面（箱子/工作台/背包/熔炉）。
    // 注意： 界面开着的时候玩家动不了 —— 所以交互之后必须能关掉，
    //    否则她会卡在那儿。这个动作不是可选项。
    //
    // 注意：注意： 只 setScreen(null) 是不够的（踩过 2026-10-09）：
    //     那只是把画面藏起来，服务端的容器还开着 —— player.containerMenu
    //     依旧停在那个容器上，于是后面所有"点格子"的格子号全对不上
    //     （把原木放进熔炉燃料格就是这么来的）。
    //     真正关闭要走 player.closeContainer()：它会给服务端发关闭包，
    //     并把 containerMenu 还原成 inventoryMenu。
    var mcG = mcbMc()
    if (mcG === null) { console.error('[mcbridge] closeGui: 不可用'); return }
    var had = null
    try { had = String(mcG.screen) } catch (eS) { had = '?' }
    var really = 'no'

    // 注意： player.closeContainer() 在 Rhino 里根本不存在
    //    （实测 Cannot find function closeContainer in object LocalPlayer@...）——
    //    所以只能走别的路。按可靠性依次试：
    //
    //    1 screen.removed() —— 这是 Java 内部真正用来关容器的方法：
    //       AbstractContainerScreen.removed() 里就是 player.closeContainer()。
    //       我们自己调它，等于借原版的手去关。这是正解。
    //    2 player.closeContainer() —— 有就调（别的映射名可能能用）
    //    3 setScreen(null) —— 只保证画面没了，容器未必关（这就是原来的坑）
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
    // 把背包里的物品换到快捷栏。arg 形如 "12 3"（背包槽位 快捷栏槽位）。
    // 注意： 仍然走 gameMode —— 槽位点击有 stateId 校验，裸发包会被服务端回滚。
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
    // 攻击准星指着的实体（打怪 / 打树上的东西不适用，挖方块走 Baritone）
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
    // 转头。arg 形如 "90 0"（yaw pitch）
    // 注意： 2026-10-10：改成走 temp 槽 —— 原来裸 setXRot 会留下残值不还原，
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
    // 转向一个世界坐标。arg 形如 "21 98 5"。
    // 比裸 yaw/pitch 好用得多 —— 交互前要先"看向"目标。
    // 注意： 同样走 temp 槽（见 look 的注释）。
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
    // 转头，并且让 Baritone 别把朝向抢回去。arg 形如 "180 -10"（yaw pitch）
    //
    // 注意：注意： 为什么不是直接 setYRot（2026-10-10 实测）：
    //    Baritone 有自己的朝向管理（randomLooking / freeLook），寻路时每 tick 都在写朝向。
    //    直接 setYRot 等于跟它抢方向盘，抢不过 —— 实测：
    //      命令 yaw=180  ->  立刻读到 -179.7（抢到一帧） ->  2.5 秒后变成 southeast（被完全接管）。
    //
    // 已完成： 正解是走 Baritone 自己的 API：ILookBehavior.updateTarget(Rotation, boolean)。
    //    注意： 但它只生效一个 tick —— 真正的重设在下面 ClientEvents.tick 里每 tick 做。
    //    这里只负责"设状态 + 立刻见效"。
    //
    // 注意： updateTarget 是每 tick 被清的，所以 mcbSlots.aim 才是真正的状态；
    //    要停就调 releaseAim。
    var mcAi = mcbMc()
    if (mcAi === null || mcAi.player === null) { console.error('[mcbridge] aim: 没进世界'); return }
    var aiParts = arg.split(' ')
    var aiYaw = Number(aiParts[0])
    var aiPitch = aiParts.length > 1 ? Number(aiParts[1]) : 0
    if (isNaN(aiYaw) || isNaN(aiPitch)) { console.error('[mcbridge] aim 参数非法: ' + arg); return }
    mcbTuneBaritone()
    mcbSlots.aim = { mode: 'angle', yaw: aiYaw, pitch: aiPitch,
                     until: $mcbTick + MCB_AIM_MAX_HOLD_TICKS }
    mcbSetAntiCheat(false)     //  <-  瞄准期间：让 Baritone 别管朝向
    mcbAimApply()          // 立刻来一发，别等下一个 tick
    console.info('[mcbridge] aim -> yaw=' + aiYaw + ' pitch=' + aiPitch + '（每 tick 保持）')
    return
  }

  if (action === 'aimAt') {
    // 盯着一个世界坐标看，而且每 tick 用当前位置重算朝向 ——
    // 所以"走过去看着那口箱子"是真的"看着那口箱子"，不是"看着那个方向"。
    // arg 形如 "21 98 5"。
    var mcAt = mcbMc()
    if (mcAt === null || mcAt.player === null) { console.error('[mcbridge] aimAt: 没进世界'); return }
    var atParts = arg.split(' ')
    var atx = Number(atParts[0]), aty = Number(atParts[1]), atz = Number(atParts[2])
    if (isNaN(atx) || isNaN(aty) || isNaN(atz)) { console.error('[mcbridge] aimAt 参数非法: ' + arg); return }
    mcbTuneBaritone()
    mcbSlots.aim = { mode: 'point', x: atx, y: aty, z: atz,
                     until: $mcbTick + MCB_AIM_MAX_HOLD_TICKS }
    mcbSetAntiCheat(false)     //  <-  瞄准期间：让 Baritone 别管朝向
    mcbAimApply()
    console.info('[mcbridge] aimAt (' + atx + ',' + aty + ',' + atz + ')（每 tick 重算，保持）')
    return
  }

  if (action === 'releaseAim') {
    // 松开朝向锁。
    //
    // 注意： Baritone 的 ILookBehavior 没有"清除目标"的方法（只有 updateTarget
    //    和 getAimProcessor）—— 但目标反正每 tick 自己会过期，
    //    所以这里只要不再重设就行。
    //
    // 注意： 2026-10-10：这里不再强行 mcbSetAntiCheat(true) ——
    //    松手之后多半正好轮到 idle 空闲层接管（她这会儿还在看别处），
    //    强行开回去下一 tick 又得关，白白抖一下。
    //    现在由 mcbSlotAim 每 tick 按"有没有人管朝向"统一决定。
    mcbSlots.aim = null
    console.info('[mcbridge] releaseAim: 持续注视已松开（接下来交给空闲层 / Baritone）')
    return
  }

  if (action === 'clickSlot') {
    // 点容器/背包里的一个格子。arg 形如 "slot button mode"（button/mode 可省）。
    //
    // 这一个动作同时解锁三件事：开箱子拿东西 / 背包 2×2 合成 / 工作台 3×3 合成。
    //
    //   button: 0 = 左键，1 = 右键
    //   mode  : 0=PICKUP(普通点，拿起/放下)   1=QUICK_MOVE(shift 快速移动，最常用)
    //           2=SWAP(和快捷栏换)            3=CLONE(创造模式复制)
    //           4=THROW(丢出去)               5=QUICK_CRAFT(拖拽涂抹)
    //           6=PICKUP_ALL(双击收同种)
    //
    // 注意： 和 useItemOn 同一条铁律：只调 MultiPlayerGameMode，绝不自己构造
    //    ServerboundContainerClickPacket。 窗口 id、槽位映射、stateId 序列号
    //    全由原版管；自己发包会被服务端判定失序并撤销你的操作（1.21.1 的 sequence 机制）。
    //
    // 抄的是 mineflayer 的 bot.clickWindow(slot, button, mode) —— 它整套 craft 逻辑
    // 就是反复调这一个原语（摆料  ->  拿产物）。算法见 lib/plugins/craft.js。
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

    // 第 3 批 3.5：点格子之前先转过去看着那个容器。
    // 原来开着箱子点格子时她是背对着的 —— 格子点对了，但看着不像在用它。
    // 位置用上一次 useOnAt 的那一格（开容器必然先经过它）；拿不到就不转。
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
    // 也把结果上行回去 —— 服务端日志/RCON 比翻客户端日志方便
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
  try {
    mcbHandle(action, arg)
    console.info('[mcbridge] NETWORK_RECV_OK')
  } catch (e) {
    console.error('[mcbridge] 处理动作失败: ' + e)
  }
})

// --- 定时上行 -------------------------------------------------------------
//
// 注意： 函数体里只能用 var —— 这个回调每 tick 都会跑，用 const/let 会 redeclaration。

let $uplinkTick = 0
let $mcbTick = 0            // 自己数的 tick（朝向槽的 TTL / 空闲扫视的排班都用它）

// 注意： 「按住使用」槽的状态 定义在上面的 mcbSlots 里（use.ticks / use.sawUsing）。
//    别在这儿再声明一遍 —— let mcbSlots.use.ticks = 0 是非法语法，
//    而且就算能写也只是遮蔽，槽的语义就散了。

ClientEvents.tick(() => {
  $mcbTick = $mcbTick + 1

  // ⭐ 每 tick 把每个行为槽施加一次。
  //    注意： 这一句就是"移动 + 转头 + 用东西"能并存的地方 —— 三个槽互不知道对方存在，
  //    各自管各自的通道。抄的是原版 Brain 的 memory 模型
  //    （LOOK_TARGET 和 WALK_TARGET 由不同 sink 消费，天然并存）。
  //    walk 槽不在这儿 —— 那是 Baritone 自己的。
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

// ---- 行为槽的每 tick 施加 ---------------------------------------------------
//
// 只做"施加"，不做"决策" —— 谁该写这个槽是上层的事
// （插件侧那层抢占：反射 > 用户指令 > 当前任务。见 mcbridge_server.js 的说明）。
// 客户端只负责"槽里是什么，我就每 tick 维持什么"。

function mcbTickSlots() {
  mcbSlotAim()
  mcbSlotUse()
  mcbSlotHotbar()
}

// 手上拿什么 —— 第 3 批 3.3。第四个槽，和 walk / aim / use 并列。
//
// 为什么要有：原来"手上是什么"是一次性命令（mcb hotbar / mcb equip），
// 发完就当它一直是那样了。可它随时会被别的东西改掉（另一次点击、她自己的动作），
// 于是插件侧以为手上是镐子、实际拿着火把 —— 这种错很难从日志上看出来。
// 改成槽之后，"手上是它"变成一个每 tick 维持的性质。
//
// 注意： 这个槽只管两件事：
//   1 把选中的快捷栏格按 want.slot 维持住
//   2 want.item 给了的话，在快捷栏里找那个东西，找到就选它
// 它**不搬东西**（背包  <->  快捷栏的交换）—— 那是插件侧 containers.wear 的活，
// 客户端每 tick 去点格子太吵，而且会把正在跑的其它界面操作搅乱。
// 找不到就什么都不做，让插件侧去搬；别每 tick 反复试同一件做不到的事。
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

// 在快捷栏 0~8 里找某个注册名，返回格号；没有回 null。
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

// 朝向槽 —— 每 tick 重设。
// 注意：注意： 这一步不能省：ILookBehavior.updateTarget 设的目标只活一个 tick，
//    PlayerUpdateEvent.POST 末尾会把它清空。不每 tick 重设 = 只锁一帧，
//    下一个 tick 就被 Baritone 抢回去（这正是"移动+转头做不到"的根因）。
//
// ⭐ 2026-10-10：槽从"一个 aim"改成三层（temp / aim / idle，见 mcbSlots）。
//    这里顺便按"有没有人管朝向"自动开关 antiCheatCompatibility：
//    有人管  ->  关（别让 Baritone 抢方向盘）；没人管（它在干活） ->  开（让它正常转）。
//    注意： 不能把"关"写死 —— 关着她走路时不看行进方向，会横着走。
function mcbSlotAim() {
  var rot = mcbAimRotation()
  if (rot === null) {
    mcbSetAntiCheat(true)
    return
  }
  mcbSetAntiCheat(false)
  mcbAimPush(rot)
}

// 只做"把算好的朝向推出去"这一件事（mcbAimApply 是"算 + 推"，给一次性调用用）
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

// 「按住使用」槽 —— 到点、或者她自己用完了，就松手。
// 注意： 这里绝不重发 useItem —— 那会把服务端的倒计时重置回满值，永远吃不完。
//    我们只负责"按下 / 松开"这一个动作，维持使用是原版的事。
function mcbSlotUse() {
  var u = mcbSlots.use
  if (u.ticks <= 0) return
  u.ticks = u.ticks - 1
  var holdMc = mcbMc()
  var holdUsing = false
  try { if (holdMc !== null && holdMc.player !== null) holdUsing = !!holdMc.player.isUsingItem() } catch (eHU) { }
  if (holdUsing) u.sawUsing = true
  // 松手条件：到安全上限，或"确实用过、现在已经结束"
  var done = (u.ticks === 0) || (u.sawUsing && !holdUsing)
  if (done) {
    try { if (holdMc !== null) holdMc.options.keyUse.setDown(false) } catch (eHR) { }
    console.info('[mcbridge] 使用键已松开（' + (u.ticks === 0 ? '到上限' : '她已结束') + '）')
    u.ticks = 0
    u.sawUsing = false
  }
}

console.info('[mcbridge] 脚本已加载（含状态上行 + probe，每 ' + MCB_UPLINK_EVERY_TICKS + ' tick 上行一次）')
