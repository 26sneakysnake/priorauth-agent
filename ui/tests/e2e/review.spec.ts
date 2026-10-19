import { type Page, expect, test } from "@playwright/test";

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("demo-reviewer");
  await page.getByLabel("Password").fill(process.env.E2E_DEMO_PASSWORD ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Pended cases" })).toBeVisible();
  await expect(page.getByTestId("queue-row").first()).toBeVisible();
}

test("a reviewer approves a pended case and it leaves the queue", async ({ page }) => {
  await signIn(page);
  const link = page.getByTestId("queue-row").first().getByRole("link");
  const caseName = (await link.textContent()) ?? "";
  await link.click();
  await expect(page.getByText("Engine decision:")).toContainText("PEND");
  await expect(page.getByText("scripted-oracle-not-a-model+protected")).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("A reviewer approved this case.")).toBeVisible();
  await expect(page.getByTestId("review-entry")).toContainText("approve");
  await page.getByRole("link", { name: "Back to the queue" }).click();
  await expect(page.getByTestId("queue-row").first()).toBeVisible();
  await expect(page.getByRole("link", { name: caseName, exact: true })).toHaveCount(0);
});

test("a reviewer follows a citation to its page and flags an agent error", async ({ page }) => {
  await signIn(page);
  await page.getByTestId("queue-row").first().getByRole("link").click();
  await expect(page.getByText("Engine decision:")).toContainText("PEND");

  // A citation link scrolls to its highlight, which then stands out; the highlight is the quoted text.
  const card = page.getByRole("article").first();
  const citation = card.getByRole("link", { name: /^page \d+$/ }).first();
  const quote = (await citation.locator("xpath=following-sibling::q[1]").textContent()) ?? "";
  await citation.click();
  const active = page.locator("mark.bg-yellow-300");
  await expect(active.first()).toBeVisible();
  const highlighted = (await active.allTextContents()).join("");
  const squash = (s: string) => s.toLowerCase().replace(/\s+/g, " ").replace(/^[\s"'.,;:]+|[\s"'.,;:]+$/g, "");
  expect(squash(highlighted)).toBe(squash(quote));

  await page.getByLabel("Flag an agent error").check();
  const criterion = page.getByLabel("Criterion");
  const option = criterion.locator("option").nth(1);
  const criterionId = (await option.getAttribute("value")) ?? "";
  await criterion.selectOption(criterionId);
  await page.getByLabel("Correct status").selectOption("NOT_MET");
  await page.getByLabel("Comment").fill("The cited line is about a different visit.");
  await page.getByRole("button", { name: "Flag an agent error" }).click();
  await expect(page.getByRole("status")).toHaveText("Flag an agent error: recorded.");
  await expect(page.getByTestId("review-entry")).toContainText(`flag_error · ${criterionId} → NOT_MET`);

  await page.reload(); // stored, not only displayed
  await expect(page.getByTestId("review-entry")).toContainText("The cited line is about a different visit.");
  await page.getByRole("link", { name: "Back to the queue" }).click();
  await expect(page.getByText("error flagged").first()).toBeVisible();
});
