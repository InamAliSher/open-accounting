import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { BACKEND_URL, companyHeaders, ensureCompanyById } from "./helpers";

const COMPANY_ID = "creditnoteapplicationsui";
const ISSUE_DATE = "2026-08-01";

async function accountId(request: APIRequestContext, code: string): Promise<number> {
  const response = await request.get(`${BACKEND_URL}/api/v1/accounts`, {
    headers: companyHeaders(COMPANY_ID),
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  const accounts = (await response.json()) as Array<{ id: number; code: string }>;
  const account = accounts.find((item) => item.code === code);
  expect(account, `Expected account ${code}`).toBeTruthy();
  return account!.id;
}

async function createInvoice(
  request: APIRequestContext,
  invoiceNumber: string,
  accountId: number,
  direction: "AR" | "AP",
  contactName: string,
): Promise<number> {
  const response = await request.post(`${BACKEND_URL}/api/v1/invoices`, {
    headers: companyHeaders(COMPANY_ID),
    data: {
      direction,
      contact_name: contactName,
      invoice_number: invoiceNumber,
      issue_date: ISSUE_DATE,
      currency: "AUD",
      subtotal: "110.00",
      gst_amount: "0.00",
      total: "110.00",
      gst_inclusive: false,
      source: "manual",
      lines: [
        {
          description: "Credit-note application test",
          account_id: accountId,
          quantity: "1",
          unit_price: "110.00",
          line_subtotal: "110.00",
          line_gst: "0.00",
          line_total: "110.00",
          tax_code: "none",
          gst_rate: "0",
        },
      ],
    },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return ((await response.json()) as { id: number }).id;
}

async function postInvoice(request: APIRequestContext, invoiceId: number): Promise<void> {
  const response = await request.post(`${BACKEND_URL}/api/v1/invoices/${invoiceId}/post`, {
    headers: companyHeaders(COMPANY_ID),
  });
  expect(response.ok(), await response.text()).toBeTruthy();
}

async function createAuthorisedCreditNote(
  request: APIRequestContext,
  sourceInvoiceId: number,
  creditNoteNumber: string,
): Promise<number> {
  const sourceLinesResponse = await request.get(
    `${BACKEND_URL}/api/v1/credit-notes/source-invoices/${sourceInvoiceId}`,
    { headers: companyHeaders(COMPANY_ID) },
  );
  expect(sourceLinesResponse.ok(), await sourceLinesResponse.text()).toBeTruthy();
  const sourceLines = (await sourceLinesResponse.json()) as {
    lines: Array<{ source_invoice_line_id: number }>;
  };
  const response = await request.post(`${BACKEND_URL}/api/v1/credit-notes`, {
    headers: companyHeaders(COMPANY_ID),
    data: {
      source_invoice_id: sourceInvoiceId,
      credit_note_number: creditNoteNumber,
      issue_date: ISSUE_DATE,
      lines: [
        {
          source_invoice_line_id: sourceLines.lines[0].source_invoice_line_id,
          quantity: "1.0000",
        },
      ],
    },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  const creditNote = (await response.json()) as { id: number };
  const post = await request.post(`${BACKEND_URL}/api/v1/credit-notes/${creditNote.id}/post`, {
    headers: companyHeaders(COMPANY_ID),
  });
  expect(post.ok(), await post.text()).toBeTruthy();
  return creditNote.id;
}

async function openInvoice(page: Page, invoiceNumber: string): Promise<void> {
  await page.getByRole("row").filter({ hasText: invoiceNumber }).click();
  await expect(page.getByRole("heading", { name: new RegExp(invoiceNumber) })).toBeVisible();
}

async function closeDrawer(page: Page): Promise<void> {
  await page
    .getByRole("heading", { name: /FICTIONAL-APP-(AR|AP)-001/ })
    .locator("xpath=ancestor::div[contains(@class,'flex items-center justify-between')]/button")
    .click();
}

async function openAuthorisedCreditNote(page: Page, creditNoteNumber: string): Promise<void> {
  const sourceSection = page.getByRole("region", { name: "Credit notes for source invoice" });
  const creditNoteRow = sourceSection.getByRole("row").filter({ hasText: creditNoteNumber });
  await expect(creditNoteRow).toBeVisible();
  await creditNoteRow.getByRole("button", { name: "View" }).click();
  await expect(page.getByRole("heading", { name: "View authorised credit note" })).toBeVisible();
}

async function selectCompany(page: Page): Promise<void> {
  const switcher = page.getByLabel("Select company");
  await expect(switcher.locator(`option[value="${COMPANY_ID}"]`)).toHaveCount(1);
  await switcher.selectOption(COMPANY_ID);
  await expect(switcher).toHaveValue(COMPANY_ID);
}

test("authorised credit notes can partially apply and reverse for AR and AP", async ({ page, request }) => {
  await ensureCompanyById(request, COMPANY_ID, "Credit Note Applications UI Pty Ltd");
  const headers = companyHeaders(COMPANY_ID);
  const arAccountId = await accountId(request, "4000");
  const apAccountId = await accountId(request, "6100");
  const arInvoiceId = await createInvoice(
    request,
    "FICTIONAL-APP-AR-001",
    arAccountId,
    "AR",
    "Application Customer",
  );
  const apInvoiceId = await createInvoice(
    request,
    "FICTIONAL-APP-AP-001",
    apAccountId,
    "AP",
    "Application Supplier",
  );
  await postInvoice(request, arInvoiceId);
  await postInvoice(request, apInvoiceId);
  const arCreditNoteId = await createAuthorisedCreditNote(
    request,
    arInvoiceId,
    "FICTIONAL-APP-AR-CREDIT",
  );
  const apCreditNoteId = await createAuthorisedCreditNote(
    request,
    apInvoiceId,
    "FICTIONAL-APP-AP-CREDIT",
  );

  await page.goto("/invoices");
  await selectCompany(page);
  await openInvoice(page, "FICTIONAL-APP-AR-001");
  const arDrawer = page
    .getByRole("heading", { name: /FICTIONAL-APP-AR-001/ })
    .locator("xpath=ancestor::div[contains(@class,'w-[640px]')]");
  await expect(arDrawer.getByText("Cash paid", { exact: true })).toBeVisible();
  await expect(arDrawer.getByText("Credit applied", { exact: true })).toBeVisible();
  await expect(arDrawer.getByText("Outstanding", { exact: true })).toBeVisible();
  await expect(arDrawer.getByText("Cash paid").locator("..")).toContainText("$0.00");
  await expect(arDrawer.getByText("Credit applied").locator("..")).toContainText("$0.00");
  await expect(arDrawer.getByText("Outstanding").locator("..")).toContainText("$110.00");
  await openAuthorisedCreditNote(page, "FICTIONAL-APP-AR-CREDIT");

  const arCreditNote = page.getByRole("region", { name: "Credit note applications" });
  await arCreditNote.getByLabel("Invoice to apply").selectOption({ label: "FICTIONAL-APP-AR-001 · $110.00" });
  await arCreditNote.getByLabel("Application amount").fill("40.00");
  await arCreditNote.getByLabel("Application date").fill("2026-08-15");
  await arCreditNote.getByRole("button", { name: "Apply", exact: true }).click();
  const arConfirmation = page.getByRole("heading", { name: "Apply this credit to the invoice?" }).locator("..");
  await expect(arConfirmation).toContainText("decrease the credit note remaining amount");
  await expect(arConfirmation).toContainText("invoice outstanding amount by the same amount");
  await arConfirmation.getByRole("button", { name: "Apply credit" }).click();
  await expect(arCreditNote.getByText("$40.00", { exact: true })).toBeVisible();
  await expect(arCreditNote.getByText("active", { exact: true })).toBeVisible();
  await expect(arCreditNote.getByText("Invoice number")).toBeVisible();
  await expect(arCreditNote.getByRole("columnheader", { name: "Application date" })).toBeVisible();
  await expect(arCreditNote.getByRole("button", { name: "Reverse" })).toBeVisible();
  await expect(arCreditNote.getByRole("button", { name: "Refund" })).toHaveCount(0);
  await expect(arCreditNote.getByRole("button", { name: "Payment" })).toHaveCount(0);
  await expect(arCreditNote.getByRole("button", { name: "Void" })).toHaveCount(0);

  await arCreditNote.getByRole("button", { name: "Reverse" }).click();
  const reverseDialog = page.getByRole("heading", { name: "Reverse this credit application?" }).locator("..");
  await expect(reverseDialog).toContainText("Reverse this credit application?");
  await reverseDialog.getByLabel("Reversal date").fill("2026-08-16");
  await reverseDialog.getByRole("button", { name: "Reverse application" }).click();
  await expect(arCreditNote.getByText("reversed", { exact: true })).toBeVisible();
  await expect(arCreditNote.getByText("16/08/2026", { exact: true })).toBeVisible();
  await expect(arCreditNote.getByText("active", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Close" }).click();

  await page.goto("/invoices");
  await selectCompany(page);
  await openInvoice(page, "FICTIONAL-APP-AP-001");
  const apDrawer = page
    .getByRole("heading", { name: /FICTIONAL-APP-AP-001/ })
    .locator("xpath=ancestor::div[contains(@class,'w-[640px]')]");
  await expect(apDrawer.getByText("Cash paid").locator("..")).toContainText("$0.00");
  await expect(apDrawer.getByText("Credit applied").locator("..")).toContainText("$0.00");
  await expect(apDrawer.getByText("Outstanding").locator("..")).toContainText("$110.00");
  await openAuthorisedCreditNote(page, "FICTIONAL-APP-AP-CREDIT");
  const apCreditNote = page.getByRole("region", { name: "Credit note applications" });
  await apCreditNote.getByLabel("Invoice to apply").selectOption({ label: "FICTIONAL-APP-AP-001 · $110.00" });
  await apCreditNote.getByLabel("Application amount").fill("25.00");
  await apCreditNote.getByLabel("Application date").fill("2026-08-15");
  await apCreditNote.getByRole("button", { name: "Apply", exact: true }).click();
  const apConfirmation = page.getByRole("heading", { name: "Apply this credit to the invoice?" }).locator("..");
  await apConfirmation.getByRole("button", { name: "Apply credit" }).click();
  await expect(apCreditNote.getByText("$25.00", { exact: true })).toBeVisible();
  await expect(apCreditNote.getByText("active", { exact: true })).toBeVisible();
  await page
    .getByRole("heading", { name: "View authorised credit note" })
    .locator("xpath=ancestor::div[contains(@class,'fixed inset-0 z-50')]")
    .getByRole("button", { name: "×" })
    .click();
  await closeDrawer(page);
  await openInvoice(page, "FICTIONAL-APP-AP-001");
  const refreshedApDrawer = page
    .getByRole("heading", { name: /FICTIONAL-APP-AP-001/ })
    .locator("xpath=ancestor::div[contains(@class,'w-[640px]')]");
  await expect(refreshedApDrawer.getByText("Cash paid").locator("..")).toContainText("$0.00");
  await expect(refreshedApDrawer.getByText("Credit applied").locator("..")).toContainText("$25.00");
  await expect(refreshedApDrawer.getByText("Outstanding").locator("..")).toContainText("$85.00");
  await expect(apCreditNote.getByRole("button", { name: "Refund" })).toHaveCount(0);
  await expect(apCreditNote.getByRole("button", { name: "Payment" })).toHaveCount(0);
  await expect(apCreditNote.getByRole("button", { name: "Void" })).toHaveCount(0);
  await closeDrawer(page);

  const arInvoice = await request.get(`${BACKEND_URL}/api/v1/invoices/${arInvoiceId}`, {
    headers,
  });
  expect(arInvoice.ok()).toBeTruthy();
  expect(await arInvoice.json()).toMatchObject({
    credit_applied_amount: "0.00",
    outstanding_amount: "110.00",
  });
  const apInvoice = await request.get(`${BACKEND_URL}/api/v1/invoices/${apInvoiceId}`, {
    headers,
  });
  expect(apInvoice.ok()).toBeTruthy();
  expect(await apInvoice.json()).toMatchObject({
    credit_applied_amount: "25.00",
    outstanding_amount: "85.00",
  });
  const arCreditNoteResponse = await request.get(`${BACKEND_URL}/api/v1/credit-notes/${arCreditNoteId}`, {
    headers,
  });
  expect(arCreditNoteResponse.ok()).toBeTruthy();
  const arCreditNoteApi = (await arCreditNoteResponse.json()) as {
    applied_amount: string;
    remaining_amount: string;
    applications: Array<{ status: string; reversal_date: string | null }>;
  };
  expect(arCreditNoteApi).toMatchObject({
    applied_amount: "0.00",
    remaining_amount: "110.00",
  });
  expect(arCreditNoteApi.applications).toHaveLength(1);
  expect(arCreditNoteApi.applications[0]).toMatchObject({ status: "reversed", reversal_date: "2026-08-16" });
  const apCreditNoteResponse = await request.get(`${BACKEND_URL}/api/v1/credit-notes/${apCreditNoteId}`, {
    headers,
  });
  expect(apCreditNoteResponse.ok()).toBeTruthy();
  const apCreditNoteApi = (await apCreditNoteResponse.json()) as {
    applied_amount: string;
    remaining_amount: string;
  };
  expect(apCreditNoteApi).toMatchObject({
    applied_amount: "25.00",
    remaining_amount: "85.00",
  });
});
