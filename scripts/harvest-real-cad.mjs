import { mkdir, writeFile } from "node:fs/promises";
import { createWriteStream } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { pipeline } from "node:stream/promises";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const outputDir = join(root, "public", "assets", "real-cad");

const targets = [
  {
    id: "esp32-feather",
    api: "https://api.github.com/repos/adafruit/Adafruit_CAD_Parts/contents/3405%20ESP32%20Feather%20HUZZAH",
    preferred: {
      stl: "3405 Adafruit HUZZAH32 ESP32 Feather.stl",
      step: "3405 Adafruit HUZZAH32 ESP32 Feather.step",
      image: "3405-Adafruit-HUZZAH32-Feather.gif",
    },
  },
  {
    id: "radio-featherwing",
    api: "https://api.github.com/repos/adafruit/Adafruit_CAD_Parts/contents/3229%20Radio%20FeatherWing",
    preferred: {
      stl: "3229 Radio FeatherWing.stl",
      step: "3229 Radio FeatherWing.step",
      image: "3229 Radio FeatherWing.jpg",
    },
  },
  {
    id: "bme280",
    api: "https://api.github.com/repos/adafruit/Adafruit_CAD_Parts/contents/2652%20Adafruit%20BME280",
    preferred: {
      stl: "Adafruit BME280.stl",
      step: "Adafruit BME280.step",
      image: "Adafruit-BME280.jpg",
    },
  },
  {
    id: "microlipo",
    api: "https://api.github.com/repos/adafruit/Adafruit_CAD_Parts/contents/1304%20MicroLipo%20Charger",
    preferred: {
      stl: "1304 MicroLipo Charger V2.stl",
      step: "1304 MicroLipo Charger V2.step",
      image: "1304 MicroLipo Charger V2.jpg",
    },
  },
  {
    id: "oled-128x32",
    api: "https://api.github.com/repos/adafruit/Adafruit_CAD_Parts/contents/931%20OLED%20128x32%20I2C",
    preferred: {
      step: "931 OLED 128x32 I2C.step",
    },
  },
];

function extFor(kind, fileName) {
  if (kind === "image") return fileName.toLowerCase().endsWith(".gif") ? "gif" : "jpg";
  return kind;
}

async function fetchJson(url) {
  return withRetry(async () => {
    const response = await fetch(url, {
      headers: {
        "User-Agent": "ai-pcb-module-backplane-mvp",
        Accept: "application/vnd.github+json",
      },
    });
    if (!response.ok) throw new Error(`GET ${url} failed: ${response.status} ${response.statusText}`);
    return response.json();
  });
}

async function download(url, outPath) {
  await withRetry(async () => {
    const response = await fetch(url, { headers: { "User-Agent": "ai-pcb-module-backplane-mvp" } });
    if (!response.ok || !response.body) throw new Error(`Download failed ${url}: ${response.status}`);
    await pipeline(response.body, createWriteStream(outPath));
  });
}

async function withRetry(fn, attempts = 4) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      if (attempt === attempts) break;
      await new Promise((resolve) => setTimeout(resolve, 900 * attempt));
    }
  }
  throw lastError;
}

await mkdir(outputDir, { recursive: true });

const manifest = {
  generatedAt: new Date().toISOString(),
  source: "https://github.com/adafruit/Adafruit_CAD_Parts",
  note: "Real public CAD assets crawled for the MVP demo. STL is rendered directly; STEP is kept for export/review.",
  modules: [],
};

for (const target of targets) {
  const files = await fetchJson(target.api);
  const moduleResult = {
    id: target.id,
    api: target.api,
    downloaded: {},
  };

  for (const [kind, fileName] of Object.entries(target.preferred)) {
    const found = files.find((item) => item.name === fileName);
    if (!found?.download_url) {
      moduleResult.downloaded[kind] = { status: "missing", fileName };
      continue;
    }

    const ext = extFor(kind, fileName);
    const localFile = `${target.id}.${ext}`;
    const outPath = join(outputDir, localFile);
    await download(found.download_url, outPath);
    moduleResult.downloaded[kind] = {
      status: "downloaded",
      fileName,
      localUrl: `/assets/real-cad/${localFile}`,
      sourceUrl: found.html_url,
      rawUrl: found.download_url,
      size: found.size,
    };
  }

  manifest.modules.push(moduleResult);
  console.log(`harvested ${target.id}`);
}

await writeFile(join(outputDir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
console.log(`wrote ${join(outputDir, "manifest.json")}`);
