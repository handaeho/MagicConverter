"""Resolve explicit Word tracking/scale through bounded style inheritance."""
from docx.oxml.ns import qn
from magic_converter.core.jobs.job_control import check_cancelled

_CONTEXTUAL_RUN_LAYOUT = {qn('w:' + tag) for tag in (
    'rtl', 'cs', 'position', 'fitText', 'vertAlign', 'eastAsianLayout', 'snapToGrid')}
_CONTEXTUAL_PARAGRAPH_LAYOUT = {qn('w:' + tag) for tag in ('bidi', 'textDirection', 'snapToGrid')}
_FONT_STYLE = {qn('w:' + tag) for tag in ('b', 'i', 'bCs', 'iCs', 'smallCaps')}
_WORD_2010 = '{http://schemas.microsoft.com/office/word/2010/wordml}'


class WordCharacterMetrics:
    def __init__(self, styles, warnings):
        self.warnings = warnings
        self.styles = {identity: node for node in styles.findall(qn('w:style'))
                       if (identity := node.get(qn('w:styleId')))}
        self.defaults = {node.get(qn('w:type')): identity for identity, node in self.styles.items()
                         if node.get(qn('w:default')) in {'1', 'true', 'on'}}
        self.cache = {}
        self.document = styles.find(qn('w:docDefaults'))
        if self.document is not None:
            self.document = self.document.find(qn('w:rPrDefault'))
            if self.document is not None:
                self.document = self.document.find(qn('w:rPr'))

    def warn(self, text):
        if text not in self.warnings:
            self.warnings.append(text)

    def chain(self, identity):
        if identity in self.cache:
            return self.cache[identity]
        nodes, seen, current = [], set(), identity
        while current in self.styles:
            check_cancelled()
            if current in seen or len(seen) >= 64:
                self.warn('DOCX 문자 스타일 상속이 순환하거나 깊이 한도를 넘어 일부 속성을 생략합니다.')
                break
            seen.add(current)
            style = self.styles[current]
            nodes.append((style.find(qn('w:rPr')), style.find(qn('w:pPr'))))
            base = style.find(qn('w:basedOn'))
            current = base.get(qn('w:val')) if base is not None else None
        if len(self.cache) < 512:
            self.cache[identity] = nodes
        return nodes

    def __call__(self, run, paragraph):
        props = run._r.rPr
        char = props.find(qn('w:rStyle')) if props is not None else None
        pprops = paragraph._p.pPr
        para = pprops.find(qn('w:pStyle')) if pprops is not None else None
        char_id = char.get(qn('w:val')) if char is not None else self.defaults.get('character')
        para_id = para.get(qn('w:val')) if para is not None else self.defaults.get('paragraph')
        char_styles, para_styles = self.chain(char_id), self.chain(para_id)
        layers = [props, *(rpr for rpr, _ in (*char_styles, *para_styles)), self.document]
        found = {}
        for layer in layers:
            if layer is None:
                continue
            for tag in ('spacing', 'w'):
                node = layer.find(qn('w:' + tag))
                if tag not in found and node is not None:
                    found[tag] = node
            if len(found) == 2:
                break
        result = {}
        if (run.font.name or '').casefold() == 'tahoma':
            fonts = props.find(qn('w:rFonts'))
            paragraph_layers = [pprops, *(ppr for _, ppr in para_styles)]
            font_style = _FONT_STYLE | {qn('w:caps')} if any('a' <= c <= 'z' for c in run.text) else _FONT_STYLE
            # The verified case uses a named, regular face and direct size in
            # left-to-right paragraphs with no unmodeled layout modifiers.
            incompatible = (
                fonts is None or fonts.get(qn('w:hAnsi'), '').casefold() != 'tahoma'
                or any(qn('w:' + key) in fonts.attrib for key in ('asciiTheme', 'hAnsiTheme'))
                or props.find(qn('w:sz')) is None
                or any(node.tag in _CONTEXTUAL_PARAGRAPH_LAYOUT
                       for layer in paragraph_layers if layer is not None for node in layer)
                or any(node.tag == qn('w:tbl') for node in paragraph._p.iterancestors())
                or any(node.tag in _CONTEXTUAL_RUN_LAYOUT or node.tag.startswith(_WORD_2010)
                       or (node.tag in font_style and node.get(qn('w:val')) not in {'0', 'false', 'off'})
                       for layer in layers if layer is not None for node in layer)
            )
            if incompatible:
                result['proportional_tracking_allowed'] = False
        if 'w' in found:
            try:
                scale = int(found['w'].get(qn('w:val')))
                if not 50 <= scale <= 200:
                    raise ValueError('Unsupported scale')
                result['width_percent'] = scale
            except (TypeError, ValueError):
                self.warn('일부 DOCX 장평은 HWPX 범위를 벗어나 기본값으로 보존됩니다.')
        if 'spacing' in found:
            try:
                points = int(found['spacing'].get(qn('w:val'), '0')) / 20
                if abs(points) > 1_000_000:
                    raise ValueError('Character spacing too large')
                result['spacing_pt'] = points
            except (TypeError, ValueError):
                self.warn('일부 DOCX 고정 pt 자간이 유효하지 않아 기본값으로 보존됩니다.')
        return result
