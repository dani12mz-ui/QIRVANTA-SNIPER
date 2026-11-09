# Подготовка за GitHub публикация

Статус: `PENDING_GITHUB_PUBLICATION`. Потребителят поиска кодовете в GitHub. Подготвен е отделният клон `fix/audited-validation-integrity`; към създаването на този отчет още няма потвърдена remote публикация. Main и началният baseline не се променят.

Тестван локален code commit: `51e0aa20a5604b9d79e7214f6e2aca59ac01048a`; source fingerprint: `c7946faaa06cfc85ae1a757a5a9804fdffeb6552be9112dec20a9dc5ae674cb3`. Remote commit, създаден през GitHub Git Data API, може да има различен SHA; идентичността на доставения код се проверява с файловите SHA256/source fingerprint.

`reports/changes.patch` покрива промените на целия committed code snapshot спрямо началния commit103bc307, плюс необходимия за тестовете source_recovery.json. Останалите генерирани reports/доказателства и предходният patch са изключени. Проверен е `git apply --check` върху отделно разархивирано копие на baseline. След прилагане от refactored изпълни `python tools/build.py --check` и `python tools/run_tests.py`.

Личният предходен TradingView скрипт е само локален backup извън repository и не е в Git/ZIP. Текущите отчети пазят резултатите и ограниченията от TradingView; архивните локални тестове са обозначени като исторически.
