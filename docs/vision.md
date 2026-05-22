---
tags: [vision, north-star]
status: stable
depends_on: []
---

# Tiri — vision

## In this system

**Linked from:** [[README]]
**Links to:** *(none — this document is the why behind everything else)*
**Layer:** north star

---

## What this is

This document explains why this system exists, what it is trying to become, and where its responsibilities end. It is not a requirements document — those are in the component docs. It is the frame that makes individual design decisions legible. When two technically valid choices conflict, this document is the tiebreaker.

Read this before reading anything else.

---

## Where Tiri starts

Databricks Genie is a well-executed natural language interface to structured data. It does what it is designed to do: take a question, generate SQL, and return a result. That capability is genuinely valuable and forms the foundation this system builds on.

Tiri addresses the scenarios where that foundation needs to extend further — not retrieval questions, but reasoning questions.

The distinction matters. Retrieval questions and reasoning questions look similar on the surface but require fundamentally different responses. "What was revenue last quarter?" is a retrieval question. One SQL query answers it. "How is the team performing against targets, and where is the biggest gap?" requires identifying the right tables, understanding what "targets" means in this context, running several queries, and synthesizing a coherent answer from the results. No single SQL query does that work.

Genie is designed for the first class of question and handles it well. Tiri is built for the second class of question — one that plans, retrieves, synthesizes, and tells the user honestly what it cannot determine. In that sense, Tiri is a form of validation: if it succeeds, it demonstrates that the Databricks platform — Genie's architecture, Unity Catalog, Model Serving, Vector Search — is a sound foundation for building serious data reasoning systems. Tiri is designed for the scenarios where Genie's built-in behavior does not meet integration requirements: multi-query reasoning, BYO LLM, per-user credential enforcement, MCP composability, or explicit uncertainty for high-stakes audiences.

---

## What Tiri is

A data reasoning environment. Not a better SQL interface.

The distinction matters. A SQL interface takes a question and returns a query result. A data reasoning environment takes a question, figures out what data is needed to answer it, retrieves that data, reasons about what it means, and returns an answer with evidence — including an honest account of what the data does and does not support.

The SQL is implementation detail. The answer is the product.

Concretely: when a user asks "why did churn increase last quarter," Tiri does not return a single query result and stop. It forms a reasoning plan, runs multiple queries across relevant tables, identifies the dominant pattern in the results, and returns a synthesized answer with the supporting data visible. It also tells the user what it cannot determine from the data alone.

This is the behavior of a competent junior analyst. Not a search engine. Not a dashboard. A reasoning agent that happens to use SQL as its primary tool.

---

## The user

Tiri will be used by people spanning a very wide range: congressional staffers preparing testimony, CEOs and CTOs making strategic decisions, managers reporting on team or operational performance, and analysts doing exploratory work. Some will be comfortable with data. Many will not. A few will be actively looking for ways the answer could be wrong.

This diversity has one unifying implication: **Tiri must earn trust, not assume it.**

A system that sounds confident is dangerous to this audience. A staffer who presents a data-generated claim in a hearing without understanding its basis is a liability — for them and for their organization. The same is true for an executive making a capital allocation decision on the basis of an answer they didn't scrutinize.

Trust is earned through transparency, not personality. This system should show its reasoning. It should quantify uncertainty where it can. It should express the limits of what the data supports. It should never produce a fluent, confident-sounding answer that papers over a genuine gap.

**Correctness is the primary value.** Tiri should be clear, precise, and honest. A system that informs decisions with real consequences — testimony, capital allocation, operational changes — earns trust by being right and being transparent about its limits, not by being engaging.

---

## What a successful interaction looks like

A manager asks: *"How is the team performing against targets this quarter?"*

The system does not return a table of numbers. It:

1. Identifies the relevant tables: performance metrics, target definitions, team roster
2. Recognizes that "this quarter" requires knowing the current date and the fiscal calendar definition
3. Runs the necessary queries
4. Synthesizes a response: "Through [date], the team has completed X of Y targets (Z%). Three targets are on track, two are behind. The largest gap is in [area], which is tracking at W% of target." 
5. Shows the supporting data — the actual query results — so the user can verify
6. States what it cannot determine: "Whether this represents an improvement over prior quarters requires historical target data, which is not currently in scope for this room."

The user walks away with an answer they can act on and defend. They know what the system looked at. They know what it did not look at. They can hand this to their own manager or to a committee and point to the evidence.

That is the bar. Not impressive. Not delightful. **Defensible.**

---

## Where Tiri's responsibility ends

This is the honest hard question, and it deserves a direct answer.

**Tiri is responsible for:**
- Accurately retrieving what the data says
- Synthesizing across multiple queries to form a coherent answer
- Quantifying confidence where possible ("3 of 4 data sources agree that...")
- Naming what it cannot determine from the available data
- Showing the evidence behind every claim it makes
- Saying "I don't know" or "the data is insufficient" when that is the true answer

**Tiri is not responsible for:**
- Causal claims ("X caused Y") from observational data
- Predictions or forecasts, unless derived from a model already present in the data
- Recommendations or judgments ("you should do X")
- Claims about data it does not have access to
- Reconciling conflicting data sources — it should surface the conflict, not resolve it

**The boundary in plain language:** Tiri is a witness, not an analyst. A good witness tells you precisely and completely what they observed. They do not speculate about causes. They do not recommend verdicts. They point to the evidence and let the decision-maker decide.

