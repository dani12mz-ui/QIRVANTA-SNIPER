# QIRVANTA SNIPER PREMIUM V2

Base: `c63db3152ae26b1af4001a4aa35f1eab963d53d8` · Branch: `feature/qirvanta-sniper-premium-v2`.

- [Premium V2 strategy](../unified/experimental/QIRVANTA_SNIPER_PREMIUM_V2.pine): presentation update over the pinned V1 engine.
- [V2 Intelligence indicator](../unified/experimental/QIRVANTA_SNIPER_V2_INTELLIGENCE.pine): separate, experimental observations with no order or alert dispatch.
- [Signal and cost study](PREMIUM_V2_QUANT.md) · [Management and risk audit](PREMIUM_V2_MANAGEMENT.md) · [Native acceptance and captures](PREMIUM_V2_NATIVE.md).
- [Immutable V1 native S0/S5 evidence](PREMIUM_NATIVE_VERIFICATION.md): prior baseline, independently preserved.

## Presentation and causality

Desktop defaults to the upper-right dark card. Compact reduces label width and row density, Auto uses compact density on M5 and shorter charts, and Mobile pins the smaller card to the lower right. Pine cannot detect the viewport. Use `Layout preset`, `Позиция на таблото` and `Букви в таблото (pt)` in Inputs. The dark TradingView chart theme is a chart setting.

V2 defaults to **1,200 calculated bars** to run on the tested TradingView account. This changes the calculation window and warmup, so the default results are not directly comparable to V1's full history. Set **Inputs → Calculation → Calculated bars → 0** for all available bars when the account can run that range. Match the actual data window before comparing execution. No trading decision rule was changed.

The card distinguishes confirmed candidates, submitted plans, observed emulator fills, partial exits, closed campaigns and unfilled plans. A chart BUY/SELL badge means a plan was submitted at the confirmed bar, never that a broker filled it. Plan and fill prices have separate rows. Chart lines identify their source as Plan or Fill; the dotted initial stop stays frozen and the current stop starts at its recorded update. A check beside a target requires a native exit tagged TP1/2/3. Only the last exit part supplies the displayed close reason. Net P&L comes from the native campaign event; incomplete ledgers suppress it. Net R uses actual initial risk and can be undefined even when cash P&L exists. Gross R:R to TP3 is distinct from realized R, and a BE stop price does not guarantee net break even.

V2 stores bounded trade snapshots as events arrive and draws retained geometry only at the dataset edge. The historical submission badges remain on their own bars. Guides can displace the text while the lines stay at the original prices. Prices are shown at the instrument tick precision. Within a matched data window, the main strategy continues to use the original confirmed-bar clock, admission, fills, levels, risk handling and alerts. The builder verifies both the pinned V1 and Champion SHA-256 values, verifies the engine and alert bytes, and rejects new data requests or order calls in the renderer.

The renderer's maximum retained inventory is 84 lines, 26 boxes, 126 labels and one 2 × 18 table; it contributes four plots and no extra data requests. Those are source bounds. TradingView's compiler does not expose its compiled token count. The checked-in script was compiled as a whole in the test profile.

The standalone indicator discloses pivots at confirmation, uses strictly later closes for sweep-to-shift confirmation, and displays H1/H4 values from completed HTF bars using offset expressions. Classification labels count explicit support conditions rather than reporting win probabilities. Observations and optional Pine logs are separate from S0/S5. The quant study's pre-registered screens are evaluated on an archived fill tape, not an independent backtest of this indicator. No experimental result replaces S5.

## Reproduce

From the repo root, with Python 3:

```powershell
python -X utf8 -B unified/tools/build_sniper_premium_v2.py --check
$env:PYTHONPATH = (Get-Location).Path + ';' + (Join-Path (Get-Location).Path 'unified')
python -X utf8 -B -m unittest discover -s unified/tests
python -X utf8 -B unified/tools/premium_v2_native_pair.py prepare --cutoff 2026-11-10T14:30:00Z --output ../premium-v2-observers
```

Run observers only in private test drafts. The cutoff fixes the audit endpoint, not the first bar loaded by the chart. Use the archived V1 raw logs only if all dataset fingerprints match; otherwise collect fresh V1 and V2 logs in the same session. The `compare` subcommand checks admitted plans, native positions and exits, cash reconciliation, profit factor and drawdown. Observer parity is separate from compiling and running the full chart script.
