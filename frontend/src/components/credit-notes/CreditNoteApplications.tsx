import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { formatDate, formatMoney } from "../../lib/format";
import { useCompanyStore } from "../../store/company";
import type {
  CreditNote,
  CreditNoteApplication,
  Invoice,
} from "../../types/api";
import { ConfirmDialog } from "../ui/ConfirmDialog";

const CENTS_SCALE = 100n;

function moneyToCents(value: string): bigint | null {
  const match = value.trim().match(/^(\d+)(?:\.(\d{0,2}))?$/);
  if (!match) return null;
  try {
    return BigInt(match[1]) * CENTS_SCALE + BigInt((match[2] ?? "").padEnd(2, "0"));
  } catch {
    return null;
  }
}

function centsToMoney(value: bigint): string {
  return `${value / CENTS_SCALE}.${(value % CENTS_SCALE).toString().padStart(2, "0")}`;
}

function applicationStatusLabel(application: CreditNoteApplication): string {
  return application.status === "reversed" ? "reversed" : "active";
}

export default function CreditNoteApplications({ creditNote }: { creditNote: CreditNote }) {
  const queryClient = useQueryClient();
  const currentId = useCompanyStore((state) => state.currentId);
  const idempotencyKey = useRef(crypto.randomUUID());
  const [invoiceId, setInvoiceId] = useState<number | null>(null);
  const [amount, setAmount] = useState("");
  const [applicationDate, setApplicationDate] = useState("");
  const [confirmApply, setConfirmApply] = useState(false);
  const [reverseApplication, setReverseApplication] = useState<CreditNoteApplication | null>(null);
  const [reversalDate, setReversalDate] = useState("");
  const [error, setError] = useState<string | null>(null);

  const invoicesQuery = useQuery({
    queryKey: ["invoices", currentId, "authorised", creditNote.contact_id, creditNote.direction],
    queryFn: async () => {
      const response = await api.get<Invoice[]>("/invoices", {
        params: {
          direction: creditNote.direction,
          status: "authorised",
          contact_id: creditNote.contact_id,
        },
      });
      return response.data;
    },
    enabled: !!currentId,
    retry: false,
  });

  const eligibleInvoices = useMemo(() => {
    if (!invoicesQuery.data) return [];
    return invoicesQuery.data.filter((invoice) =>
      invoice.direction === creditNote.direction &&
      invoice.contact_id === creditNote.contact_id &&
      invoice.currency === creditNote.currency &&
      invoice.status === "authorised" &&
      moneyToCents(invoice.outstanding_amount ?? "0") !== null &&
      (moneyToCents(invoice.outstanding_amount ?? "0") ?? 0n) > 0n,
    );
  }, [creditNote, invoicesQuery.data]);

  const selectedInvoice = eligibleInvoices.find((invoice) => invoice.id === invoiceId) ?? null;
  const remainingCents = moneyToCents(creditNote.remaining_amount);
  const outstandingCents = selectedInvoice ? moneyToCents(selectedInvoice.outstanding_amount) : null;
  const requestedCents = moneyToCents(amount);
  const amountError =
    !amount.trim()
      ? "Enter an application amount."
      : requestedCents === null || requestedCents <= 0n
        ? "Enter a positive amount with up to two decimal places."
        : remainingCents === null || requestedCents > remainingCents
          ? "Application exceeds the credit note's remaining amount."
          : outstandingCents === null || requestedCents > outstandingCents
            ? "Application exceeds the invoice's outstanding amount."
            : null;
  const applicationError = !applicationDate
    ? "Choose an application date."
    : amountError;

  const applyMutation = useMutation({
    mutationFn: async () => {
      const response = await api.post<CreditNoteApplication>(
        `/credit-notes/${creditNote.id}/applications`,
        {
          invoice_id: selectedInvoice!.id,
          amount: centsToMoney(requestedCents!),
          application_date: applicationDate,
        },
        {
          headers: { "Idempotency-Key": idempotencyKey.current },
        },
      );
      return response.data;
    },
    onSuccess: async () => {
      setConfirmApply(false);
      setAmount("");
      setApplicationDate("");
      setInvoiceId(null);
      setError(null);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["credit-notes", "detail", currentId, creditNote.id],
          exact: true,
        }),
        queryClient.invalidateQueries({ queryKey: ["invoices", currentId] }),
      ]);
    },
    onError: (caught) => setError(apiErrorMessage(caught)),
  });

  const reverseMutation = useMutation({
    mutationFn: async () => {
      const response = await api.post<CreditNoteApplication>(
        `/credit-note-applications/${reverseApplication!.id}/reverse`,
        { reversal_date: reversalDate },
      );
      return response.data;
    },
    onSuccess: async () => {
      setReverseApplication(null);
      setReversalDate("");
      setError(null);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["credit-notes", "detail", currentId, creditNote.id],
          exact: true,
        }),
        queryClient.invalidateQueries({ queryKey: ["invoices", currentId] }),
      ]);
    },
    onError: (caught) => setError(apiErrorMessage(caught)),
  });

  const invoiceNumber = (invoiceId: number) =>
    invoicesQuery.data?.find((invoice) => invoice.id === invoiceId)?.invoice_number ?? "Unknown invoice";

  const submitApplication = () => {
    if (!selectedInvoice || !requestedCents || applicationError) return;
    setError(null);
    setConfirmApply(true);
  };

  const confirmApplication = () => {
    if (!selectedInvoice || !requestedCents) return;
    void applyMutation.mutate();
  };

  const confirmReverse = () => {
    if (!reverseApplication || !reversalDate) return;
    void reverseMutation.mutate();
  };

  return (
    <section className="border border-slate-200 rounded p-3 space-y-3" aria-label="Credit note applications">
      <div>
        <h3 className="text-sm font-medium text-slate-900">Apply credit</h3>
        <p className="text-xs text-slate-500">
          Available credit: {formatMoney(creditNote.remaining_amount, creditNote.currency)} ·
          {" "}Applied: {formatMoney(creditNote.applied_amount, creditNote.currency)}
        </p>
      </div>

      {invoicesQuery.isLoading && <p className="text-xs text-slate-500">Loading eligible invoices…</p>}
      {invoicesQuery.isError && (
        <p className="text-sm text-rose-700" role="alert">
          {apiErrorMessage(invoicesQuery.error)}
        </p>
      )}
      {error && <p className="text-sm text-rose-700" role="alert">{error}</p>}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
        <label className="text-xs text-slate-600">
          <span className="block mb-1">Invoice</span>
          <select
            className="input w-full"
            aria-label="Invoice to apply"
            value={invoiceId ?? ""}
            onChange={(event) => {
              setInvoiceId(event.target.value ? Number(event.target.value) : null);
              setAmount("");
            }}
          >
            <option value="">Select invoice</option>
            {eligibleInvoices.map((invoice) => (
              <option key={invoice.id} value={invoice.id}>
                {invoice.invoice_number} · {formatMoney(invoice.outstanding_amount, invoice.currency)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-slate-600">
          <span className="block mb-1">Amount</span>
          <input
            className="input w-full"
            type="number"
            min="0.01"
            step="0.01"
            inputMode="decimal"
            aria-label="Application amount"
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            placeholder="0.00"
          />
        </label>
        <label className="text-xs text-slate-600">
          <span className="block mb-1">Application date</span>
          <input
            className="input w-full"
            type="date"
            aria-label="Application date"
            value={applicationDate}
            onChange={(event) => setApplicationDate(event.target.value)}
          />
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {selectedInvoice && (
          <span className="text-xs text-slate-500">
            Maximum {formatMoney(
              centsToMoney(
                (remainingCents ?? 0n) < (outstandingCents ?? 0n)
                  ? remainingCents ?? 0n
                  : outstandingCents ?? 0n,
              ),
              creditNote.currency,
            )}
          </span>
        )}
        <button
          type="button"
          className="btn-primary text-xs"
          disabled={!selectedInvoice || !applicationDate || !!amountError || applyMutation.isPending}
          onClick={submitApplication}
        >
          {applyMutation.isPending ? "Applying…" : "Apply"}
        </button>
      </div>

      {eligibleInvoices.length === 0 && !invoicesQuery.isLoading && (
        <p className="text-xs text-slate-500">
          No eligible authorised invoices match this credit note's Contact, direction, and currency.
        </p>
      )}

      <div>
        <h4 className="text-xs font-medium text-slate-700 mb-1">Application history</h4>
        {creditNote.applications.length === 0 ? (
          <p className="text-xs text-slate-500">No applications yet.</p>
        ) : (
          <div className="overflow-auto border border-slate-200 rounded">
            <table className="w-full text-xs min-w-[620px]">
              <thead className="text-left text-slate-500 border-b bg-slate-50">
                <tr>
                  <th className="p-2">Invoice number</th>
                  <th className="p-2 text-right">Amount</th>
                  <th className="p-2">Application date</th>
                  <th className="p-2">Status</th>
                  <th className="p-2">Reversal date</th>
                  <th className="p-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {creditNote.applications.map((application) => {
                  const status = applicationStatusLabel(application);
                  return (
                    <tr key={application.id} className="border-b last:border-b-0">
                      <td className="p-2 font-mono">{invoiceNumber(application.invoice_id)}</td>
                      <td className="p-2 text-right">{formatMoney(application.amount, creditNote.currency)}</td>
                      <td className="p-2">{formatDate(application.application_date)}</td>
                      <td className="p-2">
                        <span className={status === "active" ? "text-emerald-700" : "text-slate-500"}>
                          {status}
                        </span>
                      </td>
                      <td className="p-2">{formatDate(application.reversal_date)}</td>
                      <td className="p-2 text-right">
                        {status === "active" ? (
                          <button
                            type="button"
                            className="text-rose-700 hover:underline"
                            onClick={() => {
                              setReverseApplication(application);
                              setReversalDate("");
                            }}
                          >
                            Reverse
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmApply}
        title="Apply this credit to the invoice?"
        message={
          <div className="space-y-2">
            <p>
              Applying {formatMoney(requestedCents ? centsToMoney(requestedCents) : "0.00", creditNote.currency)}
              {" "}will decrease the credit note remaining amount and the invoice outstanding amount by the same amount.
            </p>
            <p className="font-medium">
              Invoice: {selectedInvoice?.invoice_number ?? "Unknown"} · Application date: {applicationDate}
            </p>
          </div>
        }
        confirmLabel="Apply credit"
        busy={applyMutation.isPending}
        onCancel={() => setConfirmApply(false)}
        onConfirm={confirmApplication}
      />

      <ConfirmDialog
        open={reverseApplication !== null}
        destructive
        title="Reverse this credit application?"
        message={
          <div className="space-y-2">
            <p>
              This will reverse the application of {formatMoney(reverseApplication?.amount ?? "0", creditNote.currency)}
              {" "}from {invoiceNumber(reverseApplication?.invoice_id ?? 0)}.
            </p>
            <label className="block text-xs text-slate-600">
              <span className="block mb-1">Reversal date</span>
              <input
                className="input w-full"
                type="date"
                aria-label="Reversal date"
                value={reversalDate}
                onChange={(event) => setReversalDate(event.target.value)}
              />
            </label>
          </div>
        }
        confirmLabel="Reverse application"
        busy={reverseMutation.isPending}
        onCancel={() => {
          setReverseApplication(null);
          setReversalDate("");
        }}
        onConfirm={confirmReverse}
      />
    </section>
  );
}