This line is drawn here for a specific reason. Causal inference from observational data is genuinely hard. Organizations with dedicated data science teams get it wrong regularly. A system that attempts causal claims without explicit statistical methodology will eventually be wrong in a high-stakes context. The cost of that error — a policy decision made on a false causal claim, a congressional testimony challenged on its evidence basis, a firing decision later revealed to be based on a confounded metric — is too high.

The right response to "why did churn increase?" is: *"The data shows that churn increased 12% in Q3. It also shows that average contract value increased 18% in the same period and that the affected customers were disproportionately from the SMB segment. Whether the contract value change caused the churn, or both were caused by something else, cannot be determined from this data alone. Here are the queries and results that support these observations."*

That answer is more valuable than a confident causal claim. It gives the decision-maker what they need to ask the next question — or to know they need a data scientist.

**Hypothesis mode — the planned exception**

Tiri does not generate causal claims. It may, in rooms where hypothesis mode is explicitly enabled, generate *hypotheses* — provisional, uncertain statements about what patterns in the data are consistent with a potential explanation. A hypothesis is not a conclusion. It is a candidate explanation that the data does not contradict, offered for human evaluation.

Every hypothesis Tiri generates must include:
- What data patterns are consistent with it
- What data patterns cut against it
- Whether it is testable with data available in this room
- If testable: what analysis would confirm or refute it

Hypothesis mode is off by default. It must be explicitly enabled by the room author, who accepts responsibility for the audience and context. A room serving congressional staffers should not have hypothesis mode enabled. A room serving data scientists exploring root causes might.

The distinction that must always be maintained, even in hypothesis mode: *"The data is consistent with X"* is not the same as *"X caused Y."* Tiri never says the second. It may, in hypothesis mode, say the first — with the evidence visible, the uncertainty explicit, and the path to verification clear. See [[extensions]] EXT-11.

---

## The north star: a reliable junior analyst

If Tiri works as intended, a non-technical user should be able to ask a business question in plain English, receive an answer they can defend in a meeting, understand what the answer is based on, and know where the answer's limits are — without writing a single line of SQL, without knowing which tables exist, and without needing a data team to translate.

Tiri should feel like a junior analyst who:
- Knows the data well
- Does the retrieval work without being asked
- Synthesizes results into a coherent answer
- Shows their work
- Says "I'm not sure" when they're not sure
- Never bluffs
- In rooms where hypothesis mode is enabled: offers candidate explanations clearly marked as hypotheses, shows what supports and contradicts each, and points toward how each could be verified

It should not feel like a search engine, a chatbot, or a dashboard. Those are different tools for different jobs.

---

## What this means for the architecture

Every significant design decision in the component docs should be traceable back to this vision. Some direct implications:

**Dynamic table selection over fixed room scope** — because the right tables for a question depend on the question, not on what an admin pre-configured. See [[agents]], [[knowledge_store]].

**Multi-query reasoning over single SQL generation** — because most business questions require more than one query. The pipeline is plan → retrieve → synthesize, not question → SQL → result. See [[room_engine]], [[agents]].

**Correctness gates before execution** — because a wrong answer delivered confidently is worse than no answer. SQL is validated before execution. Results are checked for plausibility. See [[agents]], [[room_engine]].

**Explicit uncertainty in responses** — because the audience includes people who will act on these answers in high-stakes contexts. Confidence without basis is a liability. See [[agents]].

**MCP as the integration model** — because the data a user needs to answer a business question rarely lives in one place. SQL covers structured data. MCP tools cover documents, external APIs, and other systems. Tiri should reach across both. See [[extensions]].

**Per-user credential execution** — because the system serves users with different data access rights. An answer that reveals PII to someone who shouldn't see it is a governance failure, regardless of how accurate it is. See [[extensions]].

**Multi-model routing** — because correctness and cost are both real constraints. Using the right model for each subtask is not premature optimization — it is how you make a reasoning-heavy pipeline economically viable at scale. See [[extensions]].

**Hypothesis mode as a controlled extension** — because "why" questions are real and valuable, but require a carefully bounded design to avoid the confident-wrong-answer failure mode. Hypothesis mode is opt-in at the room level, produces explicitly provisional outputs, and always shows what data supports and contradicts each hypothesis. See [[extensions]] EXT-11.

**Three operating modes** — Tiri operates in chat mode today (request-response, single-pass pipeline). EXT-1 adds reasoning agent mode (internal multi-step loop, single user turn). EXT-4 and EXT-5 add composability — Tiri as a callable tool in a larger agent graph via MCP, and Tiri as a consumer of external MCP tools. These are distinct and additive. Chat mode is not replaced by reasoning mode; reasoning mode is not replaced by MCP. Each extends what came before.

---

## What Tiri is not trying to be

A replacement for data analysts. Tiri handles the retrieval and synthesis that consumes most of an analyst's time on routine questions. It frees analysts to work on the questions that actually require their judgment: causal analysis, experimental design, model building, strategic interpretation.

A general-purpose AI assistant. This system is scoped to data reasoning. It should not write emails, summarize documents unrelated to the data, or answer questions outside its domain. Scope discipline is part of what makes it trustworthy.

A black box. Every answer should come with the evidence that produced it. Every claim should be traceable to a query result. The user should always be able to ask "show me how you got that" and receive a complete answer.
