"""Offline OCR adapter and conservative, geometry-based multi-page extraction."""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pvp_stats import LABELS, Field, Sheet, parse_number

# The runtime API is too late to suppress ONNX Runtime's initialization event
# on POSIX. Set the documented process flag before importing onnxruntime.
os.environ.setdefault('ORT_DISABLE_TELEMETRY', '1')


@dataclass(frozen=True)
class Token:
    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float


class Backend(Protocol):
    def read(self, image) -> list[Token]: ...


class LocalOCR:
    def __init__(self):
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        import rapidocr
        from rapidocr import RapidOCR
        model_dir = Path(rapidocr.__file__).parent / 'models'
        names = ('PP-OCRv6_det_small.onnx', 'ch_ppocr_mobile_v2.0_cls_mobile.onnx', 'PP-OCRv6_rec_small.onnx')
        paths = [model_dir / name for name in names]
        if not all(path.is_file() for path in paths):
            raise RuntimeError('Modelli OCR locali mancanti: reinstallare il pacchetto completo.')
        self.engine = RapidOCR(params={
            'Det.model_path': str(paths[0]), 'Cls.model_path': str(paths[1]),
            'Rec.model_path': str(paths[2]), 'Global.log_level': 'error',
            'EngineConfig.onnxruntime.intra_op_num_threads': 2,
            'EngineConfig.onnxruntime.inter_op_num_threads': 1,
        })

    def read(self, image):
        import numpy as np
        result = self.engine(np.asarray(image.convert('RGB'))[:, :, ::-1])
        tokens = []
        if result.boxes is None:
            return tokens
        for box, text, confidence in zip(result.boxes, result.txts, result.scores):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            tokens.append(Token(text, float(confidence), (min(xs)+max(xs))/2,
                                (min(ys)+max(ys))/2, max(xs)-min(xs), max(ys)-min(ys)))
        return tokens


def normalized(text):
    return re.sub(r'[^a-z0-9]', '', text.lower())


def find_anchor(tokens, name):
    """Find a unique high-confidence anchor, allowing OCR to split two words."""
    matches = [t for t in tokens
               if normalized(t.text) == name and t.confidence >= .9]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    ordered = sorted(tokens, key=lambda token: (token.y, token.x))
    joined = []
    for left, right in zip(ordered, ordered[1:]):
        same_line = abs(left.y-right.y) <= max(left.height, right.height) * .6
        close = 0 <= right.x-left.x <= (left.width+right.width) * 1.2
        if (same_line and close and left.confidence >= .9 and right.confidence >= .9
                and normalized(left.text+right.text) == name):
            joined.append(Token(
                left.text+' '+right.text, min(left.confidence, right.confidence),
                (left.x+right.x)/2, (left.y+right.y)/2,
                abs(right.x-left.x)+(left.width+right.width)/2,
                max(left.height, right.height),
            ))
    return joined[0] if len(joined) == 1 else None


def readings_agree(label, old, new):
    """Overlapping captures can format one value differently (trailing zeros,
    dropped thousands separators, glued arrow glyphs); compare the parsed
    numbers instead of the raw text."""
    percent = LABELS.index(label) >= 8
    try:
        return parse_number(old,percent) == parse_number(new,percent)
    except ValueError:
        return False


def combine_fields(label, old, new):
    """Merge a re-read of one row into the stored field.

    Equal readings never escalate to a conflict whatever their formatting, a
    glitchy re-read never invalidates a recognized value, and a fresh
    recognized reading that agrees with a stored conflict resolves it. Only
    two valid readings that really parse to different numbers stay conflicted
    for manual verification."""
    if old.status == 'conflict':
        if new.status == 'recognized' and readings_agree(label,old.text,new.text):
            return new
        return old
    if old.text == new.text:
        if old.status == 'recognized' and new.status != 'recognized':
            return old
        return new if new.confidence > old.confidence else old
    if readings_agree(label,old.text,new.text):
        keep = old if (old.status == 'recognized' or new.status != 'recognized') else new
        return Field(keep.text,max(old.confidence,new.confidence),keep.status)
    if old.status == 'recognized' and new.status != 'recognized':
        return old
    return Field(old.text,min(old.confidence,new.confidence),'conflict')


