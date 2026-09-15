"""Monthly FH reporting appended to the existing participant workbook.

Row 2 headers after the existing 76 columns: YYYY-MM | indicator_key.
Community figures belong on VHT rows; group figures occur once per group/month.
"""
from collections import defaultdict
from datetime import date
import csv
import io
import math
import re

from db import get_setting, set_setting

# label, reporting grain, aggregation
METRICS = {
    'households_visited': ('Households visited', 'vht', 'activities'),
    'children_screened': ('Children under six screened', 'vht', 'activities'),
    'plw_screened': ('Pregnant/lactating women screened', 'vht', 'activities'),
    'anc': ('ANC visits', 'vht', 'activities'),
    'pnc': ('PNC visits', 'vht', 'activities'),
    'immunized': ('Children immunized', 'vht', 'activities'),
    **{f'{status}_{sex}': (f'{status.upper() if status != "normal" else "Normal"} children · {sex}', 'vht', 'screening results')
       for status in ('sam', 'mam', 'normal') for sex in ('male', 'female')},
    **{f'plw_{status}': (f'Women · {status.upper()}', 'vht', 'screening results') for status in ('sam', 'mam', 'normal')},
    'homes_visited': ('Care-group homes visited', 'group', 'activities'),
    'latrines': ('Homes with functional latrines', 'group', 'snapshot'),
    'rubbish_pits': ('Homes with rubbish pits', 'group', 'snapshot'),
    'drying_racks': ('Homes with drying racks', 'group', 'snapshot'),
    'miycan': ('New MIYCAN completions', 'group', 'activities'),
    'breastfeeding': ('Mothers exclusively breastfeeding', 'group', 'snapshot'),
    'kitchen_gardens': ('Functional kitchen gardens', 'group', 'snapshot'),
}
DERIVED = {'sam': ('SAM children', 'vht', 'screening results'), 'mam': ('MAM children', 'vht', 'screening results'),
           'normal': ('Normal children', 'vht', 'screening results'), 'male': ('Male children screened', 'vht', 'screening results'),
           'female': ('Female children screened', 'vht', 'screening results')}
ALL_METRICS = {**METRICS, **DERIVED}
LOCATIONS = ('district', 'subcounty', 'parish', 'village', 'health_facility', 'vht', 'group')
NOTES = [
    'Reported population: children under six. The supplied MUAC cut-offs cover ages 6–59 months only; they must not be applied to other ages.',
    'Child MUAC (6–59 months): SAM <11.5 cm; MAM 11.5 to <12.5 cm; Normal ≥12.5 cm. Women: SAM <19 cm; MAM 19 to <23 cm; Normal ≥23 cm. Uploaded classification counts are used; aggregate counts cannot be reclassified.',
    'Cumulative activities and screening results may include repeat people or households. Unique reach unavailable: this reporting dataset contains aggregate counts.',
    'Snapshots use the selected month (or latest reporting month in cumulative mode), without carrying forward missing units. MIYCAN means new completions in that month, not a running total. Totals cover submitted reports only. Month-to-month changes are unavailable when the reporting units differ.',
]


