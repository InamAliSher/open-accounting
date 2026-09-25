import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useCompanyStore } from "../../store/company";
import { useCurrentCompany } from "../../lib/useCurrentCompany";
import type {
  Account,
  InvoiceCreate,
  InvoiceDirection,
  TaxCode,
} from "../../types/api";
import DateInput from "../DateInput";

// Strip thousands separators so "1,000" parses/submits as 1000 instead of
// truncating to 1 (parseFloat) or 422-ing the backend Decimal parse.
function stripMoney(value: string): string {
  return (value ?? "").replace(/,/g, "").trim();
}

function moneyString(value: number): string {
  return Number.isFinite(value) ? value.toFixed(2) : "0.00";
}

export interface InvoiceLineDraft {
  description: string;
  quantity: string;
  unit_price: string;
  account_id: number | "";
  tax_code: TaxCode;
  gst_rate: string;
  line_subtotal: string;
  line_gst: string;
  line_total: string;
}

export interface InvoiceFormValues {
  direction: InvoiceDirection;
  contact_name: string;
  contact_abn: string;
  invoice_number: string;
  issue_date: string;
  due_date: string;
  subtotal: string;
  gst_amount: string;
  total: string;
  gst_inclusive: boolean;
  notes: string;
  account_id: number | "";
  tax_code: TaxCode;
  lines: InvoiceLineDraft[];
}

function blankLine(defaultTaxCode: TaxCode = "standard"): InvoiceLineDraft {
  return {
    description: "",
    quantity: "1",
    unit_price: "",
    account_id: "",
    tax_code: defaultTaxCode,
    gst_rate: defaultTaxCode === "gst_free" || defaultTaxCode === "input_taxed" || defaultTaxCode === "none" ? "0" : "0.10",
    line_subtotal: "0.00",
    line_gst: "0.00",
    line_total: "0.00",
  };
}

function computeLine(line: InvoiceLineDraft, fallbackTaxCode: TaxCode): InvoiceLineDraft {
  const taxCode = line.tax_code || fallbackTaxCode;
  const qty = Number(stripMoney(line.quantity) || "0");
  const unit = Number(stripMoney(line.unit_price) || "0");
  const gstRateValue = ["gst_free", "input_taxed", "none"].includes(taxCode)
    ? 0
    : Number(stripMoney(line.gst_rate) || "0.10");
  const subtotal = qty * unit;
  const gst = subtotal * gstRateValue;
  const total = subtotal + gst;
  return {
    ...line,
    tax_code: taxCode,
    gst_rate: ["gst_free", "input_taxed", "none"].includes(taxCode) ? "0" : moneyString(gstRateValue),
    line_subtotal: moneyString(subtotal),
    line_gst: moneyString(gst),
    line_total: moneyString(total),
  };
}

function summariseLines(lines: InvoiceLineDraft[]): { subtotal: string; gst_amount: string; total: string } {
  const subtotal = lines.reduce((sum, line) => sum + Number(stripMoney(line.line_subtotal) || "0"), 0);
  const gst = lines.reduce((sum, line) => sum + Number(stripMoney(line.line_gst) || "0"), 0);
  const total = lines.reduce((sum, line) => sum + Number(stripMoney(line.line_total) || "0"), 0);
  return {
    subtotal: moneyString(subtotal),
    gst_amount: moneyString(gst),
    total: moneyString(total),
  };
}

function lineIsValid(line: InvoiceLineDraft): boolean {
  if (!line.description.trim()) return false;
  const qty = Number(stripMoney(line.quantity) || "0");
  const unit = Number(stripMoney(line.unit_price) || "0");
  if (!Number.isFinite(qty) || qty <= 0) return false;
  if (!Number.isFinite(unit) || unit < 0) return false;
  if (line.account_id === "" || Number(line.account_id) <= 0) return false;
  return !!line.tax_code;
}

