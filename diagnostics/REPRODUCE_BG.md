# Възпроизвеждане без презапис на доказателствата

Използвай пълен clone с историята на commit `48e5cddab17ea831bdc277f17f0406329fdfe39c` и Python 3.12. Командите са от корена на repository.

```text
python -B -m unittest discover -s diagnostics/warnings -p "test_*.py" -v
python -B -m unittest discover -s diagnostics/accounting -p "test_*.py" -v
python -B -m unittest discover -s diagnostics/diagnosis -p "test_*.py" -v
python -B -m unittest discover -s diagnostics/protocol -p "test_*.py" -v
```

Тестовете са самостоятелни и създават временните editor buffers в системна временна директория. Те не изискват непубликувани файлове от първоначалния workspace и не презаписват историческите отчети.

За ново диагностично изпълнение генерирай временни `.txt` файлове:

```text
python diagnostics/make_probe.py work/observer.txt
python diagnostics/make_h1_probe.py work/observer.txt work/h1.txt
```

Втората команда създава `work/h1.manifest.json` до временния buffer и отказва да презапише съществуващ buffer/manifest. Първоначалните `diagnostics/diagnosis/H1_GENERATED*.json` са историческите записи от вече извършения тест и не се обновяват. H1 остава изследователски, без приемане в production.

Pine трябва да се компилира и изпълни в TradingView. Python тестовете не са Pine компилатор. Запази оригиналния script, provider, M15, разходи и всички Inputs; изнеси действителните Pine Logs в нова папка. Cutoff в този observer е фиксиран за M15 на 09.11.2026 02:45 UTC. Ако този timestamp вече не присъства в достъпната история, не се създава резултат за същия набор: не подменяй липсващите барове и не представяй новия feed като идентичен.

В този етап не се пуска автоматично бъдещо наблюдение или брокерска търговия. Точните команди за accounting/износ и определенията на метриките са в [accounting/README_BG.md](accounting/README_BG.md) и [protocol/README_BG.md](protocol/README_BG.md). За нови анализи използвай нови output пътища; не презаписвай първичните CSV и записаните отчети.
