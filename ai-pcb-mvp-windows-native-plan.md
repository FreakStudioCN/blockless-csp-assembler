# AI模块化PCB生成器MVP方案（Windows本机版）

## 目标

做一个能录制视频、能完整表达产品概念的最简MVP。

核心概念不是让LLM从零生成完整PCB，而是复用现成商用模块，例如ESP32、LoRa、GNSS、电源模块、传感器小板，由AI完成模组选型、接口匹配、底板生成、自动布局、自动布线、BOM生成、基础ERC/DRC/DFM检查，并输出JLC EDA可用工程。

首版只面向创客硬件、小批量产品、IoT/传感器/小型控制底板，不处理DDR、高速USB、射频裸电路、手机级消费电子主板等高复杂度设计。

## Demo故事线

推荐固定录屏案例：

> 做一个电池供电的远距离环境采集节点，支持ESP32、LoRa、温湿度传感器、锂电池充电和I2C扩展。

系统自动完成：

1. 从自然语言提取需求。
2. 自动选择模块：ESP32 DevKit、LoRa UART模块、AHT20/BME280传感器模块、TP4056充电模块、JST电池座、I2C扩展口。
3. 自动匹配接口：LoRa接UART，传感器接I2C，电源模块提供5V/3V3/GND。
4. 自动导入或补全模块资料、图片、尺寸、引脚、3D模型。
5. 给用户一个审阅页，只需要确认或修正少量pin map、模块尺寸、3D模型偏移。
6. 自动生成2层底板。
7. 自动布局：RF模块靠板边，天线区域keepout；电源靠边；传感器远离电源；MCU居中。
8. 自动布线：电源线加宽，信号线按保守规则走线。
9. 自动输出BOM、ERC/DRC/DFM报告、EasyEDA Standard JSON/ZIP、3D预览。
10. 在JLC EDA中导入，展示底板和3D效果。

## Windows本机技术栈

首版尽量不用Docker，不依赖复杂EDA安装链。

推荐主栈：

- Node.js 20 LTS
- TypeScript
- Vite + React
- Three.js
- Playwright，用于资料抓取、页面截图和录屏辅助
- JSZip，用于打包EasyEDA工程
- Zod，用于模块数据校验

可选增强：

- KiCad：后续用于更强的ERC/DRC、Gerber、STEP和制造文件导出。
- Freerouting：后续接入成熟自动布线。
- Python：后续用于更复杂几何算法、爬虫或EDA文件处理。

首版建议不要求安装：

- Docker
- WSL
- Java/Freerouting
- KiCad
- Altium
- 本地大模型

## MVP工程结构

```text
ai-pcb-mvp/
  package.json
  src/
    app/                    # React录屏界面
    server/                 # Node本地API
    ai_planner/             # 需求解析和模组选型
    harvester/              # 模块资料、图片、3D模型抓取/导入
    registry/               # 已审阅模块库
    matcher/                # I2C/UART/SPI/GPIO/Power接口匹配
    placer/                 # 约束布局
    router/                 # 简化2层自动布线
    checker/                # ERC/DRC/DFM/BOM检查
    easyeda/                # EasyEDA Standard JSON/ZIP生成
    preview3d/              # Three.js 3D预览
  data/
    modules/                # 模块JSON
    assets/                 # 图片、STEP或参数化3D缓存
    rules/jlc-2layer.json   # 保守制造规则
  output/
    demo-board.json
    demo-board.zip
    bom.csv
    report.json
    preview.png
```

## 模块数据模型

每个现成商用模块先沉淀成结构化JSON：

