from __future__ import annotations

from collections.abc import Sequence

from factory.config import get_config
from factory.runs import Outcome, Run


def _codes(ids: Sequence[str]) -> str:
    return ", ".join(f"`{run_id}`" for run_id in ids)


def cancel_outcome(run: Run) -> Outcome[None]:
    displaced, cleanup = run.displaced, run.cleanup
    if cleanup.unconfirmed:
        took_over = f"stopped `{displaced.trace_id}` from working here, but " if displaced else ""
        retry = f"Comment `{get_config().command_prefix} cancel` to try again."
        summary = f"{took_over}Render hasn't confirmed that {_codes(cleanup.unconfirmed)} stopped. {retry}"
        return Outcome("cancel_unconfirmed", summary, state="failed")
    if displaced:
        child = next((run_id for run_id in displaced.run_ids if run_id != displaced.trace_id), None)
        current = f" and its current run `{child}`" if child else ""
        return Outcome("cancelled", f"cancelled `{displaced.trace_id}`{current}.")
    if cleanup.confirmed:
        return Outcome("cleaned_up", f"finished stopping {_codes(cleanup.confirmed)}.")
    return Outcome("nothing_running", "nothing was running.")
