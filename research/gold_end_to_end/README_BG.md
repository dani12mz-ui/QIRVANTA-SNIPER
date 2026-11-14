# QIRVANTA GOLD PRO · проверени аналитични файлове

**За графиката** използвайте [QIRVANTA GOLD PRO COMPACT](../compact_dashboard/QIRVANTA_GOLD_PRO_COMPACT.pine), точния Pine v6 от `ee5f1d8`. На `OANDA:XAUUSD` със стандартни свещи поддържа M5/M15/M30. Поставете целия файл в **Pine Editor → Create new → Indicator**. В основния изглед посочното наблюдение има отделен ред от разрешението. Entry, SL и TP1–TP3 се появяват при показван валиден план. Наличният контекст, остарелият бар и историческият преглед имат отделни означения. [Настройки, паритет и нативни кадри](../compact_dashboard/QA_BG.md)

**За отделен Strategy Tester одит** използвайте [QIRVANTA GOLD PRO RC2 AUDIT](../../releases/gold-pro-rc2/QIRVANTA_GOLD_PRO_RC2_AUDIT.pine) и изберете **S5 RAW Parity**. Това е по-ранна нативно компилирана одитна контрола със собствен обхват. COMPACT и RC2 Audit имат отделни нативни сверки. Няма нов съвместен нативен паритет на изпълненията между тях в този пакет. [RC2 нативна проверка](../../releases/gold-pro-rc2/NATIVE_QA.md)

За сървърни TradingView известия създайте нов alert при смяна на скрипта или настройките, тъй като съществуващият alert използва запазено копие. Архивът на COMPACT удостоверява payload-и в Pine Logs. Изпращането от сървъра и получаването на телефон остават непроверени. [TradingView · Alerts](https://www.tradingview.com/pine-script-docs/concepts/alerts/)

## Възпроизвеждане на изследването

Необходим е Python 3.12 с достъп до IANA timezone `America/New_York` (на Windows може да е необходим пакетът `tzdata`). Командите са от корена на хранилището. `../mission` съдържа регистрираните `M5_dataset`, `ui_dataset` и `M30_dataset` папки. `../tv-final` съдържа оригиналните `tv-S5-M*.csv`. Нормализираната пълна история остава локална според манифестите на източника. Точните SHA-256, дата, брой и версия са в [произхода](market-input-provenance.json).

```powershell
$out = "$env:TEMP/qirvanta-end-to-end"
python -B -m research.gold_end_to_end.controls --data-root ../mission --native-root ../tv-final --output "$out/market_inputs.json.gz" --manifest "$out/market-input-provenance.json"
python -B -m research.gold_end_to_end.replay --input "$out/market_inputs.json.gz" --output "$out/full-replay.json.gz"
python -B -m research.gold_end_to_end.publish_evidence --replay "$out/full-replay.json.gz" --output "$out/tables"
python -B -m research.net_campaign_evidence.evaluate_h1 --output "$out/control-native.json.gz"
python -B -m research.net_campaign_evidence.replay_archive --data-root ../mission --native-root ../tv-final --output "$out/original-control"
python -B -m unittest discover -s tests
python -B -m unittest discover -s research/net_campaign_evidence
python -B -m unittest discover -s research/net_campaign_evidence/h1_native
python -B -m unittest discover -s research/gold_end_to_end
```

`full-replay.json.gz` съдържа всички наблюдения, планове, откази, части, разходи, остатък, марки и времеви сравнения. Регистрираните правила са в [REGISTRATION.json](REGISTRATION.json). Таблиците [EXPERIMENTS.csv](EXPERIMENTS.csv) и [REGIME_RESULTS.csv](REGIME_RESULTS.csv) са генерирани от replay-а. Състоянието на H1, настройките за ръчен експорт и точният Pine са в [H1 пакета](../net_campaign_evidence/h1_native/README_BG.md).