def candidates(label, section):
    value = normalized(label)
    aliases = {
        'counterrate': 'counterattackrate',
        'counterdamage': 'counterattackdamage',
        'counterchancedown': 'counterattackchancedown',
        'evaderate': 'evasionrate',
        'supporterdamage': 'supportdamage',
        'supporterdamageup': 'supportdamageup',
        'supporterdamageresist': 'supportdamageresist',
    }
    value = aliases.get(value, value)
    if section == 'combined' and value in ('atk','def','hp','attackspeed'):
        value = 'combined' + value
    result = [name for name in LABELS if normalized(name) == value]
    if value.startswith('supportdamage'):
        legacy = 'normalattackdamage' + value[len('supportdamage'):]
        result += [name for name in LABELS if normalized(name) == legacy]
    return result


@dataclass
class Page:
    rows: dict
    final: bool
    visible_labels: tuple[str, ...] = ()
    swipe: 'SwipeGeometry | None' = None
    rewind: 'SwipeGeometry | None' = None


@dataclass(frozen=True)
class SwipeGeometry:
    x: int
    start_y: int
    end_y: int


def merged_label_tokens(tokens, label_limit):
    """Unisci in un solo token le etichette andate a capo su due righe.

    Il pannello spezza le etichette lunghe (per esempio 'Skill Critical
    Damage DOWN' renderizzato come 'Skill Critical Damage' + 'DOWN'): la
    prima riga da sola coincide con un'altra statistica canonica, quindi
    senza la fusione i valori finirebbero sulla statistica sbagliata. La
    coppia viene fusa quando le due righe sono vicine, allineate a sinistra
    e la loro concatenazione produce una statistica canonica; la geometria
    risultante copre entrambe le righe così i valori centrati sul blocco
    restano nel match. Le righe separate restano distinte: il distacco tra
    due righe supera la soglia verticale e concatenazioni tra etichette
    diverse non producono mai un nome canonico."""
    label_tokens = sorted((t for t in tokens if t.x < label_limit), key=lambda t: t.y)
    merged, consumed = [], set()
    for i, left in enumerate(label_tokens):
        if id(left) in consumed:
            continue
        right = label_tokens[i+1] if i+1 < len(label_tokens) else None
        if (right is not None and id(right) not in consumed
                and 0 < right.y-left.y <= (left.height+right.height)*.75
                and abs(right.x-left.x) <= max(left.width, right.width)*.5
                and candidates(left.text+' '+right.text, None)):
            top = min(left.y-left.height/2, right.y-right.height/2)
            bottom = max(left.y+left.height/2, right.y+right.height/2)
            merged.append(Token(
                left.text+' '+right.text, min(left.confidence, right.confidence),
                min(left.x, right.x), (top+bottom)/2,
                max(left.x+left.width, right.x+right.width)-min(left.x, right.x),
                bottom-top))
            consumed.update((id(left), id(right)))
            continue
        merged.append(left)
    return merged


