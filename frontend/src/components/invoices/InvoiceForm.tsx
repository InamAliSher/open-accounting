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
import InvoiceLineTable from "./InvoiceLineTable";

// Strip thousands separators so "1,000" parses/submits as 1000 instead of
// truncating to 1 (parseFloat) or 422-ing the backend Decimal parse.
function stripMoney(value: string): string {
  return (value ?? "").replace(/,/g, "").trim();
}

const TAX_CODES: TaxCode[] = ["standard", "gst_free", "input_taxed", "capital", "none"];
const QUANTITY_SCALE = 10000n;
const PRICE_SCALE = 100n;

export interface InvoiceLineFormValue {
  id: string;
  description: string;
  quantity: string;
  unit_price: string;
  account_id: number | "";
  tax_code: TaxCode;
}

let lineIdSequence = 0;

export function createInvoiceLine(): InvoiceLineFormValue {
  const id = globalThis.crypto?.randomUUID?.() ?? `invoice-line-${Date.now()}-${++lineIdSequence}`;
  return {
    id,
    description: "",
    quantity: "1",
    unit_price: "",
    account_id: "",
    tax_code: "gst_free",
  };
}

export function updateInvoiceLine(
  lines: InvoiceLineFormValue[],
  id: string,
  updates: Partial<Omit<InvoiceLineFormValue, "id">>,
): InvoiceLineFormValue[] {
  return lines.map((line) => line.id === id ? { ...line, ...updates } : line);
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
  lines: InvoiceLineFormValue[];
}

export function createEmptyInvoiceForm(direction: InvoiceDirection = "AP"): InvoiceFormValues {
  return {
    direction,
    contact_name: "",
    contact_abn: "",
    invoice_number: "",
    issue_date: "",
    due_date: "",
    subtotal: "",
    gst_amount: "",
    total: "",
    gst_inclusive: true,
    notes: "",
    lines: [createInvoiceLine()],
  };
}

const EMPTY_FORM_LINE = Object.freeze(createInvoiceLine());
export const EMPTY_FORM = Object.freeze({
  direction: "AP" as const,
  contact_name: "",
  contact_abn: "",
  invoice_number: "",
  issue_date: "",
  due_date: "",
  subtotal: "",
  gst_amount: "",
  total: "",
  gst_inclusive: true,
  notes: "",
  lines: Object.freeze([EMPTY_FORM_LINE]) as unknown as InvoiceLineFormValue[],
}) as InvoiceFormValues;

interface InvoiceAmounts {
  subtotalCents: bigint;
  lines: Map<string, string | null>;
}

function parseScaledInteger(value: string, decimalPlaces: number): bigint | null {
  const raw = value.trim();
  if (raw.includes(",") && !/^\d{1,3}(?:,\d{3})*(?:\.\d+)?$/.test(raw)) return null;
  const normalized = stripMoney(raw);
  const pattern = new RegExp(`^(\\d+)(?:\\.(\\d{1,${decimalPlaces}}))?$`);
  const match = pattern.exec(normalized);
  if (!match) return null;
  const scale = 10n ** BigInt(decimalPlaces);
  const fraction = BigInt((match[2] ?? "").padEnd(decimalPlaces, "0") || "0");
  return BigInt(match[1]) * scale + fraction;
}

function parseQuantityUnits(value: string): bigint | null {
  return parseScaledInteger(value, 4);
}

function parsePriceCents(value: string): bigint | null {
  return parseScaledInteger(value, 2);
}

function formatCents(cents: bigint): string {
  const dollars = cents / PRICE_SCALE;
  const remainder = String(cents % PRICE_SCALE).padStart(2, "0");
  return `${dollars}.${remainder}`;
}

function neutralLineCents(line: InvoiceLineFormValue): bigint | null {
  const quantityUnits = parseQuantityUnits(line.quantity);
  const priceCents = parsePriceCents(line.unit_price);
  if (quantityUnits === null || priceCents === null) return null;
  return (quantityUnits * priceCents + 5000n) / QUANTITY_SCALE;
}

export function calculateNeutralLineAmount(
  quantity: string,
  unitPrice: string,
): string | null {
  const cents = neutralLineCents({
    id: "calculation",
    description: "",
    quantity,
    unit_price: unitPrice,
    account_id: "",
    tax_code: "gst_free",
  });
  return cents === null ? null : formatCents(cents);
}

function calculateInvoiceAmounts(lines: InvoiceLineFormValue[]): InvoiceAmounts {
  const lineAmounts = new Map<string, string | null>();
  let subtotalCents = 0n;
  for (const line of lines) {
    const cents = neutralLineCents(line);
    lineAmounts.set(line.id, cents === null ? null : formatCents(cents));
    if (cents !== null) subtotalCents += cents;
  }
  return { subtotalCents, lines: lineAmounts };
}

