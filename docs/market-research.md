# Market research — verification pass

Re-checked PRD §2's claims against current sources (Aug 1, 2026) so the Devpost writeup
and the pitch rest on numbers we can defend if a judge asks. **Every headline claim in §2
held up.** Details and caveats below.

## 1. Market size

| Claim in §2 | Status | Source |
|---|---|---|
| AMR/AGV fleet management software: ~$1.58B (2025) → $5.23B (2032), 18.7% CAGR | **Confirmed, exact** | MarketsandMarkets |
| Search-and-rescue robotics: $35.3B (2025) → $70.3B (2030), 14.8% CAGR | **Confirmed** — Mordor now publishes $35.29B → $70.33B at 14.79% | Mordor Intelligence |

Two figures worth adding to the pitch, both from the same MarketsandMarkets release:

- **AMRs are the fastest-growing segment at 20.5% CAGR**, outpacing the 18.7% overall —
  the robot population Colony coordinates is growing faster than the software category.
- **Multi-vendor fleet platforms grow fastest of all, at 20.9% CAGR.** This is the single
  most useful number we found: the fastest-growing slice of the market is precisely the
  heterogeneous-fleet case, which is what Colony is built for and what single-vendor fleet
  managers structurally cannot serve.

Caveat to keep us honest: the SAR robotics figure has wide analyst spread ($22–35B for the
same base year, depending on whether the definition includes drones and equipment). Cite it
as "analyst estimates cluster in the $25–35B range, Mordor at $35.3B" rather than as a
single precise number — a judge who has seen a different report will otherwise catch us.

## 2. The gap is real — and the ask is on the record

§2.3's claim that inter-robot task dependency is the missing layer is **directly supported
by Open-RMF's own community**, and the specific thread is worth quoting in the pitch.

A user asked the Open Robotics Discourse (July 2025) whether RMF supports genuine
multi-robot cooperation rather than multi-fleet scheduling. Their example is almost exactly
our demo, in a warehouse register: **Robot A carries an item to a rendezvous point, Robot B
takes it from there and completes the delivery.** The answer from the thread is that RMF
**does not support constraints between tasks**, which is what makes handoffs hard to build
on it today.

That is the strongest evidence we have, and it is better than a market number: the leading
open-source fleet framework's users are asking for the exact primitive Colony implements
(`depends_on` + transactional claiming), and the maintainers' answer is that it isn't there.

**Use this in the video.** "The leading open-source fleet framework's users are asking for
robot-to-robot task handoffs on its own forum, and the answer is that task constraints
aren't supported. That's the layer we built."

## 3. DARPA SubT — partially confirmed, needs rewording

§2.3 cites SubT for "operator overload and robot attrition as the top field problems."

**Operator overload: confirmed.** The competition format allowed only **one human
supervisor** to command all robots and access their data, and the published lessons-learned
work is explicitly about reducing operator workload — sliding-mode autonomy, adaptive
interfaces, context-aware action suggestions. The constraint was structural, not incidental.

**Robot attrition: softer than we state it.** The literature emphasizes resilience,
modularity and degraded-condition autonomy (comms-denied, GNSS-denied, perceptually
degraded) rather than naming attrition as a top-two problem. Our heartbeat-and-reassign
mechanic is still well motivated — a robot going silent is the same failure mode — but
we should say **"operator overload and robot resilience under degraded conditions"** rather
than claiming attrition was formally ranked #2. It's a small rewording that removes a claim
a robotics judge could challenge.

## 4. What this changes

1. **Add the 20.9% multi-vendor-platform CAGR to the pitch.** It is the tightest
   quantitative fit to our positioning and we weren't using it.
2. **Lead the gap argument with the Open-RMF thread, not the market size.** A user asking
   for our exact feature and being told it doesn't exist is more persuasive to judges than
   a TAM chart.
3. **Reword the SubT claim** in §2.3 as noted above.
4. **Present SAR market size as a range.** Cite the spread, name Mordor as our point
   estimate.

Nothing here challenges the product thesis. The gap analysis in §2.3 — that the stack stops
at telemetry, deconfliction and one-task-to-one-robot dispatch — survived every check.

## Sources

- [Fleet Management Software Market for AGV & AMR — MarketsandMarkets](https://www.marketsandmarkets.com/Market-Reports/amr-agv-fleet-management-software-market-43844234.html)
- [Fleet Management Software Industry worth $5.23 billion by 2032 — MarketsandMarkets press release](https://www.marketsandmarkets.com/PressReleases/amr-agv-fleet-management-software.asp)
- [Search and Rescue Robots Market Size — Mordor Intelligence](https://www.mordorintelligence.com/industry-reports/search-and-rescue-robots-market)
- [SOTA for Multi Robot Cooperation in RMF — Open Robotics Discourse](https://discourse.openrobotics.org/t/sota-for-multi-robot-cooperation-in-rmf/45075)
- [Tasks in RMF — Programming Multiple Robots with ROS 2](https://osrf.github.io/ros2multirobotbook/task.html)
- [Modular, Resilient, and Scalable System Design Approaches — Lessons learned after DARPA SubT (arXiv:2404.17759)](https://arxiv.org/abs/2404.17759)
- [Into the Robotic Depths: Analysis and Insights from the DARPA Subterranean Challenge — Annual Reviews](https://www.annualreviews.org/content/journals/10.1146/annurev-control-062722-100728)
- [Team CERBERUS Wins the DARPA Subterranean Challenge: Technical Overview and Lessons Learned](https://intelligent-earth.ox.ac.uk/publication/2039031/ora-hyrax)
