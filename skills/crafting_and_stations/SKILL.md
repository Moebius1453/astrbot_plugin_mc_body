---
name: crafting_and_stations
description: How crafting, smelting and containers really work here — reusing stations already in the world, picking the recipe you actually have materials for, and why a click receipt is not proof.
---

# 合成、冶炼、开箱子

## 复用世界里的台子，别重复搭

世界里已经有工作台/熔炉/箱子（主人搭的、你以前搭的）。
先找现成的用（mc_around 扫，mc_place 记），找不到再自己搭一个。

你搭过一次的台子值得记下来 —— 下次直接去那儿，别再搭第二个。
（这条是抄来的原话：「Reuse the world: a station you set up once is worth a note」。）

## 挑配方：挑你真有料的那一条

同一个产物往往有好几条配方（原版一条、mod 一条、替代材料一条）。
规划时优先挑"你身上已有材料"的那条，否则会出现"她说缺铁矿石，可她明明揣着生铁"。
程序侧已经按这个顺序挑了（手上有料 > 缺口少 > 原版优先），你看到"缺 X"时先信它。

## 要工作台的配方，优先往后放

如果两条路一样近，优先挑不需要工作台的（背包 2×2 就能做）。
理由：要台子就得跑一趟、还得开容器 —— 而"开容器"本身是最容易出岔子的一步。

## 注意： 回执 ≠ 成功

"点了格子"不等于"东西动了"，"开了箱子"不等于"东西进去了"。

- 服务端静默丢弃对不上号的操作 —— 界面 id 一变，点击就落空，
  两边都不报错，只有世界没变。
-  ->  判成功的唯一标准是：背包/容器的前后真的不一样了。
-  ->  所以别用总库存代替你要的那件东西，别在没核对前说"放好了"。

## 界面 id 会变

界面被顶掉（又开了别的容器）、她自己挪了一下、快照过期 —— 都会让算出来的格号指错格子。
 ->  所以开容器  ->  操作  ->  关容器这一串要连着做完，中间别插别的事。

## 分工

- 合成/冶炼：调 mc_craft / mc_smelt，它们是完整流程，你给目标就行。
- 手动翻箱子：mc_use_on 开  ->  mc_click 点  ->  mc_menu 看  ->  记得关。
- 别做的事：别自己编配方表（问 mcb recipe 就有），别用显示名匹配物品（用注册名）。
