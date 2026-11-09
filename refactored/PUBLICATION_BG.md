# Публикуван код в GitHub

Статус: **PUBLISHED**. Двата Pine файла са публикувани в отделния [клон fix/audited-validation-integrity](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/tree/fix/audited-validation-integrity) чрез GitHub Git Data API. Потвърденият [delivery commit](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/commit/3495660fe88200b6d8c8ebd5c0a7f179da0d8eed) е `3495660fe88200b6d8c8ebd5c0a7f179da0d8eed`, tree `69a241d300952945c620c44167f911a4bda4701e`. Tree съвпада точно с локалния delivery snapshot `bb4517896fd5dd64da61ce70cc4f990fbd90379c`; проверени са и двата audited Pine blob ID. Main остава на `7fe6534f72fa23da18aa9282ee8f5ac11dcc9df6`, началният delivery branch — на `103bc30788b18234f5938ed0f5346e142097a6fe`.

Локалният `git push` не успя заради credential helper; успешната публикация е през GitHub connector. Затова машинните полета са `github_published=true`, `github_pushed=false`. [Публикационните доказателства](reports/publication.json) идентифицират вече проверената кодова публикация; следващият commit добавя актуализираните отчети и окончателното TradingView доказателство. Тук не се твърди самореферентен SHA на бъдещия финален commit.

- [PRO — потвърден код](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/blob/3495660fe88200b6d8c8ebd5c0a7f179da0d8eed/audited/QIRVANTA_SNIPER_PRO.pine)
- [VALIDATION — потвърден код](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/blob/3495660fe88200b6d8c8ebd5c0a7f179da0d8eed/audited/QIRVANTA_SNIPER_VALIDATION.pine)

Тестван локален code commit: `51e0aa20a5604b9d79e7214f6e2aca59ac01048a`; source fingerprint: `c7946faaa06cfc85ae1a757a5a9804fdffeb6552be9112dec20a9dc5ae674cb3`. API commit има различен SHA, но публикуваните кодови байтове са същите. 127 локални теста са успешни; след тестовете няма промяна по source. И двата Pine файла са компилирани и изпълнени в TradingView; PRO има две разгледани предупреждения. Запазеният личен PRO, версия 2, е отворен отново и целият текст съвпада с крайния PRO след нормализиране на новите редове. [Окончателна снимка](reports/tradingview_evidence/tradingview_integrated_pro.png).

`reports/changes.patch` покрива промените на целия committed code snapshot спрямо началния commit 103bc307, плюс необходимия за тестовете source_recovery.json. Останалите генерирани reports/доказателства и предходният patch са изключени. `git apply --check` е успешен върху отделно разархивирано копие на baseline. След прилагане от refactored изпълни `python tools/build.py --check` и `python tools/run_tests.py`.

Предходният личен TradingView скрипт е само локален backup извън repository и не е в Git/ZIP. Текущите отчети запазват ограниченията: отворен mobile layout дефект, липса на пълно експортно съгласуване по сделки, OOS и forward тест. Няма твърдение за подобрена доходност.