def import_monthly(connection, sheet):
    """Validate all appended data before persisting; reuploads replace matching reports."""
    headers = next(sheet.iter_rows(min_row=2, max_row=2, values_only=True))
    columns = []
    facility_col = reporting_vht_col = None
    for i, header in enumerate(headers[76:], 76):
        text = str(header or '').strip()
        if text.lower() == 'health facility':
            facility_col = i
            continue
        if text.lower() == 'reporting vht':
            reporting_vht_col = i
            continue
        if not text:
            continue
        match = re.fullmatch(r'(\d{4}-\d{2})\s*\|\s*([a-z_]+)', text)
        if not match or match[2] not in METRICS:
            raise ValueError(f'Column {i+1}: use YYYY-MM | indicator_key (see FH reporting guide).')
        date.fromisoformat(match[1] + '-01')
        if (match[1], match[2]) in [(m, k) for _, m, k in columns]:
            raise ValueError(f'Duplicate monthly column: {text}')
        columns.append((i, match[1], match[2]))
    incoming = {}
    for rowno, row in enumerate(sheet.iter_rows(min_row=4, values_only=True), 4):
        loc = dict(zip(('vht', 'group', 'village', 'parish', 'subcounty', 'district'),
                       [str(row[i] or '').strip() for i in (1, 5, 6, 7, 8, 9)]))
        loc['health_facility'] = str(row[facility_col] or '').strip() if facility_col is not None else ''
        for index, month, metric in columns:
            value = row[index]
            if value is None or value == '':
                continue
            if isinstance(value, bool):
                raise ValueError(f'Row {rowno}: {metric} must be a non-negative whole number.')
            try:
                value = float(value)
            except (ValueError, TypeError):
                raise ValueError(f'Row {rowno}: {metric} must be a number.') from None
            if not math.isfinite(value) or value < 0 or not value.is_integer():
                raise ValueError(f'Row {rowno}: {metric} must be a non-negative whole number.')
            grain = METRICS[metric][1]
            if grain == 'vht' and str(row[4] or '').strip().lower() != 'vht':
                raise ValueError(f'Row {rowno}: community indicators must be entered on a VHT row.')
            if not loc[grain] or not loc['district'] or not loc['village']:
                raise ValueError(f'Row {rowno}: district, village and {grain} are required for monthly reporting.')
            identity = [loc[k].casefold() for k in ('district', 'subcounty', 'parish', 'village', grain)]
            import hashlib
            key = 'fh_report:' + hashlib.sha256(repr([month, grain, identity]).encode()).hexdigest()
            report_loc = dict(loc)
            if grain == 'group':
                report_loc['vht'] = (str(row[reporting_vht_col] or '').strip() if reporting_vht_col is not None
                                     else loc['vht'] if str(row[4] or '').strip().lower() == 'vht' else '')
            report = incoming.setdefault(key, {**report_loc, 'month': month, 'grain': grain, 'values': {}})
            if metric in report['values']:
                raise ValueError(f'Row {rowno}: {metric} occurs more than once for this {grain} in {month}. Enter each total only once.')
            report['values'][metric] = int(value)
    # Validate complete classification totals against an explicitly reported screening total.
    for key, report in incoming.items():
        values = {**get_setting(connection, key, {}).get('values', {}), **report['values']}
        if 'children_screened' in values:
            classified = [values.get(f'{s}_{sex}') for s in ('sam','mam','normal') for sex in ('male','female')]
            if sum(v for v in classified if v is not None) > values['children_screened']:
                raise ValueError(f"{report['month']} / {report['vht']}: classifications exceed children screened.")
            if all(v is not None for v in classified) and sum(classified) != values['children_screened']:
                raise ValueError(f"{report['month']} / {report['vht']}: complete classifications must equal children screened.")
    changed = 0
    for key, report in incoming.items():
        previous = get_setting(connection, key, {})
        report['values'] = {**previous.get('values', {}), **report['values']}
        if report != previous:
            set_setting(connection, key, report)
            changed += 1
    return changed


def reports(connection):
    import json
    return [json.loads(r['value']) for r in connection.execute("SELECT value FROM settings WHERE key LIKE 'fh_report:%'")]


def value_for(report, metric):
    values = report['values']
    if metric in METRICS:
        if metric == 'children_screened' and metric not in values:
            parts = [values.get(f'{s}_{sex}') for s in ('sam', 'mam', 'normal') for sex in ('male', 'female')]
            return sum(parts) if all(v is not None for v in parts) else None
        return values.get(metric)
    parts = ([values.get(f'{metric}_{sex}') for sex in ('male', 'female')] if metric in ('sam', 'mam', 'normal')
             else [values.get(f'{s}_{metric}') for s in ('sam', 'mam', 'normal')])
    return sum(parts) if all(v is not None for v in parts) else None


