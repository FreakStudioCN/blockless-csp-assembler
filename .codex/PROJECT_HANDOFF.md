# Blockless PCB Studio Codex Handoff

本文件给新开的 Codex 会话使用。目标是让下一次会话不用重新梳理整段聊天，也能知道这个项目是什么、当前做到哪里、应该继续改什么、哪些地方不能乱改。

## 1. 项目定位

项目名称：Blockless PCB Studio

一句话：面向创客硬件、小批量 IoT、传感器节点、小型控制底板的 AI 模块化 PCB 生成平台。

核心路线不是“LLM 从零生成完整复杂 PCB”，而是复用已验证的商用电子模块，例如 ESP32、LoRa、GNSS、电源模块、传感器小板、显示屏小板等，由 AI 自动完成：

- 模组选型
- 接口匹配
- 底板生成
- 自动布局
- 自动布线
- BOM 生成
- ERC / DRC / DFM 检查
- KiCad / JLC EDA / EasyEDA 风格工程输出
- 3D 预览
- 外壳 CAD 生成
- CNC 刀路生成概念展示

用户真正要的是“能跑的装置”，不是单独一张抽象 PCB。项目要表达的是：用成熟模块作为硬件积木，AI 负责选模块、拼模块、生成底板、生成外壳和制造文件。

## 2. 用户当前要求和审美方向

用户反复强调的方向：

- 要做一个能录制视频的 MVP，但页面上不要显得像 demo/MVP，要假装是实际可用产品。
- Windows 本机可运行，先不要依赖 Docker。
- 页面风格参考 `https://www.blueprint.io/`，重点是布局、信息密度、未来感、国际化，不是照抄色调。
- 色调可以保持当前项目原来的方向，但整体不能廉价、不能像流程式课件。
- 首页要像 Blueprint community，有 12 个公开项目库卡片。
- 项目封面要是装置外观、写实科技风，最好像真实产品，不只是 PCB 或电商图。
- Generate 页面应该是核心录屏点：点击 Generate 后模块从散开状态吸附到 PCB，线路动态生成。
- 可拖拽草图中要能操作元器件位置、方向、固定孔，布线要跟随变化。
- 3D 预览要接近 JLC EDA / KiCad 真实 3D，而不是二维贴图或假板子。
- 用户双击/选择某个 3D 模块后，模块详情卡应该在布局展示区下方，不要遮挡方向切换、固定孔等操作区。
- 右侧真实模块图片和购买链接需要更自然呈现，例如通过 active module 卡片展示，而不是固定堆在右侧占空间。
- 中英文可切换，切英文时项目名、需求描述、按钮、阶段文案都要尽量变成英文。
- CNC toolpath 必须强调真实链路：KiCad PCB -> Gerber / Excellon -> pcb2gcode -> G-code / processed SVG。不要做得像 AI 假图。
- 外壳 CAD 功能需要有生成动画；PCB 固定孔要对应外壳固定柱，传感器需要裸露时外壳要对应开槽。

## 3. 当前工程状态

根目录：`F:\blockless‑csp‑assembler`

当前技术栈：

- Vite
- React 18
- TypeScript
- Three.js
- JSZip
- Playwright 测试
- Windows PowerShell 脚本
- KiCad Host MVP
- KiCadRoutingTools
- pcb2gcode CAM 演示链路

主要 npm 命令：

```powershell
npm install
npm run dev -- --port 5173
npm run typecheck
npm run build
npm run test:ui
npm run harvest:assets
npm run kicad:plans
npm run kicad:validate
npm run kicad:build
npm run kicad:ai-route
```

当前恢复 Codex 的命令：

```powershell
codex --dangerously-bypass-approvals-and-sandbox resume 019fcfd1-4321-7901-9ed4-aedb5442ae0f
```

或：

```powershell
codex --dangerously-bypass-approvals-and-sandbox resume --last
```

## 4. 关键文件地图

### 4.1 前端主入口

- `src/App.tsx`
  - 目前是主要 UI 和交互逻辑集中地。
  - 包含步骤流：`Prompt / Plan / Review / Generate / Export`。
  - 包含中英文切换。
  - 包含首页项目库、Generate 页面、Export 页面。
  - 包含 `BoardSvg`、`Board3D`、`Enclosure3D`、`GenerateView`、`CncToolpathPanel` 等核心组件。
  - 这是下一步 UI 修改最高频的文件。

