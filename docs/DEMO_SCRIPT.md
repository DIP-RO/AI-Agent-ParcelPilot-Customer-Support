# 5-Minute Demo Video Script

**0:00–0:45 — Architecture (over a diagram or the ARCHITECTURE.md graphic)**
"ParcelPilot's data pack is deliberately unreliable — deprecated policies,
contracts that override defaults, historical answers that are wrong. So the
architecture splits responsibilities: Claude plans and explains, but three
deterministic engines do every fee, credit and SLA calculation, with clause
citations. Access control lives in the data layer — customer tokens physically
can't fetch other accounts. And state-changing actions are two-phase: the agent
can only *prepare* a signed action; only a human clicking Confirm executes it."

**0:45–2:00 — Customer demo (login as Meera, Northstar)**
1. Ask: *"Can we cancel ORD-1001 without a cancellation fee?"*
   - Point at the tool chips: order lookup → account/contract → cancellation
     engine.
   - Answer: no fee — the agreement overrides the SOP's ₹250; note the KI-211
     caveat that the SwiftShip status may lag reality.
2. Ask the same about ORD-2001 as Vikram (LumenWorks): ₹250 fee — same
   question, different contract, different answer. This is the whole point.

**2:00–3:00 — Trust demo (stay as Vikram)**
3. Ask: *"Why does our 4,200-row CSV fail? Last time support said our plan only
   allows 3,000 rows."*
   - The old ticket said 3,000 — the system flags that historical answer as
     wrong, cites the 5,000-row product limit, matches known issue KI-208, and
     gives the split-below-3,000 workaround.
4. Ask: *"ORD-2002 pickup is hours late — do we get a credit?"* → ₹300 fixed
   from the contract (not the default ₹240 formula) — then let it prepare the
   escalation and show the **Confirm card**: "nothing executed until I click."

**3:00–4:15 — Internal demo (login as Rohit, then Anita)**
5. Open **Ops Radar**: two breached P1s (TKT-501's contractual 15-minute 24x7
   clock; TKT-505's credential exposure), while LumenWorks' business-hours P2
   correctly hasn't started — the snapshot is a Sunday. Show the KI-208 repeat
   cluster and the unactioned ₹300 credit.
6. Click "Discuss in chat" on the credit → agent verifies with the engine →
   prepares apply_service_credit → Confirm as Anita (manager approval rules:
   Rohit couldn't execute above ₹1,000).

**4:15–5:00 — Decisions & close**
"Three decisions I'd highlight: deterministic engines instead of LLM
arithmetic — money answers must be reproducible; enforcement in the tool layer
instead of the prompt — injection can't leak what the model never receives;
and confirmation as an architectural invariant — there is no tool that
executes an action. Beyond the brief, both extra problems are covered: trust
mechanisms are the core, and Ops Radar makes the same data proactive. Next
I'd build the feedback/eval loop and draft-reply mode for the ops team."