def aggregate(rows, metric, month='', cumulative=False):
    grain, kind = ALL_METRICS[metric][1:]
    selected = [r for r in rows if r['grain'] == grain and
                ((not month or r['month'] <= month) if cumulative else r['month'] == month)]
    if cumulative and kind == 'snapshot' and selected:
        latest = month or max(r['month'] for r in selected)
        selected = [r for r in selected if r['month'] == latest]
    values = [value_for(r, metric) for r in selected]
    return {'value': sum(v for v in values if v is not None) if any(v is not None for v in values) else None,
            'missing': sum(v is None for v in values), 'reports': len(values)}


def status_for(actual, rule):
    if actual is None or not rule:
        return 'Not assessed'
    if rule['direction'] == 'higher':
        return 'Exceeding expectations' if actual >= rule['excellent'] else 'On track' if actual >= rule['good'] else 'Needs attention' if actual >= rule['critical'] else 'Critical'
    return 'Exceeding expectations' if actual <= rule['excellent'] else 'On track' if actual <= rule['good'] else 'Needs attention' if actual <= rule['critical'] else 'Critical'


def default_target_rule(metric):
    lower = metric.startswith(('sam', 'mam', 'plw_sam', 'plw_mam'))
    return dict(direction='lower' if lower else 'higher', critical=130 if lower else 70,
                good=100, excellent=80 if lower else 120)


