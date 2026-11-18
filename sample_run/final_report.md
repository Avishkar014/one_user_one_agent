# One User, One Agent - Replay Report
Observation coverage: simulator follow-up runs through 16 October 2026, 00:00 IST. All reconstructed measurable goal windows ended within this observation boundary.
## Replay overview

- Randomized users: **5000**
- Decisions recorded: **2255**
- Actions published: **390**

### Decision actions

- NO_ACTION: 1865
- push: 390

## Goal outcome reconstruction

- Goal episodes reconstructed: **757**
- Succeeded: **46**
- Not succeeded: **711**
- Unverifiable: **0**

Goal outcomes are reconstructed from selected-goal decisions and simulated events. They are not goal-specific causal estimates.

## Latency

- Decision processing p50: 1.0 ms
- Decision processing p95: 6.0 ms
- Trigger-to-publication p95: 0.007 seconds
- Actions over 60 seconds: 0

## Randomized-arm intent-to-treat results

| Outcome | Agent rate | Holdout rate | Difference (pp) |
|---|---:|---:|---:|
| play_started | 25.388% | 26.900% | -1.512 |
| episode_completed | 18.225% | 20.185% | -1.960 |
| purchase_completed | 1.353% | 1.448% | -0.094 |

## Interpretation and limitations
- 'Not succeeded' means the configured success condition was not observed within the measured window; it does not establish that the user would never achieve the goal.
- ITT denominators include every randomized user, including users with no generated outcome events.
- Simulator results are synthetic and must not be represented as production performance.
- Broad engagement and purchase outcomes are not interchangeable with each user's selected goal.
- Goal episodes are reconstructed from decision history; a goal change is treated as the start of a new episode.
- Goal-specific eligibility for holdout users is not reconstructed here, so no goal-specific causal uplift is claimed.
- Approximate confidence intervals do not include simulator-model uncertainty.
- Goal conditions that cannot be verified from the available fields are reported as unverifiable rather than assumed successful.