export const EMPTY_FORM: InvoiceFormValues = {
  direction: "AP",
  contact_name: "",
  contact_abn: "",
  invoice_number: "",
  issue_date: "",
  due_date: "",
  subtotal: "0.00",
  gst_amount: "0.00",
  total: "0.00",
  gst_inclusive: true,
  notes: "",
  account_id: "",
  tax_code: "gst_free",
  lines: [blankLine("gst_free")],
};

export function toCreatePayload(
  v: InvoiceFormValues,
  opts: { source?: "manual" | "pdf" | "excel"; attachment_id?: string | null; gst_registered?: boolean } = {},
): InvoiceCreate {
  const baseTax = opts.gst_registered === false ? "none" : v.tax_code || "standard";
  const normalised = v.lines.map((line) => computeLine(line, baseTax));
  const totals = summariseLines(normalised);
  const lines = normalised.filter(lineIsValid).map((line) => ({
    description: line.description.trim(),
    account_id: Number(line.account_id),
    quantity: stripMoney(line.quantity) || "1",
    unit_price: moneyString(Number(stripMoney(line.unit_price) || "0")),
    gst_rate: ["gst_free", "input_taxed", "none"].includes(line.tax_code) ? "0" : moneyString(Number(stripMoney(line.gst_rate) || "0.10")),
    line_subtotal: line.line_subtotal,
    line_gst: line.line_gst,
    line_total: line.line_total,
    tax_code: line.tax_code,
  }));

  if (opts.gst_registered === false) {
    return {
      direction: v.direction,
      contact_name: v.contact_name.trim() || null,
      contact_abn: v.contact_abn.trim() || null,
      invoice_number: v.invoice_number.trim(),
      issue_date: v.issue_date,
      due_date: v.due_date || null,
      subtotal: totals.total,
      gst_amount: "0",
      total: totals.total,
      gst_inclusive: false,
      notes: v.notes.trim() || null,
      source: opts.source ?? "manual",
      attachment_id: opts.attachment_id ?? null,
      lines,
    };
  }

  return {
    direction: v.direction,
    contact_name: v.contact_name.trim() || null,
    contact_abn: v.contact_abn.trim() || null,
    invoice_number: v.invoice_number.trim(),
    issue_date: v.issue_date,
    due_date: v.due_date || null,
    subtotal: totals.subtotal,
    gst_amount: totals.gst_amount,
    total: totals.total,
    gst_inclusive: v.gst_inclusive,
    notes: v.notes.trim() || null,
    source: opts.source ?? "manual",
    attachment_id: opts.attachment_id ?? null,
    lines,
  };
}

interface Props {
  value: InvoiceFormValues;
  onChange: (v: InvoiceFormValues) => void;
  showDirection?: boolean;
}

