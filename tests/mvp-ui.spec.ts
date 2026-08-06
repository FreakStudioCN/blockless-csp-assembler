import { expect, test } from "@playwright/test";

test.setTimeout(90_000);

async function sampledCanvasHasPaint(page: import("@playwright/test").Page) {
  return page.locator(".three-host canvas").evaluate((canvasElement) => {
    const canvas = canvasElement as HTMLCanvasElement;
    const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    if (!gl) return false;

    let painted = 0;
    const pixel = new Uint8Array(4);
    for (let x = 0; x < canvas.width; x += Math.max(1, Math.floor(canvas.width / 16))) {
      for (let y = 0; y < canvas.height; y += Math.max(1, Math.floor(canvas.height / 16))) {
        gl.readPixels(x, y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
        const [r, g, b, a] = pixel;
        const isNearWhite = r > 238 && g > 238 && b > 238;
        const isNearTransparent = a < 5;
        if (!isNearTransparent && !isNearWhite) painted += 1;
      }
    }

    return painted > 6;
  });
}

test("workspace renders module planning, board generation, export, and nonblank 3D canvas", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "模块化PCB设计工作台" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "KiCad真实输出 / 可交互草图" })).toBeVisible();
  await expect(page.locator(".kicad-route-view")).toBeVisible();
  await expect(page.getByText("KRT autorouted .kicad_pcb")).toBeVisible();
  await page.getByRole("button", { name: "底层 B.Cu" }).click();
  await expect(page.locator(".kicad-route-view")).toBeVisible();
  await page.getByRole("button", { name: "可拖拽草图" }).click();
  await expect(page.locator(".visual-panel .board-svg")).toBeVisible();
  await expect(page.getByRole("button", { name: "重新生成PCB布线" })).toBeVisible();
  await page.getByRole("button", { name: "3D拖拽" }).click();
  await expect(page.locator(".three-host canvas")).toBeVisible();
  await page.waitForTimeout(800);
  expect(await sampledCanvasHasPaint(page)).toBe(true);

  await page.getByRole("button", { name: "输入" }).click();
  await page.getByRole("button", { name: "生成模块底板" }).click();
  await expect(page.getByRole("heading", { name: "KiCad真实输出 / 可交互草图" })).toBeVisible();
  await expect(page.locator(".kicad-route-view")).toBeVisible();

  await page.getByRole("button", { name: "选型" }).click();
  await expect(page.getByRole("heading", { name: "AI模块方案" })).toBeVisible();
  await expect(page.getByText("Adafruit HUZZAH32 ESP32 Feather")).toBeVisible();

  await page.getByRole("button", { name: "检查" }).click();
  await expect(page.getByRole("heading", { name: "模块资料抓取 / 审阅" })).toBeVisible();
  await expect(page.getByText("平均资料置信度")).toBeVisible();

  await page.getByRole("button", { name: "布局" }).click();
  await expect(page.getByRole("heading", { name: "KiCad真实输出 / 可交互草图" })).toBeVisible();
  await expect(page.locator(".mini-board .board-svg")).toBeVisible();
  await page.locator(".mini-board .draggable-module").first().dragTo(page.locator(".mini-board .board-svg"), {
    targetPosition: { x: 260, y: 220 },
  });
  await expect(page.locator(".mini-board .board-svg .draggable-module").first()).toBeVisible();

  const exportButton = page.getByRole("button", { name: "导出" });
  await exportButton.scrollIntoViewIfNeeded();
  await exportButton.click({ force: true });
  await expect(page.getByRole("heading", { name: "3D底板预览" })).toBeVisible();
  await expect(page.locator(".three-host canvas")).toBeVisible();
  await page.waitForTimeout(500);

  expect(await sampledCanvasHasPaint(page)).toBe(true);

  const screenshot = await page.screenshot({ fullPage: false, timeout: 10_000 });
  await testInfo.attach("product-export-page", {
    body: screenshot,
    contentType: "image/png",
  });
});
