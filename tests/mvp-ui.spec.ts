import { expect, test, type Page } from "@playwright/test";

test.setTimeout(180_000);

async function sampledCanvasHasPaint(page: Page) {
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

test("workspace renders planning, generation progress, board output, CNC export, and nonblank 3D canvas", async ({
  page,
}, testInfo) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Modular PCB design workstation" })).toBeVisible();
  await expect(page.locator(".workspace-rail .rail-card").first()).toContainText("Long-range environmental node");

  await page.getByRole("button", { name: "Prompt" }).click();
  await expect(page.locator("textarea")).toHaveValue(
    "Build a battery-powered long-range environmental node with ESP32, LoRa, a temperature/humidity sensor, LiPo charging, and I2C expansion.",
  );

  await page.getByRole("button", { name: "Generate board" }).click();
  const progress = page.locator(".generation-progress-overlay");
  await expect(progress).toBeVisible();
  await expect(progress).toContainText("Generating PCB project");
  await expect(progress).toBeHidden({ timeout: 12_000 });

  await expect(page.getByRole("heading", { name: "Generate" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "KiCad output / interactive sketch" })).toBeVisible();
  await expect(page.locator(".kicad-route-view")).toBeVisible();
  await expect(page.getByText("KRT autorouted .kicad_pcb")).toBeVisible();

  await page.getByRole("button", { name: "Bottom B.Cu" }).click();
  await expect(page.locator(".kicad-route-view")).toBeVisible();

  await page.getByRole("button", { name: "Interactive sketch" }).click();
  await expect(page.locator(".visual-panel .board-svg")).toBeVisible();
  await expect(page.getByRole("button", { name: "Regenerate PCB routing" })).toBeVisible();

  await page.getByRole("button", { name: "3D drag" }).click();
  await expect(page.locator(".three-host canvas")).toBeVisible();
  await page.waitForTimeout(800);
  expect(await sampledCanvasHasPaint(page)).toBe(true);

  await page.getByRole("button", { name: "Plan" }).click();
  await expect(page.getByRole("heading", { name: "AI module plan" })).toBeVisible();
  await expect(page.getByText("Adafruit HUZZAH32 ESP32 Feather")).toBeVisible();

  await page.getByRole("button", { name: "Review" }).click();
  await expect(page.getByRole("heading", { name: "Module harvest / review" })).toBeVisible();
  await expect(page.getByText("Average data confidence")).toBeVisible();

  await page.getByRole("button", { name: "Generate" }).click();
  await expect(page.getByRole("heading", { name: "KiCad output / interactive sketch" })).toBeVisible();
  await expect(page.locator(".mini-board .board-svg")).toBeVisible();
  await page.locator(".mini-board .draggable-module").first().dragTo(page.locator(".mini-board .board-svg"), {
    targetPosition: { x: 260, y: 220 },
  });
  await expect(page.locator(".mini-board .board-svg .draggable-module").first()).toBeVisible();

  const exportButton = page.getByRole("button", { name: "Export" });
  await exportButton.scrollIntoViewIfNeeded();
  await exportButton.click({ force: true });
  await expect(page.getByRole("heading", { name: "3D board preview" })).toBeVisible();
  await expect(page.locator(".three-host canvas")).toBeVisible();
  await page.waitForTimeout(500);
  expect(await sampledCanvasHasPaint(page)).toBe(true);

  await expect(page.getByRole("heading", { name: "PCB to CNC toolpaths" })).toBeVisible();
  await expect(page.locator(".cnc-toolpath-panel")).toContainText("long-range-env-node_front.ngc");
  if (testInfo.project.name === "desktop-chromium" && process.env.RUN_KICAD_INTEGRATION === "1") {
    await page.getByRole("button", { name: "Generate real CNC toolpaths" }).click();
    await expect(page.locator(".cnc-toolpath-panel .route-status")).toContainText("Calling KiCad CLI + pcb2gcode");
    await expect(page.locator(".cnc-toolpath-panel .route-status")).toContainText("Real CNC toolpath files ready", {
      timeout: 150_000,
    });
  }
  await expect(page.locator(".cnc-real-preview")).toBeVisible();

  const screenshot = await page.screenshot({ fullPage: false, timeout: 10_000 });
  await testInfo.attach("product-export-page", {
    body: screenshot,
    contentType: "image/png",
  });
});
