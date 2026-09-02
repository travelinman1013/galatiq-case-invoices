"""The four model-backed roles. Each is one structured-output call with its own brief.

`Agents` needs a model; `OfflineAgents` is what runs when there is none — it never guesses.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from acme_ap import db
from acme_ap import llm as llm_mod
from acme_ap.models import Action, Critique, Decision, Finding, Invoice, InvoiceDraft

MAX_TOOL_ROUNDS = 4

EXTRACT_SYSTEM = """You are an accounts-payable clerk turning a supplier invoice into a fixed record.

Rules:
- Copy every number exactly as printed. Do not correct arithmetic; do not fill gaps with guesses.
- Use null for anything the document does not state.
- Catalog items are: {catalog}. If a line clearly names one of them with different spacing or
  case (e.g. "Widget A"), use the catalog spelling. Any other product name stays exactly as printed.
- Qualifiers such as "(rush order)" go in the line's `note`, not in `item`.
- `vendor` is the party billing us (not the recipient, and not an email header). If the document
  says "formerly X", keep the current name and mention the former name in `notes`.
- `notes` collects any free text: urgency language, PO references, delivery instructions.
- Dates: copy as written; the system normalizes them. Relative words like "yesterday" are fine.
- `tax`, `shipping`, `subtotal`, `total`: the stated amounts, if present."""

CRITIQUE_EXTRACT_SYSTEM = """You audit an invoice extraction against its source document.

Evidence from a deterministic pre-check is included. Set ok=true only when every extracted field is
supported by the source and no line item is missing. Otherwise set ok=false and list at most three
concrete issues, one short sentence each, naming the field and what the source actually says.
Do not nitpick formatting or normalization (spacing, casing, date formats)."""

VP_SYSTEM = """You are the VP of Finance at Acme Corp, deciding what happens to a supplier invoice.

A deterministic policy engine has already checked stock, arithmetic, dates, the vendor master and the
payment ledger, and set a floor for this invoice: **{floor}**. You may keep that outcome or make it
MORE conservative (approve → escalate → reject). You can never loosen it — that is enforced in code.

Use the tools to verify the facts that matter (inventory, vendor, ledger history) before deciding.
Tighten the floor only for a concrete fact you verified or that appears in the findings — not for
documents this system does not hold (purchase orders, goods receipts, contracts); the policy floor
already encodes what is required. A clean invoice with no findings should be approved.
"escalate" means a human reviews it. Explain your decision in 2–4 plain sentences for a finance
audience, citing the facts you checked."""

CRITIQUE_DECISION_SYSTEM = """You are a skeptical financial controller reviewing a VP's decision on an invoice.

Look for a fact in the invoice or the findings that contradicts the decision or that the rationale
ignores. The policy floor cannot be loosened, so only argue for a MORE conservative outcome.
Rules:
- Code has already checked, and you must NOT re-litigate: stock levels, arithmetic, due date versus
  payment terms (a few days of calendar rounding is normal), the vendor master, and ledger duplicates.
  Anything those checks accepted is settled unless a finding says otherwise.
- The absence of documents this system does not hold (PO, goods receipt, contract, approval email) is
  NOT an objection. Hypothetical risks are not objections. The floor already encodes what is required.
