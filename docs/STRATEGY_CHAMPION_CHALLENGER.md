# Champion и кандидати

Champion остава публикуваната QIRVANTA Unified логика от `c2212d7b7e0b4b7ea4be64c1fb3c9f7b33fa0f71`. Запазеният native B-ALL журнал е сверèн по 72 plan IDs и 212 изпълнени части. [Базов одит](BASELINE_AUDIT.md) · [Машинно сверяване](../research/ultimate/reports/phase2_results.json).

| Кандидат | Ниво на текущо доказателство | Решение |
|---|---|---|
| F1 .55 | Реален native A/B на фиксирани данни, по-лош net и PF | REJECT |
| F2–F12 | Отделни ретроспективни или anchored OHLC сценарии | INSUFFICIENT_EVIDENCE |
| S1–S5, S7–S9 | Самостоятелен исторически engine и запазени положителни/отрицателни резултати | INSUFFICIENT_EVIDENCE |
| S6 | Истински volume/VWAP вход липсва | BLOCKED_EXTERNAL |
| S10 | Липсва независимо валидиран routing | BLOCKED_EXTERNAL |
| D–L | Отделни OHLC правила, сравнение на общ прозорец | INSUFFICIENT_EVIDENCE |
| Logistic, Platt, ridge | Исторически inference и времево разделена оценка. Непоследователни резултати и малка извадка | INSUFFICIENT_EVIDENCE |

Първоначалната регистрация фиксира FAST, exit и family вариантите. Ridge е отделен предварително фиксиран follow-up след първия исторически run. Съвпадението на 77 scenario replay резултата и 4 класификационни цели е проверено повторно. Изследователските журнали пазят всички завършени, отворени и чакащи планове. [Сверяване на повторното изпълнение](../research/ultimate/reproduction_verification.json).

Промоцията изисква последователно integrity, deterministic tests, причинно валидирани данни, приложима платформа, сравними разходи, независими статистически наблюдения, prospective paper и операторско разрешение. Най-добър исторически резултат и Brier score служат за селекция на бъдещи хипотези. `PROMOTE` остава затворен. [Forward протокол](PHASE9_FORWARD_VALIDATION.md).
