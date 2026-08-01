# Market research — verification pass

Re-checked PRD §2's claims against current sources (Aug 1, 2026) so the Devpost writeup
and the pitch rest on numbers we can defend if a judge asks. **The market figures held up
exactly. Two claims need rewording** — the SubT attrition framing (§3) and how we
characterize Open-RMF's roadmap (§2). Details below.

## 1. Market size

| Claim in §2 | Status | Source |
|---|---|---|
| AMR/AGV fleet management software: ~$1.58B (2025) → $5.23B (2032), 18.7% CAGR | **Confirmed, exact** | MarketsandMarkets |
| Search-and-rescue robotics: $35.3B (2025) → $70.3B (2030), 14.8% CAGR | **Confirmed** — Mordor now publishes $35.29B → $70.33B at 14.79% | Mordor Intelligence |

Two figures worth adding to the pitch, both from the same MarketsandMarkets release:

- **AMRs are the fastest-growing segment at 20.5% CAGR**, outpacing the 18.7% overall —
  the robot population Colony coordinates is growing faster than the software category.
- **Multi-vendor fleet platforms are the fastest-growing *platform type*, at 20.9% CAGR.**
  This is the single most useful number we found — the heterogeneous-fleet case is exactly
  what Colony is built for. Two precision notes, because the source slices the market two
  different ways: 20.9% is the highest CAGR **among platform types**, while the 20.5% AMR
  figure is the highest **among fleet types**; don't merge them into "fastest-growing of
  all." And say we are aligned with the fastest-growing platform type — not that
  single-vendor fleet managers "structurally cannot" serve it, which is a claim about
  competitor architecture the market data does not support.

Caveat to keep us honest: SAR robotics estimates vary widely between analysts *and* use
different base years — Mordor puts 2025 at $35.29B, while other firms report figures in the
low-to-mid $20B range for 2024–2025 depending on whether drones and rescue equipment are in
scope. Cite it as "Mordor Intelligence puts the market at $35.3B in 2025, with other
analysts lower depending on scope" rather than quoting a range as if it were one base year.
Our sources list below supports the Mordor figure; it does not establish a specific lower
bound, so don't quote one.

## 2. The gap is real — and the ask is on the record

§2.3's claim that inter-robot task dependency is the missing layer is **supported by
Open-RMF's own community**, and the specific thread is worth quoting in the pitch — but
quote it accurately, because a robotics judge may know it.

A user asked the Open Robotics Discourse (thread opened July 7, 2025; replies through
September 2025) whether RMF supports genuine multi-robot cooperation rather than multi-fleet
scheduling. Their example is almost exactly our demo, in a warehouse register: **Robot A
carries an item to a rendezvous point, Robot B takes it from there and completes the
delivery.**

What the thread actually establishes, in full:

- The capability is **not supported in the current version** of Open-RMF.
- A maintainer response quoted from **2023** called it *"something we are interested to work
  on as well but not high enough on the priority list."* That quote is three years old —
  don't present it as today's position.
- **Work is underway.** A maintainer confirms `bevy_impulse` is being built as a toolkit for
  defining and executing workflows "particularly well suited for managing multi-agent
  behavior," with a stated roadmap toward a multi-agent task management system.

So the honest framing is *not* "nobody is building this." It is that the capability doesn't
exist in shipping Open-RMF today, its users are asking for it, and the ecosystem's answer is
a next-generation rewrite still in progress. That is still a strong position for us — and it
is much safer than a claim a maintainer could publicly correct.

**Suggested video line.** "Users of the leading open-source fleet framework are asking for
robot-to-robot task handoffs on its own forum. It isn't in the shipping version — the
answer is a next-generation workflow engine still being built. We made that layer work today,
on top of a distributed database."

## 3. DARPA SubT — partially confirmed, needs rewording

§2.3 cites SubT for "operator overload and robot attrition as the top field problems."