- `src/styles.css`
  - 全局视觉风格、布局、卡片、Generate 页、CAM 页、外壳页样式。
  - 每次改布局或页面风格后都要检查移动端和桌面端。

### 4.2 数据层

- `src/data/caseRegistry.ts`
  - 公开项目库 / demo cases。
  - 当前已经有 12 个左右项目，但还要继续重排成更像 Blueprint community 的真实项目库。
  - 需要统一封面风格，补足英文标题、英文 prompt、模块组合和工程叙事。

- `src/data/modules.ts`
  - 商用模块库。
  - 已包含 ESP32 Feather、LoRa FeatherWing、BME280、MicroLipo、OLED、SHT31-D、DS3231、Mini GPS PA1010D、Power Relay FeatherWing、LSM6DSOX-LIS3MDL、Adalogger 等。
  - 每个模块包含接口、尺寸、pin、3D 策略、CAD URL、本地模型路径、review 状态。

- `src/data/marketplace.ts`
  - 模块购买链接和图片。
  - 当前有 amazon / lcsc 等字段。下一步应该让 active module 卡片更好展示这些信息。

- `src/domain/types.ts`
  - 领域类型定义。
  - 新增字段前先看这里，避免在 UI 里随意 invent shape。

### 4.3 生成与算法层

- `src/engine/planner.ts`
  - 从 prompt / case 生成模块方案。
  - 后续可扩展为更真实的模块选择策略。

- `src/engine/matcher.ts`
  - 接口匹配。
  - I2C / UART / SPI / GPIO / Power 等连接关系在这里或相关 engine 内生成。

- `src/engine/placer.ts`
  - 约束式布局。
  - 应继续增强 RF 靠边、GNSS 露天、传感器远离热源、电源靠输入口、屏幕朝外等规则。

- `src/engine/router.ts`
  - 前端草图里的轻量布线。
  - 当前要重点修：避免明显交叉、避免看起来太假、模块拖动后要动态 reroute。
  - 真实铜线以 KiCad / KRT 输出为准，前端草图可以是伪布线，但要看起来工程化。

- `src/engine/checker.ts`
  - ERC / DRC / DFM 检查逻辑。

- `src/engine/exporter.ts`
  - EasyEDA / JLC EDA 风格 JSON、BOM、报告和 ZIP 导出。

- `src/engine/enclosure.ts`
  - 参数化外壳生成。
  - 已支持方盒、安装柱、顶面开孔、侧面开孔、OpenSCAD 输出。
  - 后续重点：固定孔和 standoff 联动；传感器、屏幕、USB、天线位置生成开槽。

- `src/engine/geometry.ts`
  - PCB 坐标、场景坐标、pin 坐标等几何工具。

### 4.4 真实 CAD / 图片 / 制造资产

- `public/assets/real-cad/`
  - 真实 CAD 缓存：STL / STEP / 3MF / JPG / GIF。
  - `manifest.json` 需要保持更新。
  - 已知问题：有些资产可能不完整，尤其要检查 `featherwing-adalogger.step`、`oled-1-5in` 相关资产。

- `public/assets/marketplace/amazon/`
  - 模块电商图片缓存。

- `public/assets/kicad-svg/`
  - Web 使用的 KiCad 顶层/底层 SVG。

- `public/assets/kicad-renders/`
  - Web 使用的 KiCad 3D render。

- `public/assets/cam-real/`
  - pcb2gcode 真实 CAM 输出缓存，包括 G-code、processed SVG、traced SVG、manifest。

### 4.5 KiCad Host MVP

- `kicad-host-mvp/README.md`
  - 真实 EDA 方向说明。

- `kicad-host-mvp/plans/*.json`
  - AI 生成的模块计划 JSON。

- `kicad-host-mvp/plugin/ai_modular_backplane/`
  - KiCad Action Plugin。
  - `board_builder.py` 负责创建真实 KiCad board object。

- `kicad-host-mvp/scripts/generate_plan.py`
  - 从前端/数据生成 KiCad plan。

- `kicad-host-mvp/scripts/build_boards.ps1`
  - 生成 KiCad PCB。

- `kicad-host-mvp/scripts/ai_route_with_kicad_routing_tools.ps1`
  - 调用 KiCadRoutingTools 自动布线。

