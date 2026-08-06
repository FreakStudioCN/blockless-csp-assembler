# AI模块化PCB生成器单天展示Demo方案

## 定位

这是一份单天可实现、用于录屏展示概念的Demo方案。

原来的完整MVP方案继续保留，本方案是更快的演示版本。目标不是一天内做出可量产PCB工具，而是做出一个能让观众理解产品范式的可运行工程：

> AI不从零生成完整PCB，而是从已验证商用模块中选型，自动匹配接口，并生成模块底板的可视化结果、BOM、检查报告和导出包。

## 单天Demo成功标准

一天内只要求“能展示”：

1. 有一个本地Web页面可以打开。
2. 用户输入一句自然语言需求。
3. 系统自动生成模块方案。
4. 系统展示接口匹配结果。
5. 系统展示模块资料审阅页。
6. 系统展示自动布局和自动布线效果。
7. 系统展示3D底板预览。
8. 系统生成BOM和检查报告。
9. 系统生成一个可下载的JLC EDA/EasyEDA演示导出包。

允许简化：

- 模块库预置，不做真实全网爬取。
- 3D模型用参数化模型，不强求真实STEP/GLB。
- 自动布局用固定规则。
- 自动布线用SVG/Canvas路径或简化网格算法。
- EasyEDA导出包可以先是演示级JSON结构，后续再补齐严格格式。
- DRC/ERC/DFM检查先做规则化演示，不等同完整EDA检查。

## 推荐Demo案例

固定案例：

> 做一个电池供电的远距离环境采集节点，支持ESP32、LoRa、温湿度传感器、锂电池充电和I2C扩展。

自动选择模块：

- ESP32 DevKit V1 30pin
- LoRa UART模块
- AHT20或BME280温湿度传感器模块
- TP4056锂电池充电模块
- JST-PH 2Pin电池座
- I2C Grove/Qwiic扩展接口

## 单天工程技术栈

只用Windows本机容易跑起来的栈：

- Node.js
- TypeScript
- Vite
- React
- Three.js
- JSZip

首日不使用：

- Docker
- WSL
- KiCad
- Freerouting
- Python爬虫
- 本地大模型
- 真实Gerber生成

## 页面流程

### 1. Prompt页

展示一个输入框，默认填入：

```text
做一个电池供电的远距离环境采集节点，支持ESP32、LoRa、温湿度传感器、锂电池充电和I2C扩展。
```

点击“生成方案”后，进入Plan页。

### 2. Plan页

展示AI拆解结果：

- 主控：ESP32 DevKit
- 通信：LoRa UART模块
- 传感：AHT20/BME280 I2C模块
- 电源：TP4056充电模块 + 电池座
- 扩展：I2C接口

展示每个模块的选择理由，例如：

- ESP32适合Wi-Fi/BLE和低成本创客项目。
- LoRa UART模块能避免直接设计射频电路。
- AHT20/BME280模块已集成传感器外围电路。
- TP4056模块复用成熟锂电充电方案。

### 3. Review页

展示一个模块资料表，字段包括：

- 模块名称
- 尺寸
- 接口
- 电压
- 关键引脚
- 3D模型状态
- 审阅状态

单天版数据来自预置JSON，但界面文案可以显示：

- 已从缓存模块库加载
- 需要确认pin map
- 已生成参数化3D模型

允许用户点击“确认全部”。

### 4. Generate页

展示底板生成过程：

1. 生成板框。
2. 放置ESP32。
3. 放置LoRa，并添加天线keepout。
4. 放置传感器模块。
5. 放置电源模块和电池座。
6. 匹配I2C、UART、电源网络。
7. 自动生成走线。
8. 运行ERC/DRC/DFM检查。

这部分可以用进度条和SVG/Canvas动画表达，不要求真实EDA级编辑器。

### 5. Export页

展示：

- 2D PCB预览
- 3D模块化底板预览
- BOM表
- ERC/DRC/DFM检查结果
- 下载按钮：
  - `demo-board.easyeda.json`
  - `demo-board-jlc-demo.zip`
  - `bom.csv`
  - `report.json`

## 数据结构

### 模块JSON

```json
{
  "id": "esp32-devkit-v1",
  "name": "ESP32 DevKit V1",
  "category": "mcu",
  "size_mm": { "w": 28, "h": 52 },
  "interfaces": ["uart", "i2c", "spi", "gpio", "wifi", "ble", "power"],
  "voltage": ["5V", "3V3"],
  "pins": [
    { "name": "VIN", "type": "power", "net": "5V" },
    { "name": "GND", "type": "gnd", "net": "GND" },
    { "name": "GPIO21", "type": "i2c_sda", "net": "I2C_SDA" },
    { "name": "GPIO22", "type": "i2c_scl", "net": "I2C_SCL" },
    { "name": "GPIO16", "type": "uart_rx", "net": "LORA_TX" },
    { "name": "GPIO17", "type": "uart_tx", "net": "LORA_RX" }
  ],
  "model3d": {
    "mode": "parametric",
    "bodyColor": "#1f7a4d",
    "label": "ESP32"
  }
}
```