```json
{
  "id": "esp32-devkit-v1-30pin",
  "name": "ESP32 DevKit V1 30pin",
  "category": "mcu",
  "interfaces": ["wifi", "ble", "uart", "i2c", "spi", "gpio", "power"],
  "power": {
    "input": ["5V", "3V3"],
    "logic": "3V3",
    "current_mA_typ": 120
  },
  "size_mm": { "w": 28, "h": 52 },
  "pins": [
    { "name": "VIN", "type": "power_in", "net": "5V" },
    { "name": "GND", "type": "gnd", "net": "GND" },
    { "name": "GPIO21", "type": "i2c_sda" },
    { "name": "GPIO22", "type": "i2c_scl" }
  ],
  "connector": {
    "kind": "2.54mm_header",
    "rows": 2,
    "pitch_mm": 2.54
  },
  "assets": {
    "image": "data/assets/esp32-devkit.png",
    "model3d": "data/assets/esp32-devkit.glb"
  },
  "review": {
    "status": "verified",
    "notes": "Pin map manually checked for MVP demo."
  }
}
```

## 3D模型策略

为了最快录屏，3D模型分三档：

1. 优先使用可下载的STEP/OBJ/GLB模型。
2. 如果只能抓到模块图片和尺寸，则自动生成参数化3D模型：绿色小PCB、排针、主要芯片块、丝印标签。
3. 如果资料缺失，则生成占位模型，并在审阅页标记为需要人工确认。

这个策略能保证演示稳定：即使真实3D模型抓不到，画面里仍然能出现可信的模块化3D底板。

## 自动化与人工纠错边界

系统尽可能自动化：

- 自动搜索模块候选。
- 自动提取标题、图片、尺寸、引脚表、datasheet链接。
- 自动生成模块JSON草稿。
- 自动推断接口类型。
- 自动生成参数化3D模型。
- 自动生成底板工程。

人工只做少量审阅：

- 确认模块尺寸。
- 确认关键pin map。
- 确认3D模型位置和方向。
- 确认是否接受生成结果。

## 布局算法

首版不用通用布局优化器，使用约束启发式：

- MCU放中间。
- RF/LoRa/GNSS模块靠板边。
- 天线方向外扩，并创建keepout。
- 电源模块靠近电源输入接口。
- 传感器远离电源和高热模块。
- I2C模块靠近MCU对应引脚侧。
- 所有模块保持最小间距。
- 2.54mm排针网格对齐，降低布线复杂度。

## 布线算法

首版实现轻量2层网格布线：

- 使用Manhattan走线。
- 每条net用A*寻路。
- 顶层优先横向，底层优先纵向。
- 信号线默认0.25mm，电源线0.6mm或更宽。
- GND优先生成铺铜或简化为粗线连接。
- 遇到模块外形、keepout、已有走线时避让。

如果布线失败，系统给出明确原因，并允许用户重新布局或手动拖动模块。

## 检查项

首版检查不追求完整EDA级能力，但要能支撑录屏：

- ERC：未连接电源、未连接GND、接口电压不匹配、UART TX/RX方向错误、I2C缺少SDA/SCL。
- DRC：走线宽度、线间距、模块间距、孔到板边距离、keepout冲突。
- DFM：采用JLC 2层板保守规则，检查板尺寸、线宽线距、孔径、元件到板边距离。
- BOM：模块名称、数量、采购链接、LCSC编号或外部采购链接、是否需要焊接排针。

## 输出格式

首版输出：

- EasyEDA Standard JSON/ZIP，作为JLC EDA可导入文件。
- BOM.csv。
- report.json。
- preview.png。
- 可选：module_registry.json，便于展示模块资产库沉淀。

后续增强：

- KiCad工程。
- Gerber。
- Pick&Place。
- STEP。
- 更严格DRC/ERC报告。

## 录屏界面

界面建议做成5步：

1. Prompt：输入自然语言需求。
2. Plan：展示AI拆解出的功能模块和选型理由。
3. Review：展示自动抓取的模块资料，允许少量纠错。
4. Generate：展示布局、布线、检查进度。
5. Export：展示3D预览、BOM、报告和JLC EDA导出包。

录屏时最关键的画面：

- 模块自动出现。
- 引脚连线自动匹配。
- 模块在底板上自动排布。
- 走线生成。
- 3D底板预览。
- 导出JLC EDA工程。