- `kicad-host-mvp/scripts/enhance_kicad_svg.py`
  - 增强 KiCad SVG 可读性。
  - 后续应继续让顶层/底层 SVG 的 PCB 边框、模块丝印、铜线层级更清楚。

- `kicad-host-mvp/scripts/export_cnc_toolpaths.ps1`
  - KiCad CLI + pcb2gcode 真实 CAM 输出链路。

## 5. 当前已经实现的功能

### 5.1 Web 产品主流程

- 首页 Prompt。
- 公开项目库卡片。
- Plan：展示 AI 拆解模块。
- Review：展示模块资料和审核状态。
- Generate：展示 KiCad 真实输出 / 可拖拽草图 / 外壳 CAD。
- Export：展示 BOM、检查结果、3D 和导出包。

### 5.2 交互功能

- 选择公开项目。
- 中英文切换。
- Generate 过程有延迟进度动画。
- 可拖拽草图支持模块拖动。
- 支持模块旋转。
- 支持重新生成草图布线。
- 支持固定孔数据结构和显示。
- 3D 视图支持 OrbitControls。
- 3D 模块可选择，active module 卡片展示模块信息。

### 5.3 3D / CAD

- `Board3D` 支持 STL 和 3MF 加载。
- 没有真实模型时会用参数化模块模型。
- 支持颜色、芯片块、排针、标签、焊盘等细节。
- `Enclosure3D` 支持透明外壳预览和生成动画。
- `enclosure.ts` 可以输出 OpenSCAD。

### 5.4 KiCad / 路由 / CAM

- 可生成真实 `.kicad_pcb`。
- 已接入 KiCadRoutingTools。
- 可生成顶层/底层 SVG 和 KiCad 3D render。
- 可通过 KiCad CLI + pcb2gcode 生成 CNC toolpath。
- Web CAM 面板已经有图层切换、参数文字和真实输出说明。

## 6. 当前待修改项

优先级按对录屏和产品感知的影响排序。

### P0：首页项目库重做

目标：更像 Blueprint community，而不是 demo 流程页。

要做：

- `src/data/caseRegistry.ts` 中 12 个项目重排。
- 每个项目有中英文标题、真实需求描述、模块组合、行业场景。
- 封面图统一为“装置外观 / 写实科技风”，不要混用 PCB 截图、电商图和工程图。
- 项目卡片信息密度更像公开项目库：名称、类别、模块数量、状态、制造输出。
- 首页不要强调 MVP / demo。

### P0：Generate 页面动效强化

目标：点击 Generate 后出现一段“模块散开 -> 吸附到 PCB -> 线路动态生成”的炫酷过程。

要做：

- 检查 `runGenerate`、`GenerationProgressOverlay`、`GenerateMotionOverlay`。
- 动画要和真实 board placements 对齐。
- 线路生成要像工程生成过程，而不是随机画线。
- 动效结束后进入可交互布局页面。

### P0：可拖拽草图布线修正

用户最敏感的问题：布线一眼看起来不对，交叉、直角、层切换不自然。

要做：

- 优先改 `src/engine/router.ts` 和 `BoardSvg`。
- 前端草图可以使用“伪布线”，但必须分顶层/底层，视觉上不能明显交叉。
- 动态拖动元器件后布线必须同步变化。
- 走线尽量用斜角、圆角或可解释的层切换，不要全是简单直角。
- 电源线、GND、I2C、UART 要有不同线宽/颜色。
- 真实铜线仍以 KiCad/KRT 输出为准，文案要表达清楚。

### P0：模块详情卡位置和内容

目标：双击或选中 3D 模块后，在布局展示页面下方显示 active module 卡片，不遮挡工具区。

要做：

- 检查 `ModuleInspector`、`SourceCard`、Generate 页面 active module 位置。
- active module 卡片展示：
  - 真实图片
  - 名称
  - 接口
  - 尺寸
  - 电压
  - CAD 状态
  - Amazon / LCSC / source links
  - 采购 CTA
- 不要遮挡“切换元器件方向 / 放置定位孔 / 重新布线”等操作。

### P1：固定孔和外壳 CAD 联动

目标：用户放置固定孔后，外壳里自动出现对应固定柱。

要做：

