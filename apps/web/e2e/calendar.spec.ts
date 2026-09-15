import { expect, type Page, test } from "@playwright/test";

const today = new Intl.DateTimeFormat("en-CA", {
  timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit",
}).format(new Date());

async function seedEvent(page:Page, suffix:string, overrides:Record<string, unknown> = {}) {
  const response = await page.request.post("/api/v1/events", { data:{
    title_zh:`E2E 验收事件 ${suffix}`,
    title_original:`E2E acceptance event ${suffix}`,
    institution:"E2E Test Institution",
    country_code:"US",
    category:"macro_release",
    event_type:"activity",
    status:"confirmed",
    importance:"high",
    date_precision:"date",
    local_date:today,
    original_timezone:"Asia/Shanghai",
    original_time_text:"仅日期",
    market_tags:["US"],
    reminder_enabled:false,
    idempotency_key:`calendar-e2e-${suffix}`,
    ...overrides,
  }});
  expect(response.ok()).toBeTruthy();
  return response.json();
}

// The UI renders `display_title`, which the API derives from `title_original` for
// Latin-script sources; `title_zh` is only a fallback. See
// apps/web/src/lib/event-title.ts and its unit test. Assert on what is displayed,
// while still seeding (and searching by) the Chinese title to cover both fields.
function displayTitle(suffix:string):string {
  return `E2E acceptance event ${suffix}`;
}

test("dashboard uses live API data and primary navigation", async ({ page }, testInfo) => {
  const suffix = `dashboard-${testInfo.project.name}-${Date.now()}`;
  await seedEvent(page, suffix);
  await page.goto("/");
  await expect(page.getByRole("heading", { name:"市场总览" })).toBeVisible();
  await expect(page.getByText(displayTitle(suffix)).first()).toBeVisible({ timeout:15_000 });
  await page.getByRole("link", { name:"今天", exact:true }).click();
  await expect(page).toHaveURL(/\/today$/);
  await expect(page.getByText(/API 实时数据/)).toBeVisible();
});

test("search and filters update the URL and the calendar result", async ({ page }, testInfo) => {
  const suffix = `searchable-${testInfo.project.name}-${Date.now()}`;
  await seedEvent(page, suffix);
  await page.goto("/week");
  await page.getByRole("button", { name:/搜索事件/ }).click();
  await page.getByLabel("搜索关键词").fill(`E2E 验收事件 ${suffix}`);
  await page.getByRole("dialog", { name:"搜索事件" }).getByRole("button", { name:"搜索", exact:true }).click();
  await expect(page).toHaveURL(/\/week\?q=E2E/);
  await expect(page.getByText(displayTitle(suffix)).first()).toBeVisible();

  await page.locator("select[name=market]").selectOption("JP");
  await page.getByRole("button", { name:"筛选", exact:true }).click();
  await expect(page).toHaveURL(/market=JP/);
  await expect(page.getByText(displayTitle(suffix))).toHaveCount(0);
});

test("manual event drawer validates required fields without creating junk data", async ({ page }) => {
  await page.goto("/today");
  await page.getByRole("button", { name:"人工新增事件" }).click();
  await expect(page.getByRole("dialog", { name:"人工新增事件" })).toBeVisible();
  await expect(page.getByLabel("事件日期")).toHaveValue(today);
  await page.getByRole("button", { name:"保存事件" }).click();
  await expect(page.getByLabel("中文标题")).toBeFocused();
  await page.getByRole("button", { name:"取消" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("settings save round-trips through the API and applies the timezone", async ({ page }) => {
  await page.goto("/settings");
  const timezone = page.getByLabel("默认时区");
  await expect(timezone).toBeVisible();
  await timezone.selectOption("Asia/Tokyo");
  await page.getByRole("button", { name:"保存设置" }).click();
  await expect(page.getByText("设置已保存并从 API 重新读取成功")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("默认时区")).toHaveValue("Asia/Tokyo");
  await expect(page.getByText("东京时间 · UTC+9")).toBeVisible();

  await page.getByLabel("默认时区").selectOption("Asia/Shanghai");
  await page.getByRole("button", { name:"保存设置" }).click();
  await expect(page.getByText("设置已保存并从 API 重新读取成功")).toBeVisible();
});

test("sync and changes expose observable results", async ({ page }, testInfo) => {
  const suffix = `changes-${testInfo.project.name}-${Date.now()}`;
  await seedEvent(page, suffix);
  await page.goto("/changes");
  await expect(page.getByText(displayTitle(suffix))).toBeVisible({ timeout:15_000 });
  await page.getByRole("button", { name:"立即同步" }).click();
  await expect(page.getByRole("status")).toContainText(/已提交 \d+ 个来源同步任务/);
});
