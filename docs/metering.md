# Metering and allowances

On a server, an administrator can **meter** a connection: every model call on it is
charged **credits**, and each user has a monthly allowance of credits on it. Credits
replace the token budgets of earlier 0.3.0 release candidates. Personal mode meters
nothing (see [Personal mode](#personal-mode)).

Metering is set per connection, on the Connections page (`/admin/connections`), with the
connection's **Metering** and **Assignments** buttons. Both need the
`spark.admin.connections` permission, which the `administrator` role holds. See
[Connections](connections.md) for adding connections and choosing their models.

## Metered and unmetered connections

- A **metered** connection charges credits and stops a user's new requests on it once
  their monthly allowance is used.
- An **unmetered** connection (the default) has no limit. Its calls are still charged
  credits at its rates on their usage records, so an administrator's totals show what
  they would have cost, but nobody is ever stopped.

## How credits are worked out

Each call is charged from its four token counts, the connection's rates and the model's
tier:

```text
weighted tokens = input x input weight
                + output x output weight
                + cache read x cache read weight
                + cache write x cache write weight

credits = weighted tokens / base rate x tier multiplier
```

`input` is uncached input. The settings, on the connection's **Metering** dialog:

| Setting | Default | Meaning |
|---|---|---|
| Metered | Off | Whether allowances apply. |
| Base rate | 1,000 | Weighted tokens per credit on a standard model. |
| Higher tier multiplier | 2 | Credits multiplier for a model of the higher tier. |
| Lower tier multiplier | 0.5 | Credits multiplier for a model of the lower tier. The standard tier is always 1. |
| Input token weight | 1 | |
| Output token weight | 5 | |
| Cache read token weight | 0.1 | |
| Cache write token weight | 1.25 | |

**Reset to defaults** puts every rate back to these values. The base rate is a whole
number of at least 1, the multipliers must be positive and the weights cannot be
negative.

### Tiers

Each model on a connection is of the **lower**, **standard** (default) or **higher**
tier, set in the connection's models view. The tier lets one connection charge more for
a large model and less for a small one, at the same base rate.

### Worked example

The **Metering** dialog shows a worked example: what a sample turn of 1,500 input,
500 output, 8,000 cache read and 1,000 cache write tokens costs at each tier. With the
default rates:

| | Tokens | Weight | Weighted |
|---|---:|---:|---:|
| Input | 1,500 | 1 | 1,500 |
| Output | 500 | 5 | 2,500 |
| Cache read | 8,000 | 0.1 | 800 |
| Cache write | 1,000 | 1.25 | 1,250 |
| **Total** | | | **6,050** |

6,050 weighted tokens / 1,000 = 6.05 credits on a standard model, 3.025 on a lower one
and 12.1 on a higher one. With a base rate of 2,000 and a higher tier multiplier of 3,
the same turn costs 1.5125, 3.025 and 9.075 credits.

These figures are what `spark.usage.credits.estimate` returns (the dialog's worked
example, and the same calculation every usage record is charged with); the snippet
below was run for this guide:

```python
from spark.usage.credits import Rates, estimate

turn = {"input_tokens": 1500, "output_tokens": 500,
        "cache_read_tokens": 8000, "cache_write_tokens": 1000}
estimate(Rates(), turn).as_dict()["credits"]
# {'lower': '3.025000', 'standard': '6.050000', 'higher': '12.100000'}
estimate(Rates(base_rate_tokens=2000, higher_multiplier=3), turn).as_dict()["credits"]
# {'lower': '1.512500', 'standard': '3.025000', 'higher': '9.075000'}
```

### Precision and history

- Credits are exact to six decimal places. Each call's credits are rounded once, half
  up, and stored as whole millionths of a credit, so totals are exact.
- Screens and messages show credits to two decimal places; the CSV export carries
  all six.
- Every usage record keeps the rates and tier it was charged at. Changing a
  connection's rates or a model's tier changes what later calls cost, never what
  earlier calls cost.

## Assignments and allowances

On a server a user sees and uses only the connections **assigned** to them or to one of
their roles. A connection assigned to no one is offered to administrators only (holders
of `spark.admin.connections`), so a new connection can be tried before anyone else sees
it. **Assignments** on a connection lists its assignments and adds one:

- **Assign to** a role (`administrator`, `auditor`, `power_user`, `user`) or a named
  user.
- **Monthly allowance**: a whole number of credits. On a metered connection you must
  give a number or choose **No limit**; a blank allowance is refused. On an unmetered
  connection the allowance may be left blank; a number given is kept for when metering
  is turned on.
- **Remove** takes an assignment away.

Turning metering on for a connection whose assignments have no allowance leaves them
at **No limit** until you change them.

### Which allowance applies

For each user on each metered connection:

1. **The user's own assignment wins.** Its allowance applies, or no limit if it says
   **No limit**, whatever their roles' assignments say.
2. **Otherwise the highest of their roles' allowances applies.** If any of their roles'
   assignments says **No limit**, there is no limit.
3. Otherwise the connection is not theirs: it is hidden from them and any request on
   it is refused.

For example, a connection assigned to `user` with 100 credits and to `power_user` with
500 gives someone holding both roles 500 credits a month, and a user with their own
assignment of 50 credits gets 50, even if they hold `power_user`.

## Warnings, the stop and the monthly reset

- **The model picker** groups models by connection, shows each model's tier and, on a
  metered connection, the credits the user has left this month.
- **At 80%** of the allowance, the chat, debate and panel pages show a warning naming
  the connection, the credits used and the reset date.
- **At 100%**, a new request on that connection is refused with a message naming the
  connection, the credits used and when they reset: chat turns (on the stream and the
  send route), debate and panel turns and questions, scheduled action runs and
  knowledge graph build chunks. Each refusal is recorded in the audit trail
  (`spark.usage.refused`).
- **In-flight requests finish.** The check is made as each request starts, so a turn
  already running always completes, even if it, or turns started beside it, takes the
  user past the allowance. Their next request is refused.
- **The month is the UTC calendar month.** Every allowance resets at 00:00 UTC on the
  first of the month.

Other connections are not affected: a user at 100% on one metered connection can still
use their other connections.

## The usage view and CSV

Administrators (`spark.usage.admin`) open the usage view from **Administration > Usage and
budgets** (`/admin/usage`). For a chosen month (UTC), or all months, it shows:

- usage by user, connection and model: requests, the four token counts (input, output,
  cache read, cache write), total tokens and credits;
- totals by connection;
- each user's standing on each metered connection: used, allowance (or **No limit**)
  and the share used.

**Export CSV** (`/admin/usage/export.csv?month=YYYY-MM`, or `month=all`) downloads the
same usage, one row per month, user, connection and model, with the columns `month`,
`user`, `user_guid`, `connection`, `connection_id`, `model`, `requests`,
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`total_tokens` and `credits` (six decimal places). Opening the view and exporting are
both recorded in the audit trail.

A user sees their own standing at `/api/usage/me` (for one connection with
`?connection=<id>`, or `?model=<connection>/<model>`), which is what the pages and the
model picker read.

The ElevenLabs voice character cap is separate from credits and stays instance-wide.

## Personal mode

Personal mode has one local user and no metering or assignment: the Connections section
of Settings has no **Metering** or **Assignments** buttons, every enabled model is
offered, and nothing is ever stopped for usage. Usage is still recorded with its token
counts.

## Upgrading from token budgets

Earlier 0.3.0 release candidates had monthly token budgets per role and per user.
0.3.0 was never released, so those budgets are not converted: revision
`credit_metering` (applied by `spark migrate`) removes them. Set allowances on each
metered connection's assignments instead. Usage recorded before that revision costs no
credits.