## 开源项目/算法参考

- EasyEDA Standard Format：JLC EDA可导入目标格式。  
  https://docs.easyeda.com/en/DocumentFormat/EasyEDA-Format-Standard/
- EasyEDA标准文件导入说明。  
  https://prodocs.easyeda.com/en/import-export/import-easyeda/
- EasyEDA Pro格式转换器，支持KiCad等格式导入转换，但需要人工检查结果。  
  https://prodocs.easyeda.com/en/import-export/easyeda-pro-format-converter/
- KiCad CLI：后续用于ERC/DRC/BOM/Gerber/STEP等自动化。  
  https://docs.kicad.org/9.0/en/cli/cli.html
- Freerouting：开源PCB自动布线器，后续作为成熟布线后端。  
  https://github.com/freerouting/freerouting
- tscircuit：TypeScript电子设计和PCB生成生态，可参考其Web化和代码化设计思路。  
  https://github.com/tscircuit/tscircuit
- unravel-autorouter：tscircuit生态中的自动布线项目，可参考轻量布线实现。  
  https://github.com/tscircuit/unravel-autorouter
- SKiDL：Python电路描述和网表生成工具，可参考模块化电路表达。  
  https://github.com/devbisme/skidl
- atopile：代码化硬件设计和模块复用方向，可作为中长期参考。  
  https://github.com/atopile/atopile
- easyeda2kicad.py：可参考从EasyEDA/LCSC导入符号、封装和3D模型的方式，注意AGPL许可证约束。  
  https://github.com/uPesy/easyeda2kicad.py

## 最快开发里程碑

### 第1阶段：3-5天

- 做出Web界面骨架。
- 内置5-8个已审阅模块。
- 完成需求到模块方案的规则化生成。
- 完成模块审阅页。
- 完成参数化3D模块预览。

### 第2阶段：5-8天

- 完成接口匹配。
- 完成约束布局。
- 完成简化2层自动布线。
- 完成ERC/DRC/DFM基础检查。
- 完成BOM导出。

### 第3阶段：3-5天

- 完成EasyEDA Standard JSON/ZIP导出。
- 准备固定录屏案例。
- 缓存模块图片和3D资产，避免现场网络波动。
- 打磨UI和录屏路径。

## MVP成功标准

MVP只要满足以下标准，就足够表达完整概念：

1. 用户输入一句需求。
2. 系统自动选择5个左右现成商用模块。
3. 系统自动匹配电源、I2C、UART、GPIO等接口。
4. 系统展示自动导入/补全的模块资料和3D外观。
5. 用户只做少量审阅。
6. 系统自动生成底板、布局、布线。
7. 系统输出BOM和检查报告。
8. 系统输出JLC EDA可导入文件。
9. 录屏中能看到3D底板效果。

## 风险和规避

- 风险：真实模块资料抓取不稳定。  
  规避：预置模块URL和缓存资产，录屏时走缓存优先。

- 风险：真实3D模型不可得。  
  规避：自动生成参数化3D模型，真实模型作为加分项。

- 风险：EasyEDA格式细节复杂。  
  规避：首版只生成非常小的格式子集，并固定2层板、排针模块、简单走线。

- 风险：自动布线失败。  
  规避：模块全部网格对齐，连接数量少，失败时自动放大板子或重新布局。

- 风险：DRC/DFM不够权威。  
  规避：首版做保守规则检查，后续接KiCad CLI或JLC DFM工具流程。

## 最终建议

最快可行的MVP不是“真正通用AI PCB设计器”，而是一个垂直、强约束、可视化很完整的模块化底板生成器。

这个方案适合Windows本机开发和录屏：Node.js + TypeScript + React + Three.js即可跑通主流程；爬取、生成、3D预览、BOM、报告和EasyEDA导出都能在本机完成。Docker、KiCad、Freerouting、Python都可以先不作为必需依赖，等概念验证成立后再逐步接入。
