# Dashboard redesign: design plan (agreed 2026-11-24)

These choices were agreed with the user on 2026-11-24. The plan follows Anthropic's `frontend-design` skill (github.com/anthropics/skills, skills/frontend-design): plan first, spend boldness in one place, and avoid templated defaults.

## Brief
- **Who uses it:** one person running a swing-trading bot, on a PC and on an iPhone over Tailscale.
- **Main job:** answer three questions in five seconds. Is my money protected? What did the bot do? Does it need me?
- **Brief from the user:** "cleaner and minimalistic". Dark only.

## Tokens
| Token | Hex | Use |
|---|---|---|
| `--page` | `#17191C` | Page background. A neutral graphite, not the old blue-tinted near-black |
| `--ink` | `#E6E8EB` | Primary text and figures |
| `--ink-2` | `#9AA3AD` | Secondary text, column headers, timestamps |
| `--rule` | `#2A2E33` | Hairlines between table rows and section dividers |
| `--raise` | `#1E2125` | The only raised surface: menus, dialogs, the "Needs you" items |
| `--gain` | `#3FB27F` | Positive P&L numbers only |
| `--loss` | `#E5675C` | Negative P&L numbers and destructive actions (Kill, Flatten) |
| `--live` | `#E5675C` | A 2px frame around the whole page when the account is live. Paper shows no frame |

Check every text colour for WCAG AA contrast against `--page` and `--raise`.

## Type
- **Font:** IBM Plex Sans, weights 400, 500 and 600, via `next/font` and self-hosted in the export. Fallback: system-ui.
- **Numbers:** tabular figures everywhere (`font-variant-numeric: tabular-nums`). No monospace for labels or figures.
- **Scale** (1.2 ratio): 12, 14 (body), 17, 20, 24 (status sentence).
- **Case:** sentence case. No all-caps labels and no tracked-out eyebrows.
- **Line length:** 80 characters or less for prose.

## Layout: Today view, chart first
```
Trader   Today  Research  Learning  Settings        [Auto v] [Pause] [Kill] [Flatten]

Auto-trading on paper. Nothing needs you.                  <- the one bold element
Day -$78.67   Since start +0.4%   vs SPY +1.1%

Equity vs SPY  (one quiet line chart, full width of the main column)

Positions                                        | Needs you
Symbol  Qty  Cost    Last    P&L     Stop        | (empty state: "Nothing needs you.")
...                                              | Activity
                                                 | 11:01 ADI stop filled at 308.24
```
- **Phone:** one column, in this order: status sentence, Needs you, chart, positions, activity.
- **Alignment:** everything is left-aligned. Numbers are right-aligned in table columns.
- **Status sentence:** generated from the status and portfolio data in plain language. Examples:
  - "Auto-trading on paper. 11 positions, all with stops. Nothing needs you."
  - "Approve mode. 2 trades need your decision."
  - "Kill switch on: no new entries. Exits still run."
  - "Live account."

## Principles
1. **No cards and no shadows.** Space and hairline rules carry structure. Only interactive controls get a radius (6px).
2. **Colour has meaning.** Green and red appear only on gains and losses. Red also marks destructive actions and live mode.
3. **Safety controls are always visible.** Kill, flatten, pause and mode stay in the top bar, always reachable, with their existing confirmations.
4. **Plain words.** Name things the way a user would: "Needs you", not "Action queue"; "Stop" and "Protected", not "sl rows".
5. **Motion only answers an action.** Opening a dialog or confirming something. No entrance animations.
6. **Quality floor:**
   - responsive to 360px wide with no horizontal page scroll (tables scroll inside their own container);
   - visible keyboard focus;
   - `prefers-reduced-motion` respected.

## Pass 1 scope
- The tokens, type and base styles.
- The top bar.
- The Today view, which replaces the stack of command bar, metric grid, performance and gate summary.

The Research, Learning and Settings views inherit the new tokens in pass 1. They get their own layout in pass 2, after the user has reviewed pass 1.