- 检查 `defaultMountingHoles`、`addMountingHole`、`mountingHoles` state。
- 确保工具区真的有“放置固定孔”交互，而不是只有数据。
- 固定孔要能在 2D 草图中显示清楚。
- 外壳 CAD 中 standoff 数量和位置跟随固定孔。
- 后续可加删除固定孔、锁定固定孔、孔径选择。

### P1：KiCad 顶层/底层 SVG 可读性

用户反馈：KiCad 真实输出中顶层/底层模块丝印边框和 PCB 边框看不清。

要做：

- 优先检查 `public/assets/kicad-svg/*` 和 `kicad-host-mvp/scripts/enhance_kicad_svg.py`。
- 增强：
  - PCB 外框
  - 模块轮廓
  - 丝印标签
  - 铜线层级
  - via / pad 视觉
- Web 展示时不要让 SVG 被 CSS 压暗或缩到看不清。

### P1：真实 CAD 资产补齐

要做：

- 检查 `public/assets/real-cad/manifest.json`。
- 检查所有新增模块是否有 `localUrl` / `sourceUrl` / `model3d`。
- 特别注意：
  - `featherwing-adalogger.step`
  - `oled-1-5in` 相关资产
- 如果真实模型无法拿到，明确标为 parametric fallback，不要假装是真实 CAD。

### P1：CNC toolpath 面板工程感

目标：看起来像真实工程转换，不像 AI 生成图片。

已经有：

- `CncToolpathPanel`
- KiCad source view
- pcb2gcode processed SVG
- 文件列表 / 参数说明 / manifest

还要做：

- 图层切换更清楚。
- 参数文字更工程化。
- G-code 文件大小、tool diameter、isolation passes 等参数更显眼。
- 如果本机工具没就绪，清楚说明是使用缓存还是等待真实后台。

### P1：外壳 CAD 生成动画

目标：点击外壳 CAD 或生成外壳 CAD 后，有一段明确生成动画。

已经有：

- `Enclosure3D`
- `enclosureStatus`
- `enclosureAnimationKey`
- `generateEnclosureCad`

还要做：

- 动画更明显：板子、固定柱、墙体、顶盖、开孔逐步出现。
- 参数面板显示外壳尺寸、壁厚、间隙、固定柱、开孔数量。
- 对传感器、屏幕、USB、天线等开孔逻辑做更强视觉表达。

### P2：页面整体去“流程感”

当前 Stepper 仍然让页面像录屏流程。

要做：

- 保留流程能力，但视觉上改成产品工作台。
- 首页是 project gallery + prompt。
- Generate 是主工作台。
- Plan / Review / Export 可以像 tabs 或 inspector，不要像课件步骤。
- 文案中避免 “MVP / demo / 演示级”。

### P2：移动端和文字溢出

要做：

- 检查所有中文/英文切换后的按钮宽度。
- Project title、prompt、Active module 等英文模式不能仍显示中文。
- 移动端卡片不能互相遮挡。

## 7. 重要设计原则

### 7.1 真实性优先

用户已经多次指出“不真实”“像 AI 假生成”。因此：

- 真有 KiCad 输出就展示 KiCad 输出。
- 真有 pcb2gcode 输出就展示真实 G-code / SVG。
- 真有 STL / 3MF 就加载真实模型。
- 没有真实数据时，要做可信 fallback，但不要文案上假装是真实。

### 7.2 不要把 React 做成完整假 EDA

`kicad-host-mvp/README.md` 里已经明确：

```text
React AI console -> module plan JSON -> KiCad board synthesis -> KiCadRoutingTools autoroute -> KiCad DRC / 3D viewer
```

React 是 AI console、展示工作台、交互草图。真实 EDA 后端应尽量交给 KiCad / KiCadRoutingTools / pcb2gcode。

### 7.3 AI 边界要讲准确

当前 KiCadRoutingTools 是确定性 Rust A* autorouter，不是训练出的 AI router。

对外口径应该是：

- AI/LLM 负责需求理解、模块选择、接口匹配、约束生成、布局策略、DRC 反馈解释。
- Autorouter 负责具体铜线搜索。
- 检查器负责规则验证。
- 未来可以做闭环 AI routing，但当前不要虚假宣称。

### 7.4 Windows-native

用户明确要求先本机跑，尽量不要求 Docker / WSL。

