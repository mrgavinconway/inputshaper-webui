"""Generate a reviewable Klipper snippet from supported shaper recommendations."""
import math

SUPPORTED = {'zv', 'zvd', 'mzv', 'ei', '2hump_ei', '3hump_ei'}


def build_config(result, selections=None, include_damping=True):
    if result.get('test_type', result.get('settings', {}).get('test_type', 'shaper')) != 'shaper':
        raise ValueError('printer.cfg settings require an input-shaper analysis, not belt or other test data.')
    selections = selections or {}
    lines = ['[input_shaper]']
    choices, warnings, included = {}, [], []
    for axis in ('X', 'Y'):
        measurements = [m for m in result['measurements'] if m.get('excitation') == axis]
        if len(measurements) != 1:
            warnings.append(f'{axis}: no unique measurement; settings omitted.')
            continue
        measurement = measurements[0]
        filters = [f for f in measurement.get('filters', []) if f['type'] in measurement.get('choices', [])
                   and f['type'].lower() in SUPPORTED and math.isfinite(f['frequency']) and f['frequency'] > 0]
        if measurement.get('quality', {}).get('status') == 'invalid' or not filters:
            warnings.append(f'{axis}: no usable shaper recommendation; settings omitted.')
            continue
        # Preserve the engine's recommendation order, not the filter-table order.
        filters.sort(key=lambda f: measurement['choices'].index(f['type']))
        choices[axis] = [{'type': f['type'], 'frequency': f['frequency']} for f in filters]
        selected = selections.get(axis) or filters[0]['type']
        fitted = next((f for f in filters if f['type'] == selected), None)
        if fitted is None:
            raise ValueError(f'{axis}: choose one of the recommended shapers for this recording.')
        name = axis.lower()
        lines.extend([f'shaper_freq_{name}: {fitted["frequency"]:.1f}', f'shaper_type_{name}: {fitted["type"].lower()}'])
        damping = measurement.get('damping')
        if include_damping:
            if measurement.get('damping_status') == 'estimated' and isinstance(damping, (int, float)) and math.isfinite(damping) and 0 < damping < 1:
                lines.append(f'damping_ratio_{name}: {damping:.3f}')
            else:
                warnings.append(f'{axis}: damping was not estimated reliably; damping_ratio_{name} omitted.')
        if measurement.get('quality', {}).get('warnings'):
            warnings.append(f'{axis}: review the recording quality notes before using these settings.')
        included.append(axis)
    if not included:
        raise ValueError('No unique, usable X/Y shaper recommendations are available.')
    return {'text': '\n'.join(lines) + '\n', 'choices': choices, 'axes': included,
            'complete': included == ['X', 'Y'], 'warnings': warnings,
            'qualification': 'Merge into the existing [input_shaper] section. Damping values, when included, are Shake&Tune estimates; Klipper normally uses 0.1 by default.'}