def build_monitoring(connection, args):
    rows = reports(connection)
    months = sorted({r['month'] for r in rows})
    month = args.get('month', '') or (months[-1] if months else '')
    if month:
        try:
            date.fromisoformat(month + '-01')
        except ValueError:
            month = months[-1] if months else ''
    cumulative = args.get('period_mode') == 'cumulative'
    filters = {k: args.get('fh_' + k, '') for k in LOCATIONS}
    options = {}
    pool = rows
    for key in LOCATIONS:
        options[key] = sorted({r[key] for r in pool if r[key]})
        if filters[key]:
            pool = [r for r in pool if r[key] == filters[key]]
    metric = args.get('indicator', 'households_visited')
    if metric not in ALL_METRICS:
        metric = 'households_visited'
    rules = get_setting(connection, 'fh_rules', {})
    target_rules = {**{k: default_target_rule(k) for k in ALL_METRICS},
                    **get_setting(connection, 'fh_target_rules', {})}
    targets = get_setting(connection, 'fh_targets', [])
    totals = {k: aggregate(pool, k, month, cumulative) for k in ALL_METRICS}
    previous = ''
    if month:
        from datetime import timedelta
        first = date.fromisoformat(month + '-01')
        previous = (first - timedelta(days=1)).strftime('%Y-%m') if first > date.min else ''
    comparisons = []
    for key, (label, _, kind) in ALL_METRICS.items():
        current_result = aggregate(pool, key, month)
        previous_result = aggregate(pool, key, previous)
        cur, prev = current_result['value'], previous_result['value']
        partial = bool(current_result['missing'] or previous_result['missing'])
        identity_fields = ('district', 'subcounty', 'parish', 'village', ALL_METRICS[key][1])
        current_units = {tuple(r[k] for k in identity_fields) for r in pool if r['month'] == month and r['grain'] == ALL_METRICS[key][1]}
        previous_units = {tuple(r[k] for k in identity_fields) for r in pool if r['month'] == previous and r['grain'] == ALL_METRICS[key][1]}
        partial = partial or current_units != previous_units
        change = cur-prev if cur is not None and prev is not None and not partial else None
        comparisons.append(dict(key=key, label=label, current=cur, previous=prev, change=change,
                                partial=partial,
                                percent=round(change/prev*100, 1) if change is not None and prev else None))
    rankings = {}
    for grain in ('vht', 'group'):
        groups = defaultdict(list)
        for row in pool:
            if row['grain'] == grain:
                groups[tuple(row[k] for k in ('district', 'subcounty', 'parish', 'village', grain))].append(row)
        selected_metric = metric if ALL_METRICS[metric][1] == grain else ('households_visited' if grain == 'vht' else 'kitchen_gardens')
        entries = []
        for identity, members in groups.items():
            if not any(r['month'] <= month if cumulative else r['month'] == month for r in members):
                continue
            result = aggregate(members, selected_metric, month, cumulative)
            monthly_result = aggregate(members, selected_metric, month)
            entries.append({'name': identity[-1], 'location': ' / '.join(identity[:-1]), **result,
                            'status': status_for(monthly_result['value'], rules.get(selected_metric)) if not monthly_result['missing'] else 'Incomplete data'})
        entries.sort(key=lambda r: (r['value'] is None, (r['value'] or 0) * (1 if args.get('sort') == 'asc' else -1), r['name']))
        rankings[grain] = {'metric': selected_metric, 'rows': entries}
    trend_months = []
    if months and month:
        year, month_number = map(int, months[0].split('-'))
        while (key := f'{year:04d}-{month_number:02d}') <= min(month, months[-1]):
            trend_months.append(key)
            year, month_number = (year+1, 1) if month_number == 12 else (year, month_number+1)
    trend = [{'month': m, **aggregate(pool, metric, m)} for m in trend_months]
    geography = []
    level = args.get('geo_level', 'district')
    if level not in ('district', 'subcounty'): level = 'district'
    for loc in sorted({(r['district'], r['subcounty'] if level == 'subcounty' else '') for r in pool}):
        members = [r for r in pool if r['district'] == loc[0] and (level == 'district' or r['subcounty'] == loc[1])]
        geography.append({'name': ' / '.join(v for v in loc if v), **aggregate(members, metric, month, cumulative)})
    target_results = []
    for target in targets:
        if target['month'] != month or any(filters[k] and target['scope'].get(k) != filters[k] for k in LOCATIONS): continue
        members = [r for r in pool if all(not v or r[k] == v for k,v in target['scope'].items())]
        actual = aggregate(members, target['metric'], month)
        percent = round(actual['value']/target['target']*100, 1) if actual['value'] is not None and target['target'] and not actual['missing'] else None
        target_results.append({**target, 'actual': actual, 'percent': percent,
                               'status': status_for(percent, target_rules[target['metric']]) if not actual['missing'] else 'Incomplete data'})
    selected_rows = [r for r in pool if r['month'] <= month] if cumulative else [r for r in pool if r['month'] == month]
    warnings = list(NOTES)
    if any(r['grain'] == 'vht' and any(r['values'].get('normal_'+s) is None for s in ('male','female')) for r in selected_rows):
        warnings.append('Screening total is incomplete: normal-status counts were not reported for some VHT reports. Explicitly reported screening totals are shown where available; SAM + MAM alone is never used as total screened.')
    return dict(month=month, months=months, previous_month=previous, cumulative=cumulative, filters=filters, options=options,
                metrics=ALL_METRICS, input_metrics=METRICS, totals=totals, comparisons=comparisons, rankings=rankings, trend=trend,
                geography=geography, geo_level=level, indicator=metric, rules=rules, target_rules=target_rules, targets=target_results,
                warnings=warnings, rows=selected_rows, vhts=len({tuple(r[k] for k in ('district','subcounty','parish','village','vht')) for r in selected_rows if r['grain']=='vht'}))


def export_csv(data):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    fields = ['month', 'grain', *LOCATIONS, *METRICS]
    writer.writerow(fields)
    for row in data['rows']:
        values = [row.get(k, row['values'].get(k, '')) for k in fields]
        writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=', '+', '-', '@')) else v for v in values])
    return '\ufeff' + output.getvalue()