export default function InvoiceForm({ value, onChange, showDirection = true }: Props) {
  const currentId = useCompanyStore((s) => s.currentId);
  const companyQ = useCurrentCompany();
  const gstRegistrationKnown = companyQ.data != null;
  const gstRegistered = companyQ.data?.gst_registered === true;
  const { data: accounts } = useQuery({
    queryKey: ["accounts", currentId],
    queryFn: async () => (await api.get<Account[]>("/accounts")).data,
    enabled: !!currentId,
  });

  const codeTypes = value.direction === "AR" ? ["INCOME"] : ["ASSET", "EXPENSE", "COST_OF_SALES"];
  const accountChoices = (accounts ?? [])
    .filter((a) => a.active && codeTypes.includes(a.type) && !(value.direction === "AP" && ["1000", "1100", "1200"].includes(a.code)))
    .sort((a, b) => a.code.localeCompare(b.code));

  const lineAccountType = (line: InvoiceLineDraft): string | null => {
    if (line.account_id === "" || line.account_id == null) return null;
    return accountChoices.find((account) => account.id === line.account_id)?.type ?? null;
  };

  const applyLines = (nextLines: InvoiceLineDraft[]) => {
    const fallbackTax = gstRegistered ? value.tax_code || "standard" : "none";
    const normalised = nextLines.map((line) => computeLine({ ...line, tax_code: line.tax_code || fallbackTax }, fallbackTax));
    const totals = summariseLines(normalised);
    onChange({
      ...value,
      ...totals,
      tax_code: normalised[0]?.tax_code ?? fallbackTax,
      account_id: normalised[0]?.account_id ?? "",
      lines: normalised,
    });
  };

  const updateLine = (index: number, changes: Partial<InvoiceLineDraft>) => {
    const nextLines = value.lines.map((line, i) => {
      if (i !== index) return line;
      const nextLine = { ...line, ...changes };
      if ((changes.account_id !== undefined || value.direction === "AR") && nextLine.account_id !== "" && value.direction === "AP") {
        const selectedType = accountChoices.find((account) => account.id === nextLine.account_id)?.type;
        if (selectedType !== "ASSET" && nextLine.tax_code === "capital") {
          nextLine.tax_code = "standard";
        }
      }
      if (value.direction === "AR") {
        const selectedType = nextLine.account_id === "" ? null : accountChoices.find((account) => account.id === nextLine.account_id)?.type ?? null;
        if (nextLine.account_id === "" || selectedType !== "INCOME") {
          nextLine.account_id = "";
        }
        nextLine.tax_code = "standard";
      }
      return nextLine;
    });
    applyLines(nextLines);
  };

  const addLine = () => {
    const fallbackTax = gstRegistered ? value.tax_code || "standard" : "none";
    const nextLines = [...value.lines, blankLine(fallbackTax)];
    applyLines(nextLines);
  };

  const removeLine = (index: number) => {
    if (value.lines.length <= 1) return;
    const nextLines = value.lines.filter((_, i) => i !== index);
    applyLines(nextLines);
  };

  const setDirection = (direction: InvoiceDirection) => {
    if (direction === value.direction) return;
    onChange({
      ...value,
      direction,
      account_id: "",
      tax_code: direction === "AR" ? "standard" : value.tax_code,
      lines: value.lines.map((line) => ({ ...line, account_id: "", tax_code: direction === "AR" ? "standard" : line.tax_code })),
    });
  };

  const validLines = value.lines.filter(lineIsValid);
  const taxOptionsForLine = (line: InvoiceLineDraft) => {
    const options: TaxCode[] = ["standard", "gst_free", "input_taxed", "none"];
    const assetSelected = value.direction === "AP" && lineAccountType(line) === "ASSET";
    if (assetSelected && line.tax_code === "capital") options.push("capital");
    return options;
  };
  const legacyFirstLine = value.lines[0] ?? blankLine(value.direction === "AR" ? "standard" : "gst_free");

  return (
    <div className="space-y-3">
      {showDirection && (
        <Field label="Direction">
          <div className="flex gap-2">
            <button
              type="button"
              className={`px-3 py-1 text-sm rounded border ${
                value.direction === "AP" ? "bg-emerald-600 text-white border-emerald-600" : "bg-surface text-slate-700 border-slate-300"
              }`}
              onClick={() => setDirection("AP")}
            >
              AP · Bill from supplier
            </button>
            <button
              type="button"
              className={`px-3 py-1 text-sm rounded border ${
                value.direction === "AR" ? "bg-emerald-600 text-white border-emerald-600" : "bg-surface text-slate-700 border-slate-300"
              }`}
              onClick={() => setDirection("AR")}
            >
              AR · Invoice to customer
            </button>
          </div>
        </Field>
      )}

      <div className="grid grid-cols-2 gap-3">
        <Field label={value.direction === "AP" ? "Supplier name" : "Customer name"}>
          <input className="input" value={value.contact_name} onChange={(e) => onChange({ ...value, contact_name: e.target.value })} />
        </Field>
        <Field label="ABN (optional)">
          <input className="input" value={value.contact_abn} onChange={(e) => onChange({ ...value, contact_abn: e.target.value })} />
        </Field>
      </div>

      {!showDirection && value.direction === "AR" && (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Income account (needed to post to the ledger)">
            <select
              aria-label="Income account (needed to post to the ledger)"
              className="input"
              value={legacyFirstLine.account_id === "" ? "" : String(legacyFirstLine.account_id)}
              onChange={(e) => {
                const nextAccountId = e.target.value === "" ? "" : Number(e.target.value);
                updateLine(0, {
                  ...legacyFirstLine,
                  description: legacyFirstLine.description || "Customer invoice",
                  quantity: legacyFirstLine.quantity || "1",
                  unit_price: legacyFirstLine.unit_price || "110.00",
                  account_id: nextAccountId,
                  tax_code: "standard",
                });
              }}
            >
              <option value="">Select account</option>
              {accountChoices.map((a) => (
                <option key={a.id} value={a.id}>{a.code} · {a.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Total (incl GST)">
            <input
              aria-label="Total (incl GST)"
              className="input"
              value={value.total}
              onChange={(e) => {
                const nextTotal = Number(stripMoney(e.target.value) || "0");
                updateLine(0, {
                  ...legacyFirstLine,
                  description: legacyFirstLine.description || "Customer invoice",
                  quantity: "1",
                  unit_price: moneyString(nextTotal),
                  account_id: legacyFirstLine.account_id,
                  tax_code: "standard",
                });
              }}
            />
          </Field>
        </div>
      )}

      <div className="grid grid-cols-3 gap-3">
        <Field label="Invoice #">
          <input className="input" value={value.invoice_number} onChange={(e) => onChange({ ...value, invoice_number: e.target.value })} />
        </Field>
        <Field label="Issue date" hint="(DD/MM/YYYY)">
          <DateInput value={value.issue_date} onChange={(v) => onChange({ ...value, issue_date: v })} />
        </Field>
        <Field label="Due date (optional)" hint="(DD/MM/YYYY)">
          <DateInput value={value.due_date} onChange={(v) => onChange({ ...value, due_date: v })} />
        </Field>
      </div>

      <div className="rounded-md border border-slate-200 bg-slate-50 p-3 space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-slate-700">Line items</span>
          <button type="button" className="btn-secondary text-xs" onClick={addLine}>
            + Add line
          </button>
        </div>

        {value.lines.map((line, index) => (
          <div key={`${index}-${line.description}`} className="rounded border border-slate-200 bg-white p-3 space-y-3">
            <div className="grid grid-cols-12 gap-2 items-end">
              <div className="col-span-5">
                <label className="block text-xs text-slate-600 mb-1">Description</label>
                <input
                  className="input"
                  value={line.description}
                  onChange={(e) => updateLine(index, { description: e.target.value })}
                />
              </div>
              <div className="col-span-2">
                <label className="block text-xs text-slate-600 mb-1">Qty</label>
                <input
                  className="input"
                  inputMode="decimal"
                  value={line.quantity}
                  onChange={(e) => updateLine(index, { quantity: e.target.value })}
                />
              </div>
              <div className="col-span-2">
                <label className="block text-xs text-slate-600 mb-1">Unit price</label>
                <input
                  className="input"
                  inputMode="decimal"
                  value={line.unit_price}
                  onChange={(e) => updateLine(index, { unit_price: e.target.value })}
                />
              </div>
              <div className="col-span-2">
                <label className="block text-xs text-slate-600 mb-1">Tax</label>
                <select
                  aria-label="Tax"
                  className="input"
                  value={line.tax_code}
                  onChange={(e) => updateLine(index, { tax_code: e.target.value as TaxCode })}
                >
                  {taxOptionsForLine(line).map((option) => (
                    <option key={option} value={option}>
                      {option === "capital" ? "Capital" : option === "standard" ? "GST" : option === "gst_free" ? "GST-free" : option === "input_taxed" ? "Input-taxed" : "None"}
                    </option>
                  ))}
                </select>
              </div>
              <div className="col-span-1 flex justify-end">
                <button
                  type="button"
                  className="btn-secondary text-xs px-2 py-1"
                  onClick={() => removeLine(index)}
                  disabled={value.lines.length <= 1}
                >
                  Remove
                </button>
              </div>
            </div>

            <div className="grid grid-cols-12 gap-2">
              <div className="col-span-4">
                <label className="block text-xs text-slate-600 mb-1">Income / asset account</label>
                <select
                  aria-label="Income / asset account"
                  className="input"
                  value={line.account_id === "" ? "" : String(line.account_id)}
                  onChange={(e) => {
                    const nextAccountId = e.target.value === "" ? "" : Number(e.target.value);
                    const nextAccountType = nextAccountId === "" ? null : accountChoices.find((account) => account.id === nextAccountId)?.type ?? null;
                    const nextLine: InvoiceLineDraft = {
                      ...line,
                      account_id: nextAccountId,
                      tax_code: value.direction === "AR"
                        ? "standard"
                        : nextAccountType === "ASSET" && line.tax_code === "capital"
                          ? "capital"
                          : nextAccountType !== "ASSET" && line.tax_code === "capital"
                            ? "standard"
                            : line.tax_code,
                    };
                    updateLine(index, nextLine);
                  }}
                >
                  <option value="">Select account</option>
                  {accountChoices.map((a) => (
                    <option key={`${a.id}-${index}`} value={a.id}>{a.code} · {a.name}</option>
                  ))}
                </select>
              </div>
              <div className="col-span-2">
                <label className="block text-xs text-slate-600 mb-1">GST rate</label>
                <input className="input bg-slate-100" value={line.gst_rate} readOnly />
              </div>
              <div className="col-span-2">
                <label className="block text-xs text-slate-600 mb-1">Subtotal</label>
                <input className="input bg-slate-100" value={line.line_subtotal} readOnly />
              </div>
              <div className="col-span-2">
                <label className="block text-xs text-slate-600 mb-1">GST</label>
                <input className="input bg-slate-100" value={line.line_gst} readOnly />
              </div>
              <div className="col-span-2">
                <label className="block text-xs text-slate-600 mb-1">Total</label>
                <input className="input bg-slate-100" value={line.line_total} readOnly />
              </div>
            </div>

            {!lineIsValid(line) && (
              <p className="text-xs text-amber-700">
                {line.description.trim() ? "" : "Description is required. "}
                {Number(stripMoney(line.quantity) || "0") <= 0 ? "Quantity must be greater than zero. " : ""}
                {!Number.isFinite(Number(stripMoney(line.unit_price) || "0")) || Number(stripMoney(line.unit_price) || "0") < 0 ? "Unit price must be valid and non-negative. " : ""}
                {line.account_id === "" || Number(line.account_id) <= 0 ? "Account is required. " : ""}
              </p>
            )}
          </div>
        ))}
      </div>

      <div className="grid grid-cols-3 gap-3">
        <Field label="Subtotal">
          <input className="input bg-slate-100" value={value.subtotal} readOnly />
        </Field>
        <Field label="GST">
          <input className="input bg-slate-100" value={value.gst_amount} readOnly />
        </Field>
        <Field label="Total">
          <input className="input bg-slate-100" value={value.total} readOnly />
        </Field>
      </div>

      <div className="flex items-center gap-4 text-xs text-slate-600">
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={value.gst_inclusive}
            onChange={(e) => onChange({ ...value, gst_inclusive: e.target.checked })}
          />
          GST inclusive
        </label>
        {!gstRegistrationKnown && <span className="text-slate-500">Company metadata loading…</span>}
        {!gstRegistered && gstRegistrationKnown && <span className="text-amber-700">Company is not GST-registered; GST is zero.</span>}
      </div>

      {validLines.length !== value.lines.length && (
        <p className="text-xs text-amber-700">Complete each line before submission.</p>
      )}

      <Field label="Notes">
        <textarea className="input min-h-[60px]" value={value.notes} onChange={(e) => onChange({ ...value, notes: e.target.value })} />
      </Field>

      <div className="text-xs text-slate-500">Header totals are derived from all visible lines and are not editable.</div>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="block text-slate-600 mb-1">
        {label} {hint && <span className="text-slate-400 font-normal">{hint}</span>}
      </span>
      {children}
    </label>
  );
}
