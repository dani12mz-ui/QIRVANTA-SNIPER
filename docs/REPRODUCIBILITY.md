# Възпроизвеждане на Ultimate

Изходен commit `c2212d7b7e0b4b7ea4be64c1fb3c9f7b33fa0f71`, tree `304b153b5f2e094d97082f7e9a1798cd2652942f`. Дървото фиксира съдържанието на всеки предишен файл. Реално проверената Windows среда използва CPython 3.12.14 AMD64 и SQLite 3.53.1. Заключеният `tzdata` wheel е изтеглен с проверка на SHA-256. [Базов manifest](ultimate_evidence/baseline.json) · [Lock файл](../qirvanta_ultimate/requirements.lock) · [Проверка на пакета](ultimate_evidence/dependency_audit.json)

От корена на checkout:

```powershell
python -m pip install --only-binary=:all: --require-hashes -r qirvanta_ultimate/requirements.lock
$env:PYTHONTZPATH = ''
python -B unified/tools/build.py --check
python -B unified/tools/build_ultimate.py --check
python -B -m unittest discover -s tests -v
python -B -m unittest discover -s research/tests -v
Push-Location unified
python -B -m unittest discover -s tests -v
Pop-Location
```

`build_ultimate.py --build` преизчислява само отделния експериментален Pine файл. `--observer` създава нов временен `.txt` с инструментиране за точно OANDA:XAUUSD M15. За проверката на native A/B използвайте действителните отделни Pine Logs exports за F1 OFF и ON. Командата спира при грешен режим, source hash, dataset hash, период или непълно осчетоводяване. Публичният резултат пази campaign IDs, всички изходи, нетен резултат и такси. Пълните CSV exports остават локални.

```powershell
python -B unified/tools/build_ultimate.py --observer ..\ultimate\NEW-observer.txt
python -B -m unified.tools.audit_ultimate_native ..\ultimate\f1_off_logs.csv ..\ultimate\f1_on_logs.csv --out ..\ultimate\NEW-native-audit.json
python -B -m qirvanta_ultimate.risk_stress diagnostics/accounting/B-ALL.audit.json ..\ultimate\NEW-risk-stress.json
python -B -m qirvanta_ultimate.mt5 probe
```

Изследователският runner фиксира dataset и source hashes, допускания за разходи, правила и всяко изпълнено сравнение. M15 оригиналният архив и обновените TradingView барове имат отделни хешове. За сценарии на M5/M30 са нужни локалните предишни exports с точните им hashes. За генерация на отчети се подава нова директория. Brier, log loss, MSE и прогнози са сравними при същия split. Измерената inference latency варира между изпълненията. [Изследователски runner](../research/ultimate/run.py) · [Регистрация](../research/ultimate/registration.json) · [Резултати](PERFORMANCE_COMPARISON.md)

Локалният monitor се стартира изрично в foreground. За смяна на версията спрете процеса, архивирайте journal и профила, проверете hash и отворете нов journal с нова регистрация за следващия период. Възстановяване на стар run се прави с неговите оригинални код, конфигурация и журнал. Отворените позиции остават в оригиналния журнал. [Ръководство за оператора](DEPLOYMENT_GUIDE_BG.md)
