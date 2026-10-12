"""The nine questions an operator must never have collapsed into one word.

`PREFLIGHT_OK`, `REALISED`, `ENTRY_ECONOMICS_PASS` and "the order gate is open"
answer DIFFERENT questions. Reading any one of them as another is how a
readable venue becomes "ready to trade" and a booked number becomes "income".
This module is the one place the answers are assembled, from the facts each
surface has, so preflight and the carry report cannot drift into saying
different things. A fact a surface does not have is reported as
``NOT_CHECKED`` / ``UNKNOWN``, never defaulted to a reassuring value.

The answers are vocabulary-disjoint on purpose: no question's answer can be
mistaken for another question's.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Position agreement between journal and venue, in base units.
POSITION_DUST = 1e-6


def _lifecycle_reasons(statement: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for lc in statement.get("funding_lifecycles", []):
        for r in lc.get("reasons", []):
            if r not in out:
                out.append(r)
    return out


def nine_questions(*, reads: Optional[Dict[str, bool]] = None,
                   venue_perp_qty: Optional[float] = None,
                   statement: Optional[Dict[str, Any]] = None,
                   economics: Optional[Dict[str, Any]] = None,
                   order_gate: Optional[bool] = None) -> List[Dict[str, Any]]:
    """The nine answers, each with the evidence it rests on.

    ``reads`` maps check name to ok; ``venue_perp_qty`` is the venue's perp
    size if it was read; ``statement`` is ``Journal.statement()``; ``economics``
    is the preflight entry-economics block; ``order_gate`` is whether the carry
    order gate allows orders.
    """
    q: List[Dict[str, Any]] = []

    def add(n: int, key: str, question: str, answer: str, **evidence: Any) -> None:
        q.append({"n": n, "key": key, "question": question, "answer": answer,
                  "evidence": evidence})

    market = ("mark_perp", "mark_spot", "funding_bps", "fee_perp", "fee_spot")
    if reads is None:
        add(1, "market_data", "Were venue and market-data requests readable?",
            "NOT_CHECKED")
        add(2, "account_position", "Was account position state readable?",
            "NOT_CHECKED")
    else:
        bad = [k for k in market if k in reads and not reads[k]]
        add(1, "market_data", "Were venue and market-data requests readable?",
            "MARKET_DATA_READABLE" if not bad and any(k in reads for k in market)
            else "MARKET_DATA_UNREADABLE", unreadable=bad)
        acct = ("perp_position", "spot_inventory", "open_carry_orders", "equity")
        bad = [k for k in acct if k in reads and not reads[k]]
        add(2, "account_position", "Was account position state readable?",
            "ACCOUNT_STATE_READABLE" if not bad and any(k in reads for k in acct)
            else "ACCOUNT_STATE_UNREADABLE", unreadable=bad)

    if statement is None:
        add(3, "book_position", "Was the book's signed position independently "
            "reconstructable?", "NO_JOURNAL")
    else:
        journal_qty = float(statement.get("open_perp_qty", 0.0))
        if venue_perp_qty is None:
            add(3, "book_position", "Was the book's signed position "
                "independently reconstructable?", "JOURNAL_ONLY_NOT_COMPARED",
                journal_signed_perp_qty=journal_qty)
        else:
            same = abs(abs(journal_qty) - abs(float(venue_perp_qty))) <= max(
                POSITION_DUST, 1e-6 * max(abs(journal_qty), 1.0))
            add(3, "book_position", "Was the book's signed position "
                "independently reconstructable?",
                "JOURNAL_MATCHES_VENUE" if same else "JOURNAL_DISAGREES_WITH_VENUE",
                journal_signed_perp_qty=journal_qty,
                venue_perp_size=float(venue_perp_qty))

    if statement is None:
        add(4, "funding_attribution", "Was funding attribution complete for "
            "the lifecycle?", "FUNDING_UNKNOWN_NO_JOURNAL")
    else:
        lcs = statement.get("funding_lifecycles", [])
        if not lcs:
            ans = "NO_POSITION_LIFECYCLES"
        elif statement.get("funding_confirmed"):
            ans = "RECONCILED_COMPLETE"
        else:
            ans = "PROVISIONAL"
        add(4, "funding_attribution", "Was funding attribution complete for "
            "the lifecycle?", ans, lifecycles=len(lcs),
            reconciled=sum(1 for l in lcs if l.get("reconciled")),
            reasons=_lifecycle_reasons(statement),
            venue_rows_unattributed=statement.get("funding_unattributed", 0),
            funding_unread_markers=statement.get("funding_unread", 0),
            engine_estimate_entries=statement.get("funding_estimated_entries", 0))

    if statement is None:
        add(5, "fees", "Were all required fee components confirmed?",
            "FEES_UNKNOWN_NO_JOURNAL")
    elif not statement.get("fills"):
        add(5, "fees", "Were all required fee components confirmed?",
            "NO_FILLS_TO_CONFIRM")
    else:
        add(5, "fees", "Were all required fee components confirmed?",
            "FEES_CONFIRMED" if statement.get("fees_confirmed")
            else "FEES_UNCONFIRMED", unknown=statement.get("fees_unknown", 0),
            unresolved=statement.get("fees_unresolved", 0),
            unlabelled=statement.get("fees_unlabelled", 0))

    if economics is None:
        add(6, "entry_evaluator", "Did the production entry evaluator permit "
            "the entry on the observed inputs?", "NOT_EVALUATED")
    else:
        state = str(economics.get("state", "UNKNOWN"))
        add(6, "entry_evaluator", "Did the production entry evaluator permit "
            "the entry on the observed inputs?", state,
            reason=economics.get("reason", ""))

    add(7, "order_authorisation", "Did the order-authorisation gate permit "
        "orders?", "NOT_CHECKED" if order_gate is None else
        ("GATE_ALLOWS_ORDERS" if order_gate else "GATE_REFUSES_ORDERS"))

    if statement is None:
        add(8, "fills", "Were any actual fills observed?",
            "FILLS_UNKNOWN_NO_JOURNAL")
    else:
        n = int(statement.get("fills", 0))
        add(8, "fills", "Were any actual fills observed?",
            "FILLS_BOOKED" if n else "NO_FILLS_BOOKED", fills=n)

    if statement is None:
        add(9, "net_pnl", "Is net PnL venue-observed and attributable, or "
            "merely simulated/modelled?", "NOT_AVAILABLE")
    else:
        basis = str(statement.get("net_basis", "unknown"))
        ans = net_pnl_provenance(basis)
        add(9, "net_pnl", "Is net PnL venue-observed and attributable, or "
            "merely simulated/modelled?", ans, net_basis=basis,
            replay_and_oos_results_are_never_this=True)
    return q


def net_pnl_provenance(net_basis: str) -> str:
    """One word for where a net figure came from; the ledger's basis, renamed."""
    if net_basis == "no_fills":
        return "NONE_NO_FILLS"
    if net_basis == "confirmed":
        return "VENUE_STATED_AND_ATTRIBUTED"
    return "PROVISIONAL_" + net_basis.replace("provisional_", "").upper()


def one_line(questions: List[Dict[str, Any]]) -> str:
    """A compact rendering for terminals; every answer stays separate."""
    return " | ".join(f"{x['n']}.{x['key']}={x['answer']}" for x in questions)