因此：

- 命令优先写 PowerShell。
- 脚本优先兼容 Windows。
- 不要把 Docker 加成必需依赖。

## 8. 推荐下一步执行顺序

如果新 Codex 接手，建议按这个顺序做：

1. 运行 `npm run typecheck` 看当前是否干净。
2. 打开 `src/App.tsx`，定位 `PromptView`、`GenerateView`、`ModuleInspector`、`BoardSvg`、`Board3D`。
3. 打开 `src/data/caseRegistry.ts`，重排 12 个项目库。
4. 优化首页项目卡 UI 和封面图策略。
5. 优化 Generate 页面布局，让 active module 卡片在预览区下方。
6. 修 `src/engine/router.ts` 和 `BoardSvg`，减少草图布线交叉。
7. 确认固定孔功能可实际操作，外壳 standoff 跟随。
8. 调整外壳 CAD 动画和参数展示。
9. 优化 KiCad SVG 可读性，必要时跑 `enhance_kicad_svg.py` 或相关脚本。
10. 更新真实 CAD manifest。
11. 跑 `npm run typecheck`、`npm run build`。
12. 启动 `npm run dev -- --port 5173`，用浏览器检查首页和 Generate 页。
13. 如涉及 3D/Canvas，必须截图或用 Playwright 检查非空渲染。

## 9. 验证清单

完成修改后至少检查：

- `npm run typecheck`
- `npm run build`
- 首页 12 个项目卡是否显示正常。
- 中文/英文切换是否完整。
- 选择项目后 prompt 是否跟着变。
- 点击 Generate 是否有延迟进度动画。
- Generate 页面三个 preview mode 是否可切换：
  - KiCad 真实输出
  - 可拖拽草图
  - 外壳 CAD
- 可拖拽草图：
  - 模块可拖动
  - 模块可旋转
  - 布线跟随变化
  - 固定孔可放置
  - active module 卡片不遮挡工具区
- 3D 视图：
  - 不空白
  - 模块颜色和真实图片/模型尽量合理
  - 选择模块有反馈
- 外壳 CAD：
  - 点击后有动画
  - 固定柱数量和固定孔一致
  - 开孔列表合理
  - 可下载 `.scad`
- CNC：
  - 图层切换正常
  - 能看出 KiCad -> Gerber -> pcb2gcode -> G-code 链路
  - 使用缓存或后台生成时状态明确

## 10. 已知风险

- `src/App.tsx` 很大，改动时要避免无关重构。
- 工作区有很多生成物和历史输出，尤其 `kicad-host-mvp/generated`、`public/assets/cam-real`、`dist`、`test-results`，不要随意删除或回滚。
- 如果 git 工作区已有非本次改动，按用户改动处理，不要 reset。
- 真实 CAD 抓取不稳定。视频演示要优先使用本地缓存。
- KiCad / pcb2gcode 是否安装、路径是否正确依赖本机环境。Web 上要能优雅展示缓存状态。
- 当前自动布线不应夸大成“训练好的 AI PCB router”。

## 11. 给下一次 Codex 的直接提示词

可以在新会话里直接说：

```text
请先阅读 F:\blockless‑csp‑assembler\.codex\PROJECT_HANDOFF.md，然后继续优化 Blockless PCB Studio。优先处理首页 12 个公开项目库、Generate 页面交互布局、active module 卡片、可拖拽草图布线、固定孔和外壳 CAD 联动。不要回滚现有生成物。完成后跑 typecheck/build，并启动本机 dev server 给我看。
```

## 12. 最近一次高层进度摘要

已经完成：

- 主流程已接好。
- 3D 预览可用。
- STL / 3MF 真实模型可加载。
- 参数化外壳可生成。
- KiCad Host MVP 已接入。
- KiCadRoutingTools 可生成真实 routed PCB。
- pcb2gcode 真实 CAM 链路已有 Web 面板。
- 中英文切换已有基础。
- `npm run typecheck` 和 `npm run build` 之前曾通过。

主要还差：

- 更像真实产品的首页。
- 更自然的 Generate 工作台布局。
- 更可信的草图布线。
- 更清晰的 KiCad SVG。
- 更完整的固定孔/外壳联动。
- 更完整的真实模块图片、购买链接和 CAD manifest。
- 更强的录屏级生成动画。