function structurallyComplete(line: InvoiceLineFormValue, direction: InvoiceDirection): boolean {
  const quantity = parseQuantityUnits(line.quantity);
  const price = parsePriceCents(line.unit_price);
  return line.description.trim() !== "" &&
    quantity !== null && quantity > 0n &&
    price !== null &&
    Number.isInteger(line.account_id) && Number(line.account_id) > 0 &&
    TAX_CODES.includes(line.tax_code) &&
    !(direction === "AR" && line.tax_code === "capital");
}

export function synchronizeInvoiceAmounts(value: InvoiceFormValues): InvoiceFormValues {
  const amounts = calculateInvoiceAmounts(value.lines);
  const complete = value.lines.length > 0 && value.lines.every((line) =>
    structurallyComplete(line, value.direction) && amounts.lines.get(line.id) !== null,
  );
  return {
    ...value,
    subtotal: complete ? formatCents(amounts.subtotalCents) : "",
    gst_amount: complete ? "0.00" : "",
    total: complete ? formatCents(amounts.subtotalCents) : "",
  };
}

function validIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

export function validateInvoiceForm(
  value: InvoiceFormValues,
  _gstRegistered = true,
): string[] {
  const errors: string[] = [];
  if (!value.contact_name.trim()) errors.push("Customer or supplier name is required.");
  if (!value.invoice_number.trim()) errors.push("Invoice number is required.");
  if (!validIsoDate(value.issue_date)) errors.push("A valid issue date is required.");
  if (!value.lines.length) errors.push("At least one invoice line is required.");

  for (const [index, line] of value.lines.entries()) {
    const label = `Line ${index + 1}`;
    if (!line.description.trim()) errors.push(`${label}: description is required.`);
    const quantity = parseQuantityUnits(line.quantity);
    if (quantity === null || quantity <= 0n) errors.push(`${label}: quantity must be greater than zero.`);
    const unitPrice = parsePriceCents(line.unit_price);
    if (unitPrice === null) errors.push(`${label}: enter a valid unit price.`);
    if (!Number.isInteger(line.account_id) || Number(line.account_id) < 1) errors.push(`${label}: account is required.`);
    if (!TAX_CODES.includes(line.tax_code)) errors.push(`${label}: tax rate is required.`);
    if (line.tax_code === "capital" && value.direction !== "AP") {
      errors.push(`${label}: capital tax treatment is only valid for AP invoices.`);
    }
  }

  const amounts = calculateInvoiceAmounts(value.lines);
  const complete = value.lines.length > 0 && value.lines.every((line) =>
    structurallyComplete(line, value.direction) && amounts.lines.get(line.id) !== null,
  );
  if (complete && (
    value.subtotal !== formatCents(amounts.subtotalCents) ||
    value.gst_amount !== "0.00" ||
    value.total !== formatCents(amounts.subtotalCents)
  )) {
    errors.push("Invoice amounts are inconsistent with the invoice lines.");
  }
  return errors;
}