### 连接网络

```json
[
  { "net": "5V", "from": "tp4056.OUT+", "to": ["esp32.VIN", "lora.VCC"] },
  { "net": "3V3", "from": "esp32.3V3", "to": ["sensor.VCC", "i2c_ext.VCC"] },
  { "net": "GND", "from": "battery.GND", "to": ["esp32.GND", "lora.GND", "sensor.GND"] },
  { "net": "I2C_SDA", "from": "esp32.GPIO21", "to": ["sensor.SDA", "i2c_ext.SDA"] },
  { "net": "I2C_SCL", "from": "esp32.GPIO22", "to": ["sensor.SCL", "i2c_ext.SCL"] },
  { "net": "LORA_TX", "from": "lora.TX", "to": "esp32.GPIO16" },
  { "net": "LORA_RX", "from": "lora.RX", "to": "esp32.GPIO17" }
]
```

## 布局规则

单天版直接使用固定启发式：

- 板子尺寸：80mm x 55mm。
- ESP32放中间偏左。
- LoRa放右上角，靠近板边。
- LoRa天线区域画出keepout。
- TP4056放左下角，靠近USB/电池入口。
- 电池座放底边。
- 传感器放右下角，远离电源模块。
- I2C扩展口放右侧边缘。

## 布线规则

单天版使用可视化布线：

- 电源线用红色粗线。
- GND用黑色粗线或铺铜区域表达。
- I2C用蓝色线。
- UART用绿色线。
- 所有走线走直角路径。
- 若路径冲突，自动加一个via点或绕行。

这足够展示“自动布线”概念。后续再替换成真实网格A*或Freerouting。

## 检查报告

单天版生成固定但可信的检查项：

```json
{
  "erc": [
    { "level": "pass", "message": "All power nets connected" },
    { "level": "pass", "message": "I2C SDA/SCL matched" },
    { "level": "pass", "message": "UART TX/RX crossed correctly" }
  ],
  "drc": [
    { "level": "pass", "message": "Minimum trace width >= 0.25mm" },
    { "level": "pass", "message": "Minimum clearance >= 0.25mm" },
    { "level": "warn", "message": "LoRa antenna keepout requires manual review" }
  ],
  "dfm": [
    { "level": "pass", "message": "2-layer JLC prototype rule profile selected" },
    { "level": "pass", "message": "Board outline within low-cost prototype range" }
  ]
}
```

## JLC EDA导出策略

单天版导出一个演示包：

```text
demo-board-jlc-demo.zip
  demo-board.easyeda.json
  bom.csv
  report.json
  README.md
```

`demo-board.easyeda.json`先表达：

- board outline
- module placements
- nets
- tracks
- labels
- keepout

首日不保证覆盖EasyEDA完整生产级格式，但文件命名和结构要为后续接入真实EasyEDA Standard JSON保留位置。

## 一天开发排期

### 上午：工程和数据

- 创建Vite React工程。
- 内置6个模块JSON。
- 写死一个需求到模块方案的planner。
- 实现模块审阅表。
- 实现接口匹配表。

### 下午：可视化和生成

- 实现2D PCB SVG预览。
- 实现走线可视化。
- 实现Three.js参数化3D预览。
- 实现BOM和检查报告。
- 实现ZIP导出。

### 晚上：录屏打磨

- 固定一个完整演示路径。
- 加载动画和步骤状态。
- 准备导出文件。
- 录制演示视频。

## 需要砍掉的内容

为了确保一天能完成，以下内容不进入单天版：

- 真实全网模块爬取。
- 真实STEP/GLB自动下载和转换。
- 真实EasyEDA完整格式适配。
- 真实Gerber输出。
- 真实JLC DFM联动。
- 复杂自动布线器。
- 任意自然语言需求泛化。
- 任意模块库扩展。
- 多种PCB场景。

## 视频话术

可以这样讲：

> 传统AI PCB方案试图让LLM从零生成完整电路和PCB，可靠性很难保证。我们的路线是把已经验证过的商用模块作为硬件积木，AI只做模块选择、接口匹配和底板生成。这样能把问题从“自由硬件设计”降维成“模块编排和底板自动化”，更适合创客硬件和小批量产品。

录屏重点展示：

- AI选择模块。
- 系统解释为什么选这些模块。
- 接口自动匹配。
- 用户只做轻量审阅。
- 自动生成底板。
- 自动布局布线。
- 3D预览。
- BOM和检查报告。
- JLC EDA导出包。

## 与完整MVP方案的关系

本方案是单天展示版，用于最快验证概念和录屏。

原完整MVP方案继续保留，用于后续真实工程化：

- 加入真实模块资料爬取。
- 加入真实EasyEDA Standard格式适配。
- 加入更严格的自动布局和布线。
- 加入KiCad CLI、Freerouting或其他EDA后端。
- 加入更完整的ERC/DRC/DFM检查。

单天版先证明产品方向，完整MVP再证明工程可行性。
