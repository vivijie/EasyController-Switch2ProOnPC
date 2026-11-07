# EasyController

> 把 **Switch 2 Pro 手柄** 变成 PC 上开箱即用的标准 **XInput 手柄**，专为 FPS 调校。

插上手柄 → 双击启动 → 浏览器里调曲线和死区 → 进游戏就能玩。

支持游戏：**远光84** · **Apex 英雄**

---

## 它解决什么问题

Switch 2 Pro 手柄在 PC 上能识别，但直接用会撞上四堵墙：

| 痛点 | 表现 | EasyController 的做法 |
|---|---|---|
| 行程不满 | 物理摇杆只能推到 ±0.8，永远打不满 | 校准 + 曲线重映射到完整 1.0 |
| 死区打架 | 游戏自带死区吃掉了微调区间 | 反死区：负中心死区 + 边缘提前满值，把行程补回来 |
| 手感不对 | 摇杆输出形状方块化、有顿挫 | 圆形/方形连续混合 + 自定义响应曲线（最多 20 点） |
| 背键/截图键没用 | PC 上这些键没有对应功能 | 任意按键 → 虚拟手柄键 或 键盘按键（最多 3 键同时按） |

全程**虚拟手柄输出**，游戏里看到的就是一个标准 Xbox 360 手柄。

---

## 特性

**网页界面，三个标签页**

- **连接手柄** —— 连接状态、读取速率（约 650 Hz）、PC 手柄顺序说明
- **键位设置** —— 点手柄图上的按键即可改键，实时显示"手柄键值 / 键盘键值"
- **摇杆设置** —— 校准、响应曲线、方圆混合、死区，全部带实时图形预览

**摇杆**

- 分步校准：先采中心点，再沿边缘转 3~4 圈采行程，绿色扇形环实时反馈覆盖度
- 支持正/负中心死区、0~30% 边缘死区
- 内置 Apex 反死区预设（左：中心 -5% / 边缘 10%，右：0 死区）
- 曲线支持预设（线性、Sacred）与自定义保存，黄色虚线预览目标曲线

**按键**

- 手柄键值、键盘键值双显示，手柄图上直接显示**实际输出**（改键后立刻变）
- 键盘按键支持 3 个键位槽，点一下按键即可录入，无需手打组合键
- 一键恢复出厂按键映射

**其它**

- 断线自动检测，插拔后一键重连
- 配置自动保存（改动后 5 秒自动落盘），也可手动保存
- 右上角「手柄测试网站」直达 hardwaretester，随时验证输出

---

## 快速开始（推荐）

**第 1 步** 下载并解压本仓库

**第 2 步** 打开 `1 DSH.txt`，把内容**整段**发给 DeepSeek Harness

> DSH 会自动完成：安装 Python 3.11 → 装依赖 → 检查 ViGEmBus 驱动 → 自检 → 首次连接手柄 → 拉起服务

**第 3 步** 之后每次使用，双击 `2 双击启动.bat` 即可

---

## 手动安装（不想用 DSH）

```bash
# 1. 环境：Python 3.11+
python --version

# 2. 依赖
cd EasyController
python -m pip install -r app/requirements.txt

# 3. 自检
python app/run_sticklab.py --check-only

# 4. 启动
python app/run_sticklab.py
```

然后浏览器打开 `http://127.0.0.1:8899`（端口被占用时会自动顺延，以窗口打印的地址为准）。

### 额外需要：ViGEmBus 驱动

`vgamepad` 靠它在 Windows 上创建虚拟 Xbox 手柄，**没装就完全没有虚拟手柄**。