export function toCreatePayload(
  v: InvoiceFormValues,
  opts: {
    source?: "manual" | "pdf" | "excel";
    attachment_id?: string | null;
    gst_registered?: boolean;
  } = {}
): InvoiceCreate {
  const gstRegistered = opts.gst_registered !== false;
  const amounts = calculateInvoiceAmounts(v.lines);
  const lines = v.lines.map((line) => {
    const lineSubtotal = amounts.lines.get(line.id) ?? "0.00";
    return {
      description: line.description.trim(),
      quantity: stripMoney(line.quantity) || "0",
      unit_price: stripMoney(line.unit_price) || "0",
      account_id: line.account_id === "" ? null : line.account_id,
      line_subtotal: lineSubtotal,
      line_gst: "0.00",
      line_total: lineSubtotal,
      tax_code: gstRegistered ? line.tax_code : "none",
    };
  });
  const subtotal = formatCents(amounts.subtotalCents);
  const gst = "0.00";
  const total = subtotal;
  return {
    direction: v.direction,
    contact_name: v.contact_name.trim() || null,
    contact_abn: v.contact_abn.trim() || null,
    invoice_number: v.invoice_number.trim(),
    issue_date: v.issue_date,
    due_date: v.due_date || null,
    subtotal,
    gst_amount: gst,
    total,
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
  const companyQ = useCurrentCompany();
  const gstRegistered = companyQ.data?.gst_registered === true;
  const set = <K extends keyof InvoiceFormValues>(k: K, v: InvoiceFormValues[K]) =>
    onChange(synchronizeInvoiceAmounts({ ...value, [k]: v }));
  const setDirection = (direction: InvoiceDirection) => {
    if (direction === value.direction) return;
    const lines = value.lines.map((line) => ({
      ...line,
      account_id: "" as const,
      tax_code: direction === "AR" && line.tax_code === "capital"
        ? "gst_free" as TaxCode
        : line.tax_code,
    }));
    onChange(synchronizeInvoiceAmounts({ ...value, direction, lines }));
  };
  const updateLine = (
    id: string,
    updates: Partial<Omit<InvoiceLineFormValue, "id">>,
  ) => onChange(synchronizeInvoiceAmounts({
    ...value,
    lines: updateInvoiceLine(value.lines, id, updates),
  }));
  const addLine = () => onChange(synchronizeInvoiceAmounts({
    ...value,
    lines: [...value.lines, createInvoiceLine()],
  }));
  const removeLine = (id: string) => {
    if (value.lines.length <= 1) return;
    onChange(synchronizeInvoiceAmounts({
      ...value,
      lines: value.lines.filter((line) => line.id !== id),
    }));
  };

  // Keyed by company id like every other accounts query: per-company SQLite
  // ids collide across companies, so serving a stale cross-company list could
  // code an invoice line to the wrong account after a switch.
  const currentId = useCompanyStore((s) => s.currentId);
  const { data: accounts } = useQuery({
    queryKey: ["accounts", currentId],
    queryFn: async () => (await api.get<Account[]>("/accounts")).data,
    enabled: !!currentId,
  });
  // AR is income; AP may be an ordinary expense/COGS or an asset acquisition.
  const codeTypes =
    value.direction === "AR"
      ? ["INCOME"]
      : ["ASSET", "EXPENSE", "COST_OF_SALES"];
  const accountChoices = (accounts ?? [])
    .filter(
      (a) =>
        a.active &&
        codeTypes.includes(a.type) &&
        !(
          value.direction === "AP" &&
          ["1000", "1100", "1200"].includes(a.code)
        ),
    )
    .sort((a, b) => a.code.localeCompare(b.code));
  const amounts = calculateInvoiceAmounts(value.lines);

  return (
    <div className="space-y-3">
      {showDirection && (
        <Field label="Direction">
          <div className="flex gap-2">
            <button
              type="button"
              className={`px-3 py-1 text-sm rounded border ${
                value.direction === "AP"
                  ? "bg-emerald-600 text-white border-emerald-600"
                  : "bg-surface text-slate-700 border-slate-300"
              }`}
              onClick={() => setDirection("AP")}
            >
              AP · Bill from supplier
            </button>
            <button
              type="button"
              className={`px-3 py-1 text-sm rounded border ${
                value.direction === "AR"
                  ? "bg-emerald-600 text-white border-emerald-600"
                  : "bg-surface text-slate-700 border-slate-300"
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
          <input
            className="input"
            value={value.contact_name}
            onChange={(e) => set("contact_name", e.target.value)}
          />
        </Field>
        <Field label="ABN (optional)">
          <input
            className="input"
            value={value.contact_abn}
            onChange={(e) => set("contact_abn", e.target.value)}
          />
        </Field>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <Field label="Invoice #">
          <input
            className="input"
            value={value.invoice_number}
            onChange={(e) => set("invoice_number", e.target.value)}
          />
        </Field>
        <Field label="Issue date" hint="(DD/MM/YYYY)">
          <DateInput
            value={value.issue_date}
            onChange={(v) => set("issue_date", v)}
          />
        </Field>
        <Field label="Due date (optional)" hint="(DD/MM/YYYY)">
          <DateInput
            value={value.due_date}
            onChange={(v) => set("due_date", v)}
          />
        </Field>
      </div>

      <InvoiceLineTable
        lines={value.lines}
        amounts={amounts.lines}
        accounts={accountChoices}
        direction={value.direction}
        gstRegistered={gstRegistered}
        onChange={updateLine}
        onAdd={addLine}
        onRemove={removeLine}
      />

      <div className="grid grid-cols-3 gap-3 border-t border-slate-200 pt-3 text-right tabular-nums">
        <div>
          <div className="text-xs text-slate-500">Subtotal</div>
          <output aria-label="Subtotal" className="font-medium">{formatCents(amounts.subtotalCents)}</output>
        </div>
        <div>
          <div className="text-xs text-slate-500">GST</div>
          <output aria-label="GST total" className="font-medium">0.00</output>
        </div>
        <div>
          <div className="text-xs text-slate-500">Total</div>
          <output aria-label="Invoice total" className="font-semibold">{formatCents(amounts.subtotalCents)}</output>
        </div>
      </div>

      <Field label="Notes">
        <textarea
          className="input min-h-[60px]"
          value={value.notes}
          onChange={(e) => set("notes", e.target.value)}
        />
      </Field>
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