- If there are no findings, the decision stands: ok=true, no issues.
- Object (ok=false) only with at most three concrete reasons, one short sentence each, each tied to a
  finding the rationale ignored or a fact on the invoice the rationale got wrong."""


def _invoice_brief(invoice: Invoice, findings: list[Finding], floor: Action | None = None) -> str:
    lines = [
        f"Invoice {invoice.invoice_number or '?'} from {invoice.vendor or '(no vendor)'}",
        f"Issued {invoice.issue_date}, due {invoice.due_date}, terms {invoice.payment_terms!r}",
        f"Total {invoice.total} {invoice.currency} "
        f"(subtotal {invoice.subtotal}, tax {invoice.tax}, shipping {invoice.shipping})",
        "Lines:",
    ]
    for li in invoice.line_items:
        note = f" [{li.note}]" if li.note else ""
        lines.append(f"  - {li.item} × {li.quantity} @ {li.unit_price}{note}")
    if invoice.notes:
        lines.append(f"Notes on invoice: {invoice.notes}")
    lines.append("Validation findings:" if findings else "Validation findings: none")
    for f in findings:
        lines.append(f"  - [{f.severity}] {f.code}: {f.message}")
    if floor:
        lines.append(f"Policy floor: {floor}")
    return "\n".join(lines)


class Agents:
    """Model-backed roles. One instance per run."""

    def __init__(self, model: Any) -> None:
        self.model = model

    # -- ingestion loop -------------------------------------------------------------------

    def extract(self, raw_text: str, issues: list[str], source_path: str) -> Invoice:
        system = EXTRACT_SYSTEM.format(catalog=", ".join(db.catalog_items()))
        user = f"Extract this invoice.\n\n<document>\n{raw_text}\n</document>"
        if issues:
            user += "\n\nA reviewer rejected the previous attempt for these reasons — fix them:\n" + "\n".join(
                f"- {i}" for i in issues
            )
        draft = llm_mod.structured(self.model, InvoiceDraft, [SystemMessage(system), HumanMessage(user)])
        data = draft.model_dump()
        data["line_items"] = [li for li in data["line_items"] if li.get("item")]
        return Invoice(**data, source_path=source_path, source_kind="llm")

    def critique_extraction(self, raw_text: str, invoice: Invoice) -> Critique:
        evidence = _precheck(raw_text, invoice)
        user = (
            f"<document>\n{raw_text}\n</document>\n\n<extraction>\n{_invoice_brief(invoice, [])}\n</extraction>\n\n"
            f"<deterministic_precheck>\n{evidence}\n</deterministic_precheck>"
        )
        return llm_mod.structured(self.model, Critique, [SystemMessage(CRITIQUE_EXTRACT_SYSTEM), HumanMessage(user)])

    # -- approval loop --------------------------------------------------------------------

    def vp_decide(self, invoice: Invoice, findings: list[Finding], floor: Action, objections: list[str]) -> Decision:
        from langchain.agents import create_agent
        from langchain.agents.structured_output import ToolStrategy

        agent = create_agent(
            self.model,
            tools=[lookup_item, lookup_vendor, ledger_history],
            system_prompt=VP_SYSTEM.format(floor=floor),
            response_format=ToolStrategy(Decision),
        )
        user = _invoice_brief(invoice, findings, floor)
        if objections:
            user += "\n\nA controller objected to your previous decision:\n" + "\n".join(f"- {o}" for o in objections)
            user += (
                "\nReconsider and decide again. The controller can be wrong: keep your decision if the objection "
                "is not a concrete fact from the findings or the invoice, and say why."
            )
        result = agent.invoke(
            {"messages": [HumanMessage(user)]},
            config={"recursion_limit": 2 * MAX_TOOL_ROUNDS + 4},
        )
        for msg in result.get("messages", []):
            if getattr(msg, "usage_metadata", None):
                llm_mod.record_usage(msg)
        decision = result.get("structured_response")
        if not isinstance(decision, Decision):
            raise RuntimeError("VP agent did not return a Decision")
        return decision

    def critique_decision(self, invoice: Invoice, findings: list[Finding], decision: Decision) -> Critique:
        user = (
            f"{_invoice_brief(invoice, findings)}\n\nVP decision: {decision.action}\nVP rationale: {decision.rationale}"
        )
        return llm_mod.structured(self.model, Critique, [SystemMessage(CRITIQUE_DECISION_SYSTEM), HumanMessage(user)])


class OfflineAgents(Agents):
    """No model configured. Structured invoices are unaffected; unstructured ones are not guessed."""

    def __init__(self) -> None:
        super().__init__(model=None)

    def extract(self, raw_text: str, issues: list[str], source_path: str) -> Invoice:
        return Invoice(source_path=source_path, source_kind="unextracted")

    def critique_extraction(self, raw_text: str, invoice: Invoice) -> Critique:
        return Critique(ok=True)

    def vp_decide(self, invoice: Invoice, findings: list[Finding], floor: Action, objections: list[str]) -> Decision:
        return Decision(action=floor, rationale="Policy floor applied; no model configured to reason further.")

    def critique_decision(self, invoice: Invoice, findings: list[Finding], decision: Decision) -> Critique:
        return Critique(ok=True)


def build_agents() -> Agents:
    model = llm_mod.get_llm()
    return OfflineAgents() if model is None else Agents(model)


# -- tools the VP agent may call ---------------------------------------------------------


@tool
def lookup_item(item: str) -> dict:
    """Look up a catalog item: returns stock on hand and catalog unit price, or not_found."""
    return db.lookup_item(item) or {"item": item, "not_found": True}


@tool
def lookup_vendor(name: str) -> dict:
    """Look up a vendor in the approved-vendor master; returns approval status and notes, or not_found."""
    return db.lookup_vendor(name) or {"name": name, "not_found": True}


@tool
def ledger_history(invoice_number: str) -> list[dict]:
    """Previous ledger entries for this invoice number (paid, rejected, pending_review)."""
    return db.ledger_history(invoice_number)


# -- deterministic evidence for the extraction critic -------------------------------------

_LINEISH = re.compile(r"(qty|x\s?\d|@|\$\s?\d)", re.IGNORECASE)


def _precheck(raw_text: str, invoice: Invoice) -> str:
    lineish = sum(1 for line in raw_text.splitlines() if _LINEISH.search(line) and re.search(r"\d", line))
    recomputed = invoice.recomputed_total()
    parts = [
        f"extracted line items: {len(invoice.line_items)}",
        f"source lines that look like line items or amounts: {lineish}",
        f"recomputed total from lines + tax + shipping: {recomputed}",
        f"stated total: {invoice.total}",
    ]
    if recomputed is not None and invoice.total is not None and abs(recomputed - invoice.total) > 1:
        parts.append(
            "NOTE: a mismatch here can be a genuine invoice error — only flag it if the extraction "
            "misread a number that the document actually states."
        )
    return "\n".join(parts)