- 装法一：`winget install -e --id Nefarius.ViGEmBus`
- 装法二：[ViGEmBus Releases](https://github.com/nefarius/ViGEmBus/releases) 下载安装

装好后设备管理器里能看到 `Nefarius Virtual Gamepad Emulation Bus`。

---

## 使用说明

### 连接手柄

1. USB 数据线连接手柄（**必须是数据线**，很多线只供电）
2. 设备管理器确认出现 `Switch 2 Pro Controller`
3. 网页点【重新连接】

### 首次使用一定要校准

摇杆页 →【校准摇杆】：

1. **松手静止**，等中心点采样完成
2. 出现「请沿着边缘转动摇杆3圈」时，**贴着边缘慢速转 3~4 圈**，把每个方向都推到极限
3. 右侧绿色环基本闭合 = 覆盖够了

校准结果会记住，**下次启动不需要重做**。只有换了手柄或手感明显不对时才需要重新校准。

### 进游戏还是认到物理手柄？

两种可能：

1. **Windows 里有两个手柄** —— 打开「游戏控制器」，把虚拟手柄设为首选设备
2. **Steam 接管了手柄** —— Steam 库 → 右键游戏 → 属性 → 控制器 → **禁用 Steam 输入**

界面右上角「手柄测试网站」可以随时确认浏览器里看到的是哪个手柄。

---

## 推荐设置

### 远光84

| 项目 | 值 |
|---|---|
| X 轴灵敏度 | 260 |
| Y 轴灵敏度 | 160 |
| XY 加速度 | 200 |
| 开镜灵敏度 | 0.5 |
| 摇杆曲线 | 线性或轻微加速 |
| 方圆 | 左方右圆（方 = 30%） |
| 死区 | 中心 0%，边缘 5% |

> 用小鸡灵狐、Switch Pro、Switch 2 Pro 反复测试，这套手感最稳。后来看到 Apex 的「线性 43」设置，数值很接近——说明手柄 FPS 受摇杆物理限制，存在一个"物理最优解"。
>
> 其他手柄可以微调：摇杆回弹越轻（如小鸡灵狐），灵敏度适当调低。

### Apex 英雄

直接用死区页的 **【Apex 左摇杆反死区】** 预设：

- 左摇杆：中心 **-5%**（放大，对冲游戏死区）、边缘 **10%**（提前满值）
- 右摇杆：**0 死区**（瞄准要求线性）

曲线建议 Sacred 或自定义的渐进加速。

---

## 目录结构

```
EasyController/
├── 0 使用说明.txt        三句话上手
├── 1 DSH.txt             给 DeepSeek Harness 的安装指令
├── 2 双击启动.bat        启动脚本
├── app/                  程序本体
│   ├── run_sticklab.py   入口
│   ├── requirements.txt  依赖清单
│   ├── sticklab/         引擎 / 读取层 / 网页服务
│   └── data/             配置（删掉 = 恢复出厂）
├── dev/                  开发资料（普通使用无需关心）
└── README.md
```

配置保存在 `app/data/sticklab_config.json`，包含校准数据、曲线点、死区参数、按键映射。

---

## 常见问题

<details>
<summary><b>双击 bat 闪一下就没了</b></summary>

Python 没装或没加进 PATH。安装 Python 时勾选 **Add Python to PATH**，或重开终端再试。
</details>

<details>
<summary><b>检测不到手柄</b></summary>

按顺序排查：

1. 换一根确定能传数据的 USB 线
2. 换一个 USB 口（优先主板后置口）
3. 关掉 Steam / 模拟器等可能独占手柄的程序
4. 设备管理器里是否真的出现 `Switch 2 Pro Controller`
</details>

<details>
<summary><b>连上了，但游戏里没反应</b></summary>

1. 网页里确认「虚拟 XInput 输出」处于开启状态
2. 打开 Windows「游戏控制器」，确认虚拟手柄在列表里
3. 两个手柄同时在的话，把虚拟手柄设为首选设备
4. 游戏内把输入设备选成 Xbox 手柄
</details>

<details>
<summary><b>每次都要重新校准</b></summary>

正常情况下校准会保存。如果需要每次都校准：

1. 确认 `app/data/sticklab_config.json` 存在且可写（不是只读、没被杀毒软件拦截）
2. 启动日志里应该有 `[校准] 已载入上次校准数据: <时间>`
3. 没有的话说明配置没读到，检查是不是把程序目录放在 OneDrive 等同步目录里被锁了
</details>

<details>
<summary><b>摇杆中心偏 / 推不到边</b></summary>

重新校准：贴着边缘慢转 3~4 圈，确保每个方向都推到极限。
</details>

<details>
<summary><b>想恢复默认</b></summary>

键位页和摇杆页都有【重置设置】按钮。或直接删掉 `app/data/sticklab_config.json`。
</details>

---

## 硬件改造小贴士

- **背键**：Switch Pro 系列背键又低又小，不好按。贴一片普通防撞贴（家具用的那种）把高度垫起来，手感立竿见影。
- **摇杆帽**：原装帽偏矮，换第三方加高摇杆帽能明显提升微调精度。

---

## 技术栈

Python 3.11 · Flask · pygame(SDL2) · vgamepad · pyusb · hidapi

- 物理手柄读取：SDL2（pygame），约 650 Hz
- 虚拟手柄输出：vgamepad → XInput
- 网页实时数据：SSE + requestAnimationFrame 插值
- 键盘模拟：SendInput + 真实扫描码（兼容 Raw Input 游戏）

---

## 作者

**@搅局打游戏** · [B 站主页](https://space.bilibili.com/644457478)

手柄检测：[hardwaretester.com/gamepad](https://hardwaretester.com/gamepad)

## License

[MIT](LICENSE)
