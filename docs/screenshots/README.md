# Screenshots (owner to supply)

The catalog entry (`catalog/pit-crew.yaml`) and the README point at these files. They are intentionally **not** committed as
placeholders (no binaries in the repo until real ones exist). Add them, then set the catalog URLs to the final commit SHA
(`TODO-SHA` in the entry) in the same commit/PR that pins `sha`.

| File | What | Size |
|---|---|---|
| `banner.png` | Catalog card image: "Pit Crew — a car agent that watches your car, warns you, and acts within your Hermes approval settings" over the car page | 2:1, e.g. 1200×600 |
| `pit-crew-page.png` | The Desktop page next to the chat (hero, tiles, alert with its fix button). Use the simulator; blur zone names | any |
| `approval-prompt.png` | Hermes' own approval prompt for "Unlock my car" (the policy checklist visible), taken with Hermes approvals set to `manual` (with approvals off there is no prompt) | any |
| `alert-push.png` | The proactive alert arriving in a chat app (Matrix, Telegram, Discord or any other Hermes delivery target), then "lock it" → "Locked — confirmed" | any |

Capture on the simulator in practice mode or after `/car live 1`. No VIN, HA URL, or named home zones on screen.
