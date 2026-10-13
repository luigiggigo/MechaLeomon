"""Canonical Detailed Stats order and strict, locale-independent sheet parsing."""
import re
from dataclasses import dataclass
from decimal import Decimal

LABELS = tuple('''Combined ATK|Combined DEF|Combined HP|Combined Attack Speed|ATK|DEF|HP|Attack Speed|ATK Bonus|DEF Bonus|HP Bonus|Attack Speed Bonus|Critical Rate|Critical Damage|Critical Damage DOWN|Skill Critical Rate|Skill Critical Damage|Skill Critical Damage DOWN|Combo Rate|Combo Chance DOWN|Counterattack Rate|Counterattack Chance DOWN|Stun Rate|Stun Chance DOWN|Evasion Rate|Evasion Rate DOWN|Airborne Rate|Disregard Airborne|Damage DOWN|Normal Attack Damage|Normal Attack Damage DOWN|Skill Damage|Skill Damage DOWN|Boss Damage|Boss Damage DOWN|Combo Damage|Combo Damage DOWN|Counterattack Damage|Counterattack Damage DOWN|Support Damage|Support Damage DOWN|Normal Attack Damage UP|Normal Attack Damage RESIST|Skill Damage UP|Skill Damage RESIST|Combo Damage UP|Combo Damage RESIST|Counterattack Damage UP|Counterattack Damage RESIST|Critical Damage UP|Critical Damage RESIST|Support Damage UP|Support Damage RESIST|HP Recovery|HP Recovery Rate|HP Recovery Amount'''.split('|'))
OPTIONAL = set(LABELS[3:12]) | {'Combo Chance DOWN', 'Stun Rate', 'Stun Chance DOWN', 'Airborne Rate', 'Disregard Airborne', 'Boss Damage', 'Boss Damage DOWN'}
REQUIRED = tuple(label for label in LABELS if label not in OPTIONAL)
NUMBER = re.compile(r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?%?\Z')


def parse_number(text: str, percent: bool = False) -> Decimal:
    text = text.strip()
    if not NUMBER.fullmatch(text) or (text.endswith('%') and not percent):
        raise ValueError('Numero non valido: usare 1,234.56 oppure 12.34%')
    value = Decimal(text.rstrip('%').replace(',', ''))
    return value / 100 if percent else value


@dataclass(frozen=True)
class Field:
    text: str = ''
    confidence: float = 0.0
    status: str = 'missing'

    def value(self, label):
        if self.status not in {'recognized', 'manual'}:
            raise ValueError('Valore da verificare')
        return parse_number(self.text, LABELS.index(label) >= 8)


class Sheet:
    def __init__(self):
        self.fields = {label: Field() for label in LABELS}

    def correct(self, label, text):
        parse_number(text, LABELS.index(label) >= 8)
        self.fields[label] = Field(text, 1.0, 'manual')

    def values(self):
        values, errors = {}, []
        for label in REQUIRED:
            try:
                values[label] = self.fields[label].value(label)
            except ValueError:
                errors.append(label)
        if errors:
            raise ValueError('Correggere/confermare: ' + ', '.join(errors))
        if values['Combined HP'] <= 0:
            raise ValueError('Combined HP deve essere positivo')
        return values
