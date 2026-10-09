import { expect, test, type Page } from "@playwright/test";
import { BACKEND_URL, companyHeaders, ensureCompanyById } from "./helpers";

const COMPANY_ID = "bankimportidentityui";

async function selectCompany(page: Page): Promise<void> {
  const selector = page.getByLabel("Select company");
  await expect(selector.locator(`option[value="${COMPANY_ID}"]`)).toHaveCount(1);
  await selector.selectOption(COMPANY_ID);
  await expect(selector).toHaveValue(COMPANY_ID);
}

async function startImport(page: Page, filename: string, csv: string): Promise<void> {
  await page.getByRole("button", { name: "Import statement…" }).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: filename,
    mimeType: "text/csv",
    buffer: Buffer.from(csv),
  });
  await page.getByRole("button", { name: "Preview" }).click();
  await expect(page.getByRole("columnheader", { name: "Amount" })).toBeVisible();
}

async function finishImport(page: Page, expectedCreated: number): Promise<void> {
  await page.getByRole("button", { name: new RegExp(`Import ${expectedCreated} row`) }).click();
  await expect(page.getByText(new RegExp(`Created ${expectedCreated} transactions`))).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();
}

test("bank import preserves occurrences and requires explicit no-ID statement review", async ({
  page,
  request,
}) => {
  await ensureCompanyById(request, COMPANY_ID, "Bank Import Identity UI Pty Ltd");
  await page.goto("/business-account");
  await selectCompany(page);

  const headers = companyHeaders(COMPANY_ID);
  const banksResponse = await request.get(`${BACKEND_URL}/api/v1/bank-accounts`, {
    headers,
  });
  expect(banksResponse.ok(), await banksResponse.text()).toBeTruthy();
  const banks = (await banksResponse.json()) as Array<{ id: number }>;
  const bankId = banks[0].id;

  const identifiedCsv =
    "Date,Description,Transaction ID,Credit\n" +
    "2026-08-10,Provider receipt A,ui-provider-001,45.00\n" +
    "2026-08-11,Provider receipt B,ui-provider-002,55.00\n";
  await startImport(page, "identified.csv", identifiedCsv);
  await expect(page.getByText("Provider receipt A")).toBeVisible();
  await finishImport(page, 2);

  await startImport(page, "identified.csv", identifiedCsv);
  await expect(page.getByText("2 duplicate(s)")).toBeVisible();
  await expect(page.getByRole("button", { name: "Import 0 row(s)" })).toBeDisabled();
  await page.getByRole("button", { name: "Cancel" }).click();

  const noIdCsv =
    "Date,Description,Credit\n" +
    "2026-08-12,Repeated occurrence,25.00\n" +
    "2026-08-12,Repeated occurrence,25.00\n";
  await startImport(page, "no-id.csv", noIdCsv);
  await expect(page.getByRole("row").filter({ hasText: "Repeated occurrence" })).toHaveCount(2);
  await finishImport(page, 2);

  await startImport(page, "no-id.csv", noIdCsv);
  await expect(page.getByText("This no-ID statement matches a prior import")).toBeVisible();
  await page.getByLabel("Same import, skip rows already recorded").check();
  await finishImport(page, 0);

  await startImport(page, "no-id.csv", noIdCsv);
  await page.getByLabel("Independent import, create these transactions again").check();
  await finishImport(page, 2);

  const transactionsResponse = await request.get(
    `${BACKEND_URL}/api/v1/bank-accounts/${bankId}/transactions`,
    { headers },
  );
  expect(transactionsResponse.ok(), await transactionsResponse.text()).toBeTruthy();
  const transactions = (await transactionsResponse.json()) as Array<{ memo: string | null }>;
  expect(transactions.filter((row) => row.memo === "Repeated occurrence")).toHaveLength(4);
  expect(transactions.filter((row) => row.memo?.startsWith("Provider receipt "))).toHaveLength(2);
});