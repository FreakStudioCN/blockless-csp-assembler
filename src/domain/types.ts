export type InterfaceKind =
  | "mcu"
  | "sensor"
  | "wireless"
  | "power"
  | "connector"
  | "storage"
  | "display";

export type ElectricalInterface =
  | "power"
  | "i2c"
  | "uart"
  | "spi"
  | "gpio"
  | "wifi"
  | "ble"
  | "lora"
  | "gnss";

export type PinType =
  | "power_in"
  | "power_out"
  | "gnd"
  | "i2c_sda"
  | "i2c_scl"
  | "uart_tx"
  | "uart_rx"
  | "gpio"
  | "spi_mosi"
  | "spi_miso"
  | "spi_sck"
  | "spi_cs";

export interface ModulePin {
  name: string;
  type: PinType;
  net?: string;
}

export interface ModuleAsset {
  mode: "cached" | "parametric" | "missing" | "stl";
  source?: string;
  sourceUrl?: string;
  localUrl?: string;
  localStepUrl?: string;
  imageUrl?: string;
  stepUrl?: string;
  bodyColor: string;
  label: string;
}

export interface HardwareModule {
  id: string;
  name: string;
  category: InterfaceKind;
  description: string;
  reasonTags: string[];
  sizeMm: { w: number; h: number };
  interfaces: ElectricalInterface[];
  voltage: string[];
  pins: ModulePin[];
  connector: string;
  source: string;
  purchaseUrl: string;
  unitPriceUsd: number;
  model3d: ModuleAsset;
  reviewStatus: "verified" | "needs_review" | "placeholder";
  reviewNotes: string;
}

export interface DemoCase {
  id: string;
  name: string;
  prompt: string;
  intentTags: string[];
  preferredModules: string[];
  boardSizeMm: { w: number; h: number };
}

export interface SelectedModule {
  module: HardwareModule;
  reason: string;
}

export interface NetEndpoint {
  moduleId: string;
  pin: string;
}

export interface NetConnection {
  net: string;
  kind: "power" | "gnd" | "i2c" | "uart" | "gpio" | "spi";
  from: NetEndpoint;
  to: NetEndpoint[];
  widthMm: number;
  layer: "top" | "bottom";
}

export interface Placement {
  moduleId: string;
  x: number;
  y: number;
  rotation: 0 | 90 | 180 | 270;
  role: string;
}

export interface TrackSegment {
  net: string;
  kind: NetConnection["kind"];
  layer: "top" | "bottom";
  widthMm: number;
  points: Array<{ x: number; y: number }>;
}

export interface Keepout {
  label: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface BoardDesign {
  id: string;
  title: string;
  prompt: string;
  boardSizeMm: { w: number; h: number };
  selectedModules: SelectedModule[];
  nets: NetConnection[];
  placements: Placement[];
  tracks: TrackSegment[];
  keepouts: Keepout[];
  bom: BomLine[];
  report: CheckReport;
}

export interface BomLine {
  item: number;
  moduleId: string;
  name: string;
  qty: number;
  connector: string;
  source: string;
  purchaseUrl: string;
  unitPriceUsd: number;
}

export interface CheckItem {
  level: "pass" | "warn" | "fail";
  code: string;
  message: string;
}

export interface CheckReport {
  erc: CheckItem[];
  drc: CheckItem[];
  dfm: CheckItem[];
  summary: {
    pass: number;
    warn: number;
    fail: number;
  };
}
