"""Render Bulgarian experiment documentation directly from recorded results."""
import json
from research.validation.protocol import ROOT, REPORTS


def number(value, digits=3):
    return '—' if value is None else f'{value:.{digits}f}'


def render():
    native=json.loads((REPORTS/'native_enrichment.json').read_text('utf-8'))
    model=json.loads((REPORTS/'logistic_validation.json').read_text('utf-8'))
    results=json.loads((REPORTS/'experiment_results.json').read_text('utf-8'))
    m30=json.loads((REPORTS/'m30_results.json').read_text('utf-8'))
    corrected=json.loads((REPORTS/'paid_cash_correction_comparison.json').read_text('utf-8'))
    actual=json.loads((REPORTS/'actual_tf_results.json').read_text('utf-8'))
    ablation=json.loads((REPORTS/'ablation_results.json').read_text('utf-8'))
    all_trials=json.loads((REPORTS/'all_129_multiple_testing.json').read_text('utf-8'))
    baseline={t['trial']['family']:t for t in results['trials'] if t['trial']['id'].endswith('-BASE')}
    coarse={t['trial']['family']:t for t in m30['trials']}
    lines=['# Резултати от инженерните изследвания','',
           '**Не е установено независимо търговско предимство и няма нов champion.** Реалната native база остава 72 позиции, 212 частични изхода, −144.492 USD, PF 0.905096 и equity DD 842.255 USD. Новите кандидати са офлайн Python симулации с друг двигател; положителна стойност в таблицата не доказва подобрение на Pine, изпълнимост при PU Prime или бъдеща доходност.','',
           '## Регистрация, данни и единица на анализа','',
           'На 09.11.2026 17:29:12 UTC, преди изпълнение, са регистрирани 80 опита: S1–S10 × базов partial-R, четири exit варианта, delay2 и два cost stress варианта. `experiment_registration.json` е непроменен. Всички нулеви, блокирани и губещи опити са запазени. Няма търсене на оптимален праг или избор само на победители.','',
           'M15 използва заключени 6 445 истински OANDA бара, SHA256 `7015f6386d2627237c57ff8625d59cbe3c9b852e4742b786cbb4679db3d48c56`, 02.08.2026 22:00–09.11.2026 02:45 UTC. Това е вече наблюдаваният период. На 17:40:28 UTC, след M15 резултатите и преди M30 изпълнение, е регистрирано допълнение за още 10 базови опита върху 3 222 точни UTC двойки от M15. Последният непълен M15 бар е изключен: производният M30 завършва 02:30 UTC, не 02:45. Не са добавяни цени и не се реконструира M5.','',
           'M30 не е нова независима/native история. Същите числови lookback/hold/cooldown параметри в барове удвояват часовите хоризонти; входната латентност и OHLC двусмислието също се променят. Това е комбиниран robustness сценарий, не изолирана причинна проверка на timeframe. S6 остава блокиран поради непроверен volume/VWAP, S10 — без независимо валидирана routing таблица.','',
           'Една позиция означава един първоначален entry/plan, обединяващ частичните изходи. Таблиците показват завършени позиции; незатворените количества и вече платените такси остават видими отделно. Python stop-first правило при OHLC едновременно SL/TP е консервативна конвенция; не е доказана Pine emulator parity.','',
           '## Базови кандидати при еднакви заявени разходи','',
           'Quantity 3, pointvalue 1, commission 0.175/unit/fill, slippage 2 ticks × 0.001. Това са симулационни единици, не PU Prime lots. DD paid-cash отчита изплатените такси в момента на плащане и cash изходите; не е mark-to-market equity DD.','',
           '| Семейство | M15 N | M15 net USD | PF | DD paid cash | Отворени | M30 N | M30 net USD |',
           '|---|---:|---:|---:|---:|---:|---:|---:|']
    for family,t in baseline.items():
        a=t['summary']; b=coarse[family]['summary']
        if t['blocked_families']:
            lines.append(f'| {family} | 0 | BLOCKED | — | — | 0 | 0 | BLOCKED |')
        else:
            lines.append(f"| {family} | {a['complete_plans']} | {number(a['net_cash'])} | {number(a['profit_factor'])} | {number(a['cash_paid_drawdown'])} | {a['open_plans']} | {b['complete_plans']} | {number(b['net_cash'])} |")
    positive=sum(t['summary']['net_cash']>0 for t in results['trials'])
    significant=sum(t['holm_adjusted_p_all_90']<.05 for t in m30['combined_multiple_testing'])
    lines += ['',f'{positive}/80 първоначални опита имат положителен net. След обща Holm корекция за всичките 90 опита има {significant} резултата с adjusted p<0.05. Самите sign-flip p-values са exploratory: зависимостта, симетрията и стационарността на резултатите не са доказани. Holm не поправя вече наблюдавани данни, неизвестен брой предходни идеи или неправилен модел на изпълнение.','',
              'S3 M15 има само 14 позиции, а на производния M30 — 5 и отрицателен net. S8 има 21 M15 и 6 M30 позиции. S5 има положителен cash, но M15 интервалът за mean R включва нула. Тези наблюдения не оправдават включване на нови семейства. S7 има незатворена позиция, която не е скрита или принудително затворена в края.','',
              '## Всички първоначални варианти','',
              'Всяка клетка е **брой завършени позиции / net USD**. Различните exit/latency настройки променят заетостта и следващите входове, затова това не е филтриране на един фиксиран списък сделки. Cost1.5/2 преизпълняват двигателя с умножени commission и slippage.','',
              '| Семейство | PARTIAL_R | FIXED_R | STRUCTURAL | BREAKEVEN | ATR_TRAIL | DELAY2 | COST1.5 | COST2 |',
              '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    ids={t['trial']['id']:t for t in results['trials']}
    for family in baseline:
        cells=[]
        for suffix in ('BASE','FIXED_R','STRUCTURAL','BREAKEVEN','ATR_TRAIL','DELAY2','COST1.5','COST2'):
            t=ids[family+'-'+suffix]
            cells.append('BLOCKED' if t['blocked_families'] else f"{t['summary']['complete_plans']} / {number(t['summary']['net_cash'],2)}")
        lines.append('| '+family+' | '+' | '.join(cells)+' |')
    ci=native['uncertainty']['net_r_mean']['ci95']
    wc=native['native_summary']['win_rate']
    bounds=native['excursions']; exposure=native['exposure']
    lines += ['', '## Неопределеност на native базата','',
              f"Mean net R = −0.102062; circular moving-block bootstrap (5 позиции, 2 000 повторения, seed 20261109) 95% интервал [{number(ci[0],6)}, {number(ci[1],6)}]. Win-rate 33/72 = 45.83%; Wilson 95% [34.83%, 57.26%]. Условията за стационарен bootstrap не са установени; това са описателни интервали, не доказателство за edge.",'',
              f"Всички 72 позиции са съпоставени по timestamps/index към OHLC. Median price-MFE е ограничен между {number(bounds['median_price_MFE_R_lower'])}R и {number(bounds['median_price_MFE_R_upper'])}R; median price-MAE между {number(bounds['median_price_MAE_R_lower'])}R и {number(bounds['median_price_MAE_R_upper'])}R. Това са граници на ценова екскурзия докато остава количество, **не точни quantity-weighted cash MFE/MAE**. Входният/изходният бар може да има high/low извън времето на позицията. Пълните вътрешни барове и действителните fills дават долната граница; целият OHLC envelope — горната.",'',
              f"Median записана продължителност {bounds['median_recorded_duration_hours']} часа; максимум {bounds['max_recorded_duration_hours']} часа включва затворени пазарни периоди. Intrabar exit timestamps са барови, не точни секунди. Експозицията върху наличните барове е в граници {100*exposure['covered_market_bar_fraction_lower']:.2f}%–{100*exposure['covered_market_bar_fraction_upper']:.2f}%; това не е quantity-weighted време или weekend ликвидност.",'',
              'Групите по семейство/посока/UTC седмица/месец/DST сесия/coarse native regime и efficiency са в `native_enrichment.json`. В `candidate_decomposition.json` са всички M15 trial групи и отделни cash реализации по месец. Entry-cohort резултатите не се смесват с календарния месец на получените пари; cross-month позиции са изведени. Volatility bins 0.8/1.2 са допълнително описателно разделение след резултатите, не предварително регистриран филтър. Native regime е само TREND/COUNTERTREND.','',
              'Sequence stress пренарежда цели позиции с фиксиран seed и отчита DD/streak чувствителност; не генерира бъдеща доходност. Deflated Sharpe не е числено представен: липсват надеждна непрекъсната mark-to-market equity серия, оправдана честота/годишнизация и независим ефективен брой избрани хипотези. Не се заменя с годишен Sharpe от 72 неравномерни сделки.','',
              '## Малък логистичен baseline и хронология','']
    m=model['chronological_60_20_20']
    lines += [f"43 train / 14 validation / 15 test кампании по време на сигнала. Скалиране и logistic weights се fit-ват само на train; Platt calibration — само на validation. Labels са печалба след действителна native commission на цялата позиция. Test Brier: raw {m['raw']['brier']:.6f}, константа train prevalence {m['constant_train_prevalence']['brier']:.6f}, calibrated {m['platt_validation_only']['brier']:.6f}. Raw моделът е по-слаб от константата; малката calibrated разлика върху 15 вече видени позиции не доказва приложимост.",'',
              'Функциите правят purge на events, чийто краен label достига следващия partition, плюс embargo от един M15 бар. Има expanding walk-forward с първи train 24 и test блокове 12; всеки fit/calibration използва само предходни labels. Артефактът пази split IDs, всички labels/features, weights/scalers, training hash, probabilities, reliability bins, Brier/log loss и fold резултати. Няма random split, автоматично изпълнение или Pine модел.','',
              'Отделният offline triple-barrier helper пази availability time; едновременно SL/TP в един OHLC бар дава AMBIGUOUS/label null, непълен horizon — CENSORED. Тези labels са gross price outcomes и **не са използвани** за логистичния net-win label. Сложни AI модели са отложени заради малкия зависим sample, липсващи независими labels/PIT данни и липса на доказана полза от простия baseline; не се твърди работещ transformer/HMM/XGBoost.','',
              '## Реални поправки в изследователския код','',
              '1. Circular block от 5 върху N≤5 дава изкуствено дегенериран mean интервал. След откриване е регистрирана статистическа поправка; тези CIs са UNAVAILABLE/INSUFFICIENT_BLOCKS. Първоначалният report/source са запазени. Нито един fill/net не е променен.','',
              f"2. Независимият review откри, че първият Python cash ledger отлага entry commission до partial exit. Поправката е регистрирана преди rerun на същите 90 сценария: entry fee се дебитира при вход, exit добавя gross−exit fee. Старият allocated closed-part ledger остава отделен; действителният paid-cash ledger захранва daily guard. {corrected['count']} сравнения доказват еднакви стари signals/plans/fill prices/qty/complete net; разликата в final paid cash е точно неприписаната към затворени части entry fee. Daily limits са None (изключени) по подразбиране, затова не са променени решенията. Началните 90 журнала и source snapshots са в `method_history/paid_cash_precorrection`.",'',
              '3. CSV timestamps вече се проверяват за целочисленост/finite преди преобразуване, вместо дробна част да се отрязва. Реалният заключен dataset и производният M30 manifest остават точно еднакви.','',
              '4. Offline broker calculator закръгля lot към цяло кратно на volume step от нула, после проверява minimum. Непроверени параметри, stale quote, неизвестна валута, недостатъчен risk budget или pause блокират резултата. Това не дава проверени PU Prime спецификации.','',
              '## Възпроизвеждане и граници','',
              'Първоначалните run/freeze команди са еднократни и умишлено отказват тихо презаписване след промяна на source. За проверка на окончателните 90 журнала: `python -m research.validation.replay --output <нов-файл.json>`. Добавете `--execute` за детерминирано преизпълнение на записаните конфигурации с фиксирания data hash; никога не посочвайте стар отчет като output. `python -m research.validation.native_report` възпроизвежда native descriptive/model reports; `python -m research.validation.candidate_analysis` и `python -m research.validation.render_report` обновяват производните анализи. Tests: `python -m unittest discover -s research/tests -p "test_validation*.py" -v`.','',
              'Действителният runtime е CPython 3.12.14, Windows AMD64, IANA provider `tzdata==2026.6` (zoneinfo TZPATH е празен). Версиите и source hashes са във `final_reproduction_manifest.json`; dependency pin е `research/validation/runtime_requirements.txt`. Не са инсталирани пакети. За еднакви DST правила на друга система задайте същия provider, вместо да разчитате на различна OS timezone база.','',
              'Първоначалните 80 + допълнителните 10 не се броят повторно като нови хипотези при accounting rerun. Economic source hashes, amendment hashes, всички неуспехи и пълни compressed журнали са запазени локално. Няма реален broker execution, timestamped news/macroeconomic history или недокоснат бъдещ тест. По-късното допълнение с действителни M5/M30 е описано по-долу; то не превръща вече наблюдавания календарен период в независима проверка. Периодът 10.11–10.12.2026 UTC е само предварително описана бъдеща рамка, още без налични данни/планирана автоматична търговия.','',
              'Консервативна работна конфигурация: запазена champion логика за наблюдение; всички нови research families/models/routing OFF, S6/S10 blocked, ръчен news pause наличен. Native RAW quantity3 не е broker risk sizing и не наследява paper/shadow statistical/daily-R guards. Положителна таблица не включва challenger.','']
    lines += ['## По-късно допълнение с действителни M5/M15/M30','',
              'На 18:01:54 UTC, преди тези изпълнения, са регистрирани още 30 BASE опита. Трите входни истории са действителни TradingView exports с проверен source/редове/timestamps/хешове: M5 5 565 бара, M15 refreshed 6 445, M30 10 091. Общият trading интервал е **23.10.2026 20:00–09.11.2026 02:30 UTC**. Началото е най-късната първа наличност на ready entry features плюс затворен H1/H4 EMA контекст (50 завършени H4 бара), закръглено нагоре до UTC половин час; краят е най-ранният последен close, закръглен надолу. Нито една доходност не участва в избора.','',
              'Предходните барове служат за feature/setup warmup. Python започва flat при общото начало; input е отрязан до общия край без принудително затваряне. Същите числови настройки в барове означават различни часови хоризонти. M15 използва по-късния refreshed CSV `6c890151…` само в това ново допълнение; оригиналните 90 остават върху `7015f638…`. Actual M30 и производният M30 имат еднакви OHLC на всичките 3 222 общи timestamps, но 31 volume различия; това не валидира VWAP.','',
              '| Native timeframe | Цял наличен период N / net | Общ entry cohort N | Общ net USD | Общ PF |',
              '|---|---:|---:|---:|---:|']
    for tf in ('5','15','30'):
        n=actual['native_cohorts'][tf]; f=n['full_loaded_history_metrics']; c=n['cohort_metrics']
        lines.append(f"| M{tf} | {f['count']} / {number(f['net_cash'])} | {c['n']} | {number(c['net_cash'])} | {number(c['profit_factor'])} |")
    lines += ['', 'Пълните налични периоди са различни: M5 започва 11.10, M15 — 02.08, M30 — 01.01.2026. +50.438 USD на пълния M5 не е доказателство за подобрение спрямо M15; в общия период M5 е −79.700 USD. Native колоните са реални full-history runs, след това entry-cohort извличане, а не native рестарт на общото начало. В този конкретен cohort няма prewindow carryover или end-censored позиции; бъдеща такава позиция се отчита отделно. Не е изчислен измислен native intrabar DD за изрязания cohort.','',
              '| Python семейство | Actual M5 N / net USD | Actual M15 N / net USD | Actual M30 N / net USD |',
              '|---|---:|---:|---:|']
    amap={(t['trial']['family'],t['trial']['timeframe']):t for t in actual['trials']}
    for family in baseline:
        cells=[]
        for tf in ('5','15','30'):
            t=amap[(family,tf)];s=t['summary']
            cells.append('BLOCKED' if t['blocked_families'] else f"{s['complete_plans']} / {number(s['net_cash'])}")
        lines.append('| '+family+' | '+' | '.join(cells)+' |')
    for t in actual['trials']:
        s=t['summary']
        if s['open_plans'] or s['pending_plans']:
            lines += ['',f"Незатворен резултат: {t['trial']['id']} има {s['open_plans']} open и {s['pending_plans']} pending позиции. Net само на завършените е {number(s['net_cash'])} USD; actual paid cash, включително частичните изходи и платената entry fee, е {number(s['cash_paid_net'])} USD. Нереализираният остатък не е оценен като затворена печалба/загуба."]
    lines += ['', 'Тези са различни семейства и различен OHLC двигател от champion. Таблицата е описателна; не показва paired подобрение на native правилата. Нулевите случаи и S6/S10 блокировките са запазени. Новите raw CSV/логове остават локално, защото права за публично разпространение не са установени.','',
              '## Ограничена проверка на прагове и принос на компонент','',
              'Отделна регистрация на 18:01:54 UTC, преди 9 варианта: S3/S5 body ATR 0.20/0.30 около 0.25; S8 ADX 18/22 около 20. Избраните S3/S5/S8 вече са показали положителен M15 BASE net, затова това е **post-selection exploratory** проверка, а не независимо потвърждение. Няма избор на най-добрия праг или промяна на production.','',
              'Ablation премахва само CONFIRMATION_CANDLE за S3/S5 или PRIOR_PAUSE за S8 чрез отделен runtime adapter. Всички други причини за отказ остават, raw-trigger state consumption е същият, всеки променен decision пази original reasons/removed gate. Frozen engine/library файловете не са променяни. Стратегическата идентичност включва adapter source hash плюс config/engine/library hashes; само config hash не е достатъчен.','',
              '| Вариант | N | Net USD | PF | Променени gate decisions |',
              '|---|---:|---:|---:|---:|']
    for t in ablation['trials']:
        s=t['summary']
        lines.append(f"| {t['trial']['id']} | {s['complete_plans']} | {number(s['net_cash'])} | {number(s['profit_factor'])} | {t['ablation_altered_decisions']} |")
    lines += ['', '„Променени gate decisions“ брои редовете с премахната конкретна причина, включително тези с други оставащи откази; това не е брой нови потвърдени сигнали или изпълнени входове.','',
              'Повече сигнали не означават по-добър резултат: премахването на candle gate увеличава S3 от 14 на 71 позиции, но net пада от +392.697 на +83.434 USD; S5 от 131 на 151 позиции, net от +393.951 на +28.906. S8 без PRIOR_PAUSE има +470.321 USD при само 22 позиции срещу 21 базови; това не оправдава внедряване. Изменената заетост създава различни входове, затова приносът не е причинен ефект върху фиксиран независим списък сделки.','',
              f"Общият брой е {all_trials['count']} предварително описани за съответния етап опита: 80+10+30+9; accounting/replay повторенията не се броят като нови. Обща Holm корекция: {all_trials['significant_count_05']} adjusted p<0.05. Известните предходни идеи извън тази мисия не са включени в този формален брой; p-values остават описателни. Нито един вариант не е включен в champion.",'',
              'Пълните local журнали съдържат reference close по барове и затова не се публикуват. Първата unpublished sparse policy оставяше raw-setup цени; сборът на многото опити възстановяваше около 45% от close историята. Окончателната **v2** премахва `reference`, `initial_stop`, `structure_target` от **всички decision rows**, включително rejected/raw candidates. Всички редове, IDs/status/reasons остават; всички действителни избрани plans, fills, входни/изходни цени, stop/targets на позициите, загуби, количества, такси и equity остават непроменени. Пълните цени на всички неизбрани кандидати са налични само в непромененото local evidence. `public_journals_manifest.json` свързва original-full SHA с public SHA и точната политика. Това е ограничение за разпространение на пазарните данни, не скриване на губещи сделки.','']
    lines += ['За 39-те допълнителни сценария: `python -m research.validation.replay_extensions --output <нов-файл.json> [--execute]`. И двата replay инструмента приемат `--public-only` за изрична проверка на sparse артефактите; когато пълният local журнал липсва, използват публичния по original SHA. `--execute` за actual timeframes изисква отделно разрешените локални CSV файлове с регистрираните hashes; не изтегля или измисля липсващи данни. Непроменените пълни local evidence и публичната редакция не се представят като еднакви bytes — съпоставя се точно описаната sanitization проекция.','']
    target=ROOT/'docs/EXPERIMENT_RESULTS.md'
    target.write_text('\n'.join(lines),'utf-8')
    return str(target)


if __name__=='__main__':print(render())
