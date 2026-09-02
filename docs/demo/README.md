# Pacioli demo - "The floor"

A real, recordable demo of the credential floor and the receipt chain. The driver
(`scripts/demo/the_floor.py`) calls Pacioli's own shipped code and prints its actual decisions and
real HMAC seals. Nothing is staged. That honesty *is* the pitch.

## Run it

```bash
# Local (self-contained - needs only `pip install pacioli pacioli-guard`). You drive the tempo:
python scripts/demo/the_floor.py            # press Enter between beats
python scripts/demo/the_floor.py --auto 3   # hands-free, ~3s/beat, humanised (asciinema)
```

## The story (~40s)

The hook answers the charge directly: an api key in Frappe maps to a user, and holding the key gets
you everything that user can do - no scope on the key, no method limit, no doctype limit. Pacioli
puts a floor under it.

| Beat | On screen | What it proves | Code that decides |
|---|---|---|---|
| **1 · the floor decides** | same submit call, two answers on one key: `ALLOW` unscoped → `REFUSE` floor-scoped; then a granted read on that scoped seat: `ALLOW` | authorization the credential can't talk past, deny-by-default | `pacioli_guard.scope.is_permitted` / `classify` |
| **2 · the record** | three receipts seal into a keyed chain, `verify` passes | every governed act gets a sealed, chained receipt | `pacioli.prove.append` / `verify_chain` |
| **3 · rewrite the past** | edit one field → `REFUSE`, named at the exact receipt | the seal is over the contents; tamper is caught | `pacioli.prove.verify_chain` |
| **4 · erase the tail** | drop the last receipt → a naive check passes, the off-box head catches the wipe | a book that doesn't balance confesses | `verify_chain(expected_head=...)` |

Every decision on screen is made by the same functions the guard and broker ship. The `is_permitted`
calls, the `classify` resolution, the HMAC seals and the linkage check are the real code, not a mock.

## `--live` - the four receipts against a real ERPNext

`--live` prints the preconditions for the four governed receipts - the floor refuses, the borrowed
over-broad key, the governed write through PLAN → CONSENT → PROVE, and the bypass refused - run
against a real ERPNext bench you configure and drive yourself. A faithful run needs, on that bench:
`pacioli-guard` current enough to enforce consent, a floor-scoped seat plus a broker seat with
consent required, and permission to create and submit a synthetic invoice. The mode runs nothing on
its own; the live path mutates real data, so you drive it.

## Recording

- `asciinema rec --overwrite --cols 96 --rows 46 -c "python scripts/demo/the_floor.py --auto 2.2" out.cast`
- Render to SVG: `npx svg-term-cli --in out.cast --out the-floor.svg --window --width 96 --height 46`
- **Read every cast back before calling it good** (parse the `.cast`, join the `"o"` frames). A render
  nobody looked at is not a receipt.
- **Never fake output, never edit a cast.** A real refusal (a `REFUSE` we predicted) is not a failure
  - that is the product working. If a script breaks, fix it off-camera and re-record cleanly.
- **Settle the captions before recording.** A caption fix after recording means a full re-record, so
  read them cold first.
