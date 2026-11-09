"""Render existing audited metrics; do not recompute trading outcomes or cohorts."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ('B-ALL', 'B-RETEST', 'B-FAST', 'B-REVERSAL')
MODELS = {'1': 'Ретест', '2': 'Бърз пробив', '3': 'Ранно обръщане'}
SIDES = {'1': 'BUY / LONG', '-1': 'SELL / SHORT'}
RELATIONS = ('WITH_HTF', 'AGAINST_HTF', 'NEUTRAL_HTF')


def load(path):
    return json.loads(path.read_text(encoding='utf-8'), parse_float=Decimal)


def number(value, digits=3, signed=False):
    if value is None:
        return '—'
    result = f'{value:+.{digits}f}' if signed and value != 0 else f'{value:.{digits}f}'
    return result.replace('.', ',').replace('-', '−')


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def metric_cells(metrics):
    if metrics is None:
        return ['0', '0 / 0 / 0', '—', '—', '—', '—', '—']
    return [str(metrics['count']), f"{metrics['wins']} / {metrics['losses']} / {metrics['breakevens']}",
            number(metrics['net_cash'], signed=True), number(metrics['profit_factor'], 4),
            number(metrics['average_win_cash']), number(metrics['average_loss_cash']),
            number(metrics['closed_cash_event_drawdown'])]


def table(lines, labels, rows):
    lines.extend(['| '+' | '.join(labels)+' |', '| '+' | '.join('---' for _ in labels)+' |'])
    lines.extend('| '+' | '.join(cell(value) for value in row)+' |' for row in rows)
    lines.append('')


def main():
    paths = {run: ROOT/(run+'.audit.json') for run in RUNS}
    audits = {run: load(path) for run, path in paths.items()}
    for run, audit in audits.items():
        if audit['status'] != 'PASS_OBSERVED_JOURNAL' or audit['campaign_state_counts'] != {'COMPLETE': audit['complete_campaign_metrics']['count']}:
            raise ValueError(f'{run}: renderer requires these fully reconciled, complete baseline campaigns')
        for dimension in ('family', 'side', 'trend_relation', 'regime'):
            group = audit['complete_campaign_groups'][dimension]
            if sum(x['count'] for x in group.values()) != audit['complete_campaign_metrics']['count']:
                raise ValueError(f'{run}/{dimension}: supplied subgroup counts do not partition the audit')
            if sum((x['net_cash'] for x in group.values()), Decimal(0)) != audit['complete_campaign_metrics']['net_cash']:
                raise ValueError(f'{run}/{dimension}: supplied subgroup net totals do not partition the audit')
    lines = ['# Native разрези на цели позиции', '',
             'Таблиците показват готовите метрики от действителните [accounting audits](accounting/README_BG.md). Не преизчисляват стратегията и не са нови отделни backtests. Всички пари са USD след native разходите; BUY/SELL означава посоката на вече симулиран вход.', '',
             '**N** е брой завършени цели позиции. **П/З/0** са печеливши/губещи/нулеви цели позиции. **DD cash** е спадът по реализираните изходи на съответното подмножество, а не native equity DD и не резултат от самостоятелно пускане на този подфилтър. PF е за цели позиции. При N=0 няма наблюдаван резултат; при липса на загуби PF остава „—“.', '']
    metrics_labels = ['N', 'П/З/0', 'Net USD', 'PF', 'Средна печалба', 'Средна загуба', 'DD cash']
    lines += ['## По модел в общия портфейл и в изолирания run', '']
    rows = []
    for run, audit in audits.items():
        for family, metrics in sorted(audit['complete_campaign_groups']['family'].items()):
            rows.append([run, MODELS[family], *metric_cells(metrics)])
    table(lines, ['Run', 'Модел', *metrics_labels], rows)
    lines += ['Приносът на модел в ALL и неговият самостоятелен run са различни извадки заради заета позиция и приоритет. Не се събират като независими портфейли.', '', '## BUY / SELL за всеки baseline run', '']
    rows = []
    for run, audit in audits.items():
        for side in ('1', '-1'):
            rows.append([run, SIDES[side], *metric_cells(audit['complete_campaign_groups']['side'].get(side))])
    table(lines, ['Run', 'Посока', *metrics_labels], rows)
    lines += ['## Спрямо замразения HTF контекст', '',
              'WITH_HTF е посока, съгласувана с ненулевия HTF bias; AGAINST_HTF е противоположна посока; NEUTRAL_HTF е bias=0. Неутралният контекст не се слива с AGAINST_HTF.', '']
    rows = []
    for run, audit in audits.items():
        for relation in RELATIONS:
            rows.append([run, relation, *metric_cells(audit['complete_campaign_groups']['trend_relation'].get(relation))])
        for relation in sorted(set(audit['complete_campaign_groups']['trend_relation'])-set(RELATIONS)):
            rows.append([run, relation, *metric_cells(audit['complete_campaign_groups']['trend_relation'][relation])])
    table(lines, ['Run', 'HTF отношение', *metrics_labels], rows)
    lines += ['## Оригинален режим и countertrend flag', '',
              'Това са оригиналните замразени метаданни, отделни от трите HTF категории. Тук COUNTERTREND включва и част от неутралния HTF контекст. Показани са само наблюдаваните режими.', '']
    rows = []
    for run, audit in audits.items():
        for regime, metrics in sorted(audit['complete_campaign_groups']['regime'].items()):
            flags = {c['submitted_metadata'].get('countertrend') for c in audit['campaigns'] if c['regime'] == regime}
            flag = ', '.join('неизвестен' if value is None else str(value).lower() for value in sorted(flags, key=str))
            rows.append([run, regime, flag, *metric_cells(metrics)])
    table(lines, ['Run', 'Original regime', 'Original countertrend', *metrics_labels], rows)
    lines += ['## ALL: пълна матрица модел × посока × HTF', '',
              'Всички 18 комбинации са видими, включително липсващите наблюдения. Числата идват от съществуващите audit подгрупи. Ако в бъдещ износ една комбинация съдържа повече от един original regime, редовете остават разделени, без сливане на PF или DD.', '']
    grid, consumed = [], set()
    combined = audits['B-ALL']['complete_campaign_groups']['family_side_trend_regime']
    for family in ('1', '2', '3'):
        for side in ('1', '-1'):
            for relation in RELATIONS:
                prefix = f'M{family}|{side}|{relation}|'
                matches = sorted(k for k in combined if k.startswith(prefix))
                if not matches:
                    grid.append([MODELS[family], SIDES[side], relation, 'няма наблюдения', *metric_cells(None)])
                for key in matches:
                    consumed.add(key)
                    grid.append([MODELS[family], SIDES[side], relation, key.split('|', 3)[3], *metric_cells(combined[key])])
    for key in sorted(set(combined)-consumed):
        model, side, relation, regime = key.split('|', 3)
        grid.append([MODELS[model[1:]], SIDES[side], relation, regime, *metric_cells(combined[key])])
    table(lines, ['Модел', 'Посока', 'HTF', 'Original regime', *metrics_labels], grid)
    lines += ['## Native exit comment и количество по модел', '',
              '„Редове“ са native partial records; „Количество“ е затвореният размер; „Последен изход на N позиции“ брои само крайния comment на цялата позиция. TP1/TP2 и SL могат да се срещат в една позиция. SL comment след TP1 не означава автоматично губеща позиция. Тук не се измисля липсваща причина за общия етикет „ГАП“.', '']
    rows = []
    for run, audit in audits.items():
        for family, metrics in sorted(audit['complete_campaign_groups']['family'].items()):
            for reason, count in sorted(metrics['exit_comment_record_counts'].items()):
                rows.append([run, MODELS[family], reason, count,
                             number(metrics['exit_comment_quantities'][reason], 0),
                             metrics['final_exit_comment_campaign_counts'].get(reason, 0)])
    table(lines, ['Run', 'Модел', 'Exit comment', 'Редове', 'Количество', 'Последен изход на N позиции'], rows)
    chronology_path = ROOT.parent/'diagnosis/H1_COMPARISON.json'
    chronology = load(chronology_path)
    lines += ['## H1: дата на вход срещу дата на реализирания cash', '',
              'Граница: **01.10.2026 00:00 UTC**. Aug–Sep и Oct–Nov вече са наблюдавани исторически периоди, не untouched OOS. „Net по вход“ отнася цялата позиция към датата на входа; „Cash по exits“ отнася всяка част към действителния ѝ изход. DD на exit периода започва от нула на границата и не е native equity DD за този период.', '']
    rows = []
    for model in ('FAST', 'ALL'):
        for variant, prefix in (('baseline', 'B'), ('candidate', 'H1')):
            phases = chronology['comparisons'][model][variant]['phases']
            for phase, label in (('AUG_SEP_DEVELOPMENT', 'Aug–Sep'), ('OCT_NOV_ALREADY_OBSERVED_CONFIRMATION', 'Oct–Nov — вече наблюдаван')):
                entry = phases[phase]['entry_cohort_complete_campaign_metrics']
                exits = phases[phase]['realized_exit_period']
                rows.append([prefix+'-'+model, label, entry['n'], number(entry['net'], signed=True),
                             number(entry['profit_factor'], 4), number(entry['net_r'], 5),
                             number(exits['net_cash'], signed=True), number(exits['closed_cash_drawdown_restarted_at_phase_zero'])])
    table(lines, ['Run', 'Период', 'N по вход', 'Net по вход', 'PF по вход', 'Net R по вход', 'Cash по exits', 'DD на exit периода'], rows)
    lines += ['Пълните ID и пресичащите границата позиции са в [H1_COMPARISON.json](diagnosis/H1_COMPARISON.json). Нито една загуба не е преместена назад или премахната. H1 остава изследователски вариант и не е внедрен.', '',
              'Възпроизвеждане: `python diagnostics/accounting/render_breakdowns.py`. Този renderer чете готовите audit/chronology полета, без да симулира, променя филтри или преизчислява PF/DD. Източниците и хешовете са в [breakdowns_manifest.json](accounting/breakdowns_manifest.json).']
    output = ROOT.parent/'BREAKDOWNS_BG.md'
    output.write_text('\n'.join(lines)+'\n', encoding='utf-8', newline='\n')
    manifest = {'scope': 'DISPLAY_ONLY_EXISTING_AUDIT_AND_CHRONOLOGY_METRICS',
                'input_sha256': {str(path.relative_to(ROOT.parent)): hashlib.sha256(path.read_bytes()).hexdigest() for path in [*paths.values(), chronology_path]},
                'renderer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'output': 'diagnostics/BREAKDOWNS_BG.md', 'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                'all_matrix_rows': len(grid), 'all_observed_matrix_groups': len(combined),
                'data_recalculated': False}
    (ROOT/'breakdowns_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