def parse_page(tokens):
    """Require title and two column anchors on every capture; never infer columns."""
    def anchor(name):
        match = find_anchor(tokens, name)
        if match is None:
            raise ValueError('Apri Detailed Stats con intestazioni You e Target visibili.')
        return match
    title, you, target = anchor('detailedstats'), anchor('you'), anchor('target')
    if not (you.x < target.x and title.y < min(you.y, target.y) and abs(you.y-target.y) <= max(you.height,target.height)):
        raise ValueError('Geometria Detailed Stats non riconosciuta.')
    gap = target.x-you.x
    label_limit = you.x-gap*.35
    section, raw_rows = None, []
    for label in merged_label_tokens(tokens, label_limit):
        if normalized(label.text) == 'combinedstats':
            section = 'combined'
            continue
        if normalized(label.text) == 'basicstats':
            section = 'basic'
            continue
        if label.y <= max(you.y,target.y):
            continue
        if label.x >= label_limit:
            continue
        choices = candidates(label.text, section)
        if not choices:
            continue
        values = []
        for head in (you,target):
            matches = [t for t in tokens if abs(t.x-head.x) < gap*.42
                       and abs(t.y-label.y) < max(label.height,t.height)*.6
                       and t is not label and normalized(t.text) not in ('',)]
            # Standalone colored arrow glyphs are not number candidates.
            matches = [t for t in matches if re.search(r'[0-9O]', t.text)]
            if len(matches) != 1:
                values.append(Field(status='suspicious'))
                continue
            t = matches[0]
            text = t.text.strip().strip('↑↓▲▼ ').strip()
            confidence = min(t.confidence,label.confidence)
            try:
                parse_number(text, LABELS.index(choices[0]) >= 8)
                status = 'recognized' if confidence >= .95 else 'suspicious'
            except ValueError:
                status = 'suspicious'
            values.append(Field(text,confidence,status))
        raw_rows.append([choices,values,label,section])
    # Resolve historical Support labels only from unambiguous order anchors
    # within this page. An isolated Support row remains unknown.
    rows = {}
    visible = []
    for i, (choices,values,label_token,row_section) in enumerate(raw_rows):
        if len(choices) > 1:
            before = [LABELS.index(r[0][0]) for r in raw_rows[:i] if len(r[0]) == 1]
            after = [LABELS.index(r[0][0]) for r in raw_rows[i+1:] if len(r[0]) == 1]
            lo, hi = max(before,default=-1), min(after,default=len(LABELS))
            choices = [c for c in choices if lo < LABELS.index(c) < hi]
        if len(choices) == 1:
            name = choices[0]
            if name in rows:
                previous = rows[name]
                rows[name] = tuple(combine_fields(name,p,v) for p,v in zip(previous,values))
            else:
                rows[name] = tuple(values)
            visible.append((name, label_token.y, row_section))
    if len(rows) < 2:
        raise ValueError('Righe insufficienti: cattura una pagina con più statistiche visibili.')
    ordered_visible = []
    for name, y, row_section in sorted(visible, key=lambda item: item[1]):
        if name not in [item[0] for item in ordered_visible]:
            ordered_visible.append((name, y, row_section))
    swipe = rewind = None
    # The Combined panel is fixed while the lower list scrolls. Section
    # headings such as Basic/Advanced can leave the viewport, so section state
    # alone cannot identify the scrollable rows on later pages. Canonical
    # Combined names remain unambiguous on every capture.
    scrollable_visible = [
        item for item in ordered_visible if not item[0].startswith('Combined ')
    ]
    if len(scrollable_visible) >= 2:
        rewind = SwipeGeometry(
            round(target.x), round(scrollable_visible[0][1]),
            round(scrollable_visible[-1][1]))
    if len(scrollable_visible) >= 6:
        # Move the penultimate visible row to the third row's position. The
        # last two rows therefore remain visible after the scroll and provide
        # overlap without relying on a particular framebuffer resolution.
        start_y = round(scrollable_visible[-2][1])
        end_y = round(scrollable_visible[2][1])
        if start_y > end_y:
            swipe = SwipeGeometry(round(target.x), start_y, end_y)
    return Page(
        rows, all(name in rows for name in LABELS[-3:]),
        tuple(name for name, _, _ in ordered_visible), swipe, rewind,
    )


class CaptureSession:
    def __init__(self):
        self.sheets = (Sheet(),Sheet())
        self.pages = 0
        self.final = False

    def merge(self, page):
        added = 0
        for label, pair in page.rows.items():
            for sheet, new in zip(self.sheets,pair):
                old = sheet.fields[label]
                if old.status == 'manual':
                    continue
                if old.status == 'missing':
                    added += 1
                    sheet.fields[label] = new
                else:
                    sheet.fields[label] = combine_fields(label,old,new)
        self.pages += 1
        self.final |= page.final
        return added


def self_test():
    """Execute all bundled models on a local synthetic image; never network/ADB."""
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB',(760,140),'white')
    draw = ImageDraw.Draw(image)
    draw.text((20,35),'Detailed Stats 12345',font=ImageFont.load_default(size=36),fill='black')
    tokens = LocalOCR().read(image)
    return any('12345' in t.text for t in tokens)
