# Provenance

## Vendored files

Five modules in `scripts/lib/` were copied from the `scripts/` folder of
`disciplined-trading-suite`, a Claude Agent Skill. The skill is distributed under the MIT
License, and its notice is reproduced at the end of this file. The previous version of this ledger
records the copy as made on 2026-06-22.

| File | Source SHA-256 (first 16 hex digits) | Status here |
| --- | --- | --- |
| `scripts/lib/risk_gate.py` | `89416c00327134b3` | Modified |
| `scripts/lib/trade_score.py` | `8af26f16790e1033` | Unchanged |
| `scripts/lib/session_controller.py` | `25615907d000e6c1` | Unchanged |
| `scripts/lib/reconcile_order.py` | `49cf15717842f0b8` | Unchanged |
| `scripts/lib/journal_event.py` | `72abb5b292be477b` | Unchanged |

"Unchanged" means the file is byte-identical to the skill's copy. This was checked by comparing
SHA-256 hashes on 2026-10-28. The changes to `risk_gate.py` are:

- limits come from a protected `config/risk_limits.yaml`, and limits that a caller passes in can
  only tighten them;
- a missing or unreadable limits file fails the gate closed (`risk_limits_config_unreadable`)
  instead of falling back to the built-in defaults;
- `futures` and `event_contract` were added to the disallowed instrument flags;
- `validate()` takes an optional limits path and reports the effective limits and their source.

The limits file is private. Without it the gate fails every proposal closed, unless a caller
passes another limits file through the `limits_path` argument of `validate()`.

## Not included from the skill

- `scripts/self_test.py`. The private repository keeps a modified copy that needs the private
  limits file.
- The nine reference documents in `references/`. They are instructions for an AI agent, and one
  of them describes a broker order workflow that this research-only system does not use.

## Other files

The previous ledger listed only the files above, and a search of this snapshot for license and
copyright notices found no others. [`examples/`](examples/) was written for this public
repository.

## License notice for the vendored files

The `disciplined-trading-suite` skill ships this license:

```text
MIT License

Copyright (c) 2026 Charles Dai

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
