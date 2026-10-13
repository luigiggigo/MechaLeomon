"""Pure static PvP math. Data Lab combat/formulas verified 2026-09-18.

https://digiupdata.com/reference/combat
https://digiupdata.com/research/formulas
No intermediate rounding; sheet percentages enter as fractions.
"""
from decimal import Decimal, localcontext

from pvp_stats import REQUIRED

ONE = Decimal(1)
TYPES = ('Normal Attack', 'Skill', 'Combo', 'Counterattack', 'Support')
COMBO_NOTE = 'Combo Chance DOWN is not included because its proc application is not currently confirmed by Data Lab.'
LIMITS = ('Static sheet comparison only. Skill Cards, Buddies, temporary buffs, shields, '
          'Ignore Damage, opener effects, skill coefficients/cooldowns and other live battle state can change the real result.')


def clamp(value):
    return max(Decimal(0), min(ONE, value))


def rounded(value):
    return max(1, int(value)) if value > 0 else 0


def hits_to_kill(hp, damage):
    if damage <= 0:
        return None
    return int((hp / damage).to_integral_value(rounding='ROUND_CEILING'))


def compare(attacker, defender, coefficient=ONE):
    """Outgoing packets; counter chance belongs to this attacker on receiving a hit."""
    for sheet in (attacker, defender):
        for key in REQUIRED:
            if key not in sheet or not isinstance(sheet[key], Decimal) or not sheet[key].is_finite() or sheet[key] < 0:
                raise ValueError(f'Missing/invalid stat: {key}')
        if sheet['Combined HP'] <= 0:
            raise ValueError('Combined HP must be positive')
    coefficient = Decimal(str(coefficient))
    if not coefficient.is_finite() or coefficient <= 0:
        raise ValueError('Skill coefficient must be positive')
    with localcontext() as ctx:
        ctx.prec = 60
        a, d = attacker, defender
        atk = a['Combined ATK']
        base = atk * atk / (atk + d['Combined DEF']) if atk else Decimal(0)
        crit = (Decimal('1.5') + max(0, a['Critical Damage'] - d['Critical Damage DOWN'])) * (1 + max(0, a['Critical Damage UP'] - d['Critical Damage RESIST']))
        skill_crit = Decimal('1.5') + max(0, a['Skill Critical Damage'] - d['Skill Critical Damage DOWN'])
        evasion = clamp(d['Evasion Rate'] - a['Evasion Rate DOWN'])
        result = {'base': base, 'evasion': evasion, 'hit_chance': 1-evasion,
                  'counter_chance': clamp(a['Counterattack Rate'] - d['Counterattack Chance DOWN']),
                  'combo_rate': clamp(a['Combo Rate']), 'target_hp': d['Combined HP'],
                  'timed_heal': a['Combined HP'] * a['HP Recovery'],
                  'heal_chance': clamp(a['HP Recovery Rate']),
                  'heal_amount': a['Combined HP'] * a['HP Recovery Amount'], 'attacks': {}}
        for kind in TYPES:
            multiplier = 1 + max(0, a[kind+' Damage'] - d[kind+' Damage DOWN'] - d['Damage DOWN'])
            amplify = 1 + max(0, a[kind+' Damage UP'] - d[kind+' Damage RESIST'])
            damage = base * multiplier * amplify
            if kind == 'Skill':
                damage *= coefficient
            if kind == 'Support':
                damage *= 1 + a['Normal Attack Damage']
            normal, critical = rounded(damage), rounded(damage * (skill_crit if kind == 'Skill' else crit))
            result['attacks'][kind] = {'noncrit': normal, 'crit': critical,
                'crit_chance': clamp(a['Skill Critical Rate' if kind == 'Skill' else 'Critical Rate']),
                'hits': hits_to_kill(d['Combined HP'], normal), 'crit_hits': hits_to_kill(d['Combined HP'], critical),
                'one_shot': normal >= d['Combined HP'], 'crit_one_shot': critical >= d['Combined HP'],
                'hit_chance': ONE if kind == 'Counterattack' else 1-evasion,
                'type_cancelled': (a[kind+' Damage'] > 0 and multiplier == 1)}
        return result


