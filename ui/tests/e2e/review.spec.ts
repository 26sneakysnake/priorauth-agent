import { type Page, expect, test } from "@playwright/test";

async function signIn(page: Page) {
  await page.goto("/login?next=/queue");
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
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("A reviewer approved this case: it is closed.")).toBeVisible();
  await expect(page.getByText("Engine decision:")).toContainText("Reviewer decision: APPROVE");
  await expect(page.getByText("Engine decision:")).toContainText("PEND");
  await expect(page.getByTestId("review-entry")).toContainText("approve");
  await page.getByRole("link", { name: "Back to the queue" }).click();
  await expect(page.getByTestId("queue-row").first()).toBeVisible();
  await expect(page.getByRole("link", { name: caseName, exact: true })).toHaveCount(0);
});

test("a reviewer follows a citation to its page and flags an agent error", async ({ page }) => {
  await signIn(page);
  await page.getByTestId("queue-row").first().getByRole("link").click();
  await expect(page.getByText("Engine decision:")).toContainText("PEND");

  // A citation link scrolls to its highlight, which then stands out; the highlight is the quoted text (compared
  // after the verifier's normalization: Unicode form, quotes, dashes, case, whitespace, edge punctuation).
  const citation = page.getByRole("article").getByRole("link", { name: /^page \d+$/ }).first();
  const quote = (await citation.locator("xpath=following-sibling::q[1]").textContent()) ?? "";
  await citation.click();
  const active = page.locator("mark.bg-yellow-300");
  await expect(active.first()).toBeVisible();
  const highlighted = (await active.allTextContents()).join("");
  const squash = (s: string) =>
    s
      .normalize("NFKC")
      .replace(/[\u2018\u2019]/g, "'")
      .replace(/[\u201c\u201d]/g, '"')
      .replace(/[\u2013\u2014\u2212]/g, "-")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .replace(/^(\.\.\.|[\s"'.,;:\u2026])+|(\.\.\.|[\s"'.,;:\u2026])+$/g, "");
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

test("a visitor signs in with the displayed demo account and runs the agent on the trap examples", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("demo-banner")).toContainText("Demonstration on synthetic data");
  await page.goto("/login");
  await expect(page.getByTestId("demo-account")).toContainText("demo-reviewer");
  await page.getByRole("button", { name: "Use the demo account" }).click();
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: /Try the agent/ })).toBeVisible();
  await expect(page.locator("[data-testid^=example-]")).toHaveCount(10);

  const injection = page.getByTestId("example-injection");
  await injection.getByRole("button", { name: "Run the agent" }).click();
  await expect(injection.getByTestId("decision-outcome")).toHaveText("PEND");
  await expect(injection.getByTestId("pend-reasons")).toContainText("INJECTION_DETECTED");

  const clean = page.getByTestId("example-clean-approve-pt");
  await clean.getByRole("button", { name: "Run the agent" }).click();
  await expect(clean.getByTestId("decision-outcome")).toHaveText("APPROVE");
  await expect(clean.locator("mark").first()).toBeVisible(); // the cited evidence is highlighted

  const dose = page.getByTestId("example-dose-x4");
  await dose.getByRole("button", { name: "Run the agent" }).click();
  await expect(dose.getByTestId("decision-outcome")).toHaveText("PEND");
  await expect(dose.getByTestId("pend-reasons")).toContainText("BIOLOGIC_RA.dose");
});