**Operator overload: confirmed.** The Final Event rules allowed exactly one team member —
DARPA's designated **Human Supervisor** — to interface with the robots on the course, and
only that person could use wireless communications with the systems during a run. The
published lessons-learned work is explicitly about reducing operator workload:
sliding-mode autonomy, adaptive interfaces, context-aware action suggestions. The
constraint was structural, not incidental. (Attribute the rule itself to **IEEE Spectrum's
reporting on the Final Event** — that is the source below that actually states it. DARPA's
challenge page describes the competition but does not spell out the supervisor rule, and
the CERBERUS paper describes the consequences rather than the rule. If we want a primary
citation, someone should pull the Final Event rules PDF before submission.)

**Robot attrition: softer than we state it.** The literature emphasizes resilience,
modularity and degraded-condition autonomy (comms-denied, GNSS-denied, perceptually
degraded) rather than naming attrition as a top-two problem. Our heartbeat-and-reassign
mechanic is still well motivated — a robot going silent is the same failure mode — but
we should say **"operator overload and robot resilience under degraded conditions"** rather
than claiming attrition was formally ranked #2. It's a small rewording that removes a claim
a robotics judge could challenge.

## 4. What this changes

1. **Add the 20.9% multi-vendor-platform CAGR to the pitch.** It is the tightest
   quantitative fit to our positioning and we weren't using it. Frame it as alignment with
   the fastest-growing segment, not as a structural claim about competitors.
2. **Lead the gap argument with the Open-RMF thread, not the market size.** A user asking
   for our exact feature is more persuasive than a TAM chart — but include the
   `bevy_impulse` caveat so the claim survives a knowledgeable judge.
3. **Reword the SubT claim** in §2.3: operator overload and resilience under degraded
   conditions, not "attrition ranked #2."
4. **Don't quote a SAR market range as one base year.** Name Mordor's $35.3B (2025) as our
   point estimate and note that other analysts are lower depending on scope.

Nothing here challenges the product thesis. The gap analysis in §2.3 — that the stack stops
at telemetry, deconfliction and one-task-to-one-robot dispatch — held up against every
source checked here, with the caveat that Open-RMF has next-generation work in flight
toward multi-agent task management. Worth re-checking before submission on Aug 18.

## Sources

- [Fleet Management Software Market for AGV & AMR — MarketsandMarkets](https://www.marketsandmarkets.com/Market-Reports/amr-agv-fleet-management-software-market-43844234.html)
- [Fleet Management Software Industry worth $5.23 billion by 2032 — MarketsandMarkets press release](https://www.marketsandmarkets.com/PressReleases/amr-agv-fleet-management-software.asp)
- [Search and Rescue Robots Market Size — Mordor Intelligence](https://www.mordorintelligence.com/industry-reports/search-and-rescue-robots-market)
- [SOTA for Multi Robot Cooperation in RMF — Open Robotics Discourse](https://discourse.openrobotics.org/t/sota-for-multi-robot-cooperation-in-rmf/45075)
- [Tasks in RMF — Programming Multiple Robots with ROS 2](https://osrf.github.io/ros2multirobotbook/task.html)
- [The DARPA SubT Finals human supervisor role — IEEE Spectrum](https://spectrum.ieee.org/darpa-subterranean-challenge-operator) *(states the one-supervisor and wireless-comms rule)*
- [DARPA Subterranean Challenge — official challenge page](https://www.darpa.mil/research/challenges/subterranean) *(challenge background only; does not state the supervisor rule)*
- [Modular, Resilient, and Scalable System Design Approaches — Lessons learned after DARPA SubT (arXiv:2404.17759)](https://arxiv.org/abs/2404.17759)
- [Into the Robotic Depths: Analysis and Insights from the DARPA Subterranean Challenge — Annual Reviews](https://www.annualreviews.org/content/journals/10.1146/annurev-control-062722-100728)
- [Team CERBERUS Wins the DARPA Subterranean Challenge: Technical Overview and Lessons Learned](https://intelligent-earth.ox.ac.uk/publication/2039031/ora-hyrax)