def abbreviated(value):
    for size, suffix in ((10**12, 'T'), (10**9, 'B'), (10**6, 'M'), (10**3, 'K')):
        if abs(value) >= size:
            return f'{value / size:.2f}{suffix}'
    return f'{value:,.2f}' if isinstance(value, Decimal) and value != int(value) else f'{int(value):,}'


def report(you, target, coefficient=ONE):
    sides = (compare(you, target, coefficient), compare(target, you, coefficient))
    lines = ['Sheet/static damage before unknown shield/Ignore Damage effects',
             f'Skill coefficient: {coefficient}', '', f'{"Metric":35} {"You -> Target":>24} {"Target -> You":>24}']
    def row(name, values, probability=False):
        rendered = [f'{v*100:.2f}%' if probability else f'{abbreviated(v)} [{v}]' for v in values]
        lines.append(f'{name:35} {rendered[0]:>24} {rendered[1]:>24}')
    for key, label in [('base','BaseAfterDefense'), ('target_hp','Target HP'), ('evasion','Effective Evasion'), ('hit_chance','Hit Chance')]:
        row(label, [s[key] for s in sides], key in ('evasion','hit_chance'))
    for kind in TYPES:
        for key in ('noncrit', 'crit', 'crit_chance', 'hits', 'crit_hits', 'one_shot', 'crit_one_shot'):
            vals = [s['attacks'][kind][key] for s in sides]
            if key in ('hits','crit_hits'):
                shown = [('N/A' if value is None else str(value)) for value in vals]
                lines.append(f'{kind+" "+key:35} {shown[0]:>24} {shown[1]:>24}')
            elif key in ('one_shot','crit_one_shot'):
                shown = [('YES' if value else 'NO') for value in vals]
                lines.append(f'{kind+" "+key:35} {shown[0]:>24} {shown[1]:>24}')
            else:
                row(kind+' '+key, vals, key == 'crit_chance')
    labels = {
        'combo_rate': 'Combo Rate',
        'counter_chance': 'Effective Counter chance',
        'timed_heal': 'Timed HP Recovery amount',
        'heal_chance': 'On-hit heal chance',
        'heal_amount': 'On-hit heal amount',
    }
    for key in labels:
        row(labels[key], [s[key] for s in sides],
            key in ('combo_rate','counter_chance','heal_chance'))
    lines += ['', 'Matchup summary']
    for name, s in zip(('You', 'Target'), sides):
        hits = s['attacks']['Normal Attack']['crit_hits']
        if s['attacks']['Normal Attack']['crit_one_shot']:
            lines.append(f'{name}: a connected normal crit one-shots the opposing side.')
        else:
            lines.append(f'{name}: needs {hits if hits is not None else "an unlimited number of"} connected normal crits from full HP.')
        lines.append(f'{name}: effective hit chance {s["hit_chance"]*100:.2f}%.')
        if s['attacks']['Skill']['type_cancelled']:
            lines.append(f'{name}: Skill Damage bonus is completely cancelled.')
    if not any(s['counter_chance'] for s in sides):
        lines.append('Neither side currently has positive Counter chance.')
    you_one_shot = sides[0]['attacks']['Normal Attack']['crit_one_shot']
    target_one_shot = sides[1]['attacks']['Normal Attack']['crit_one_shot']
    if you_one_shot != target_one_shot:
        edge = 'YOU' if you_one_shot else 'TARGET'
        reason = 'only that side has a connected normal-crit one-shot'
    else:
        edge = 'INCONCLUSIVE'
        reason = 'the displayed static checks do not establish a unique winner'
    lines.append(f'Static matchup edge: {edge} — {reason}.')
    lines += ['', COMBO_NOTE,
        'Counter chance: column attacker retaliates only after an eligible incoming hit connects. Counter hits cannot be evaded.',
        'Recovery: column attacker heals after their own connected hit; timed heal is separate.',
        'Hits to kill assume connected hits, no healing or shields; no cooldown/DPS prediction.', LIMITS]
    return '\n'.join(lines)
