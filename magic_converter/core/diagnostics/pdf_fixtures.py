"""Small PDF inputs shared by self-tests and release regression tools."""
import pymupdf as fitz


def dense_table_pdf(path):
    from magic_converter.core.pdf.pdf_fonts import resolve_font
    names = ('Arial', 'Times New Roman', 'Calibri', 'Malgun Gothic', 'Batang', 'Segoe UI')
    faces = [resolve_font(name) for name in names]
    if not all(faces):
        raise RuntimeError('Installed dense-table control fonts are required: ' + ', '.join(names))
    with fitz.open() as pdf:
        page = pdf.new_page(width=660, height=460)
        for x in (20, 225, 430, 640):
            page.draw_line((x, 20), (x, 420), width=.5)
        for y in (20, 220, 420):
            page.draw_line((20, y), (640, y), width=.5)
        for index, face in enumerate(faces):
            key = 'font' + str(index)
            page.insert_font(fontname=key, fontfile=str(face.path))
            x, top = (32, 237, 442)[index % 3], 20 + (index // 3) * 200
            for line in range(5):
                page.insert_text((x, top + 24 + line * 18), f'Cell{index} line{line} WWW iii',
                                 fontname=key, fontsize=12)
        page.insert_text((24, 445), 'Independent text outside the table', fontsize=10)
        pdf.save(path)


def glyph_scale_pdf(path):
    with fitz.open() as doc:
        page = doc.new_page(width=400, height=240)
        for index, scale in enumerate((1, .65, 1.4)):
            origin = fitz.Point(30, 60 + index * 65)
            page.insert_text(origin, 'Glyph widths WWW iii', fontsize=20,
                             morph=(origin, fitz.Matrix(scale, 1)))
        page = doc.new_page(width=400, height=240)
        page.insert_text((30, 60), 'Independent before', fontsize=20)
        page.insert_text((30, 125), 'Outline glyphs', fontsize=20, render_mode=2)
        page.insert_text((30, 190), 'Independent after', fontsize=20)
        doc.save(path)


def glyph_alpha_pdf(path):
    """Nonoverlapping translucent fill/stroke on an independently editable backdrop."""
    with fitz.open() as doc:
        page = doc.new_page(width=400, height=280)
        page.draw_rect(fitz.Rect(15, 55, 385, 255), color=None, fill=(.8, .9, 1))
        page.insert_text((25, 30), 'Independent title', fontsize=12, fill_opacity=.5)
        for index, (mode, opacity) in enumerate(((0, .25), (1, .5), (2, .75))):
            point = fitz.Point(30, 105 + index * 65)
            page.insert_text(point, 'A B C W', fontsize=36, render_mode=mode,
                             color=(.1, .2, .4), fill=(.6, .1, .2),
                             stroke_opacity=opacity, fill_opacity=opacity,
                             morph=(point, fitz.Matrix(.8 if mode == 0 else 1, 1)))
        doc.save(path)


def overlapping_glyph_pdf(path):
    """One translucent text paint with overlapping glyphs and a separate title."""
    with fitz.open() as doc:
        page = doc.new_page(width=220, height=160)
        page.draw_rect((30, 40, 105, 105), color=None, fill=(.8, .9, 1))
        page.insert_text((20, 25), 'Independent title', fontsize=12)
        before = page.read_contents()
        point = fitz.Point(40, 90)
        page.insert_text(point, 'WW', fontsize=60, fill_opacity=.5,
                         morph=(point, fitz.Matrix(.7, 1)))
        # Only the last text object gets the overlapping character advance.
        content = before + page.read_contents()[len(before):].replace(b'BT', b'BT -48 Tc')
        stream = doc.get_new_xref(); doc.update_object(stream, '<<>>')
        doc.update_stream(stream, content); page.set_contents(stream)
        doc.save(path)


def tight_table_pdf(path):
    from magic_converter.core.pdf.pdf_fonts import resolve_font
    face = resolve_font('Arial')
    if face is None:
        raise RuntimeError('Arial is required for the tight-cell control')
    with fitz.open() as doc:
        page = doc.new_page(width=560, height=240)
        page.insert_font(fontname='local', fontfile=str(face.path))
        for x in (20, 280, 540):
            page.draw_line((x, 30), (x, 210), width=.5)
        for y in (30, 120, 210):
            page.draw_line((20, y), (540, y), width=.5)
        for x, text, ys in ((32, 'Tight line ', (45, 57, 69)),
                            (292, 'Normal line ', (60, 95)),
                            (32, 'Second dense ', (137, 149, 161)),
                            (292, 'Control ', (150, 185))):
            for index, y in enumerate(ys):
                page.insert_text((x, y), text + str(index) + ' WWW iii',
                                 fontname='local', fontsize=12)
        page.insert_text((24, 232), 'Independent outside', fontsize=10)
        doc.save(path)


def effect_pdf(path, *, blend=True, repeat=False):
    with fitz.open() as doc:
        page = doc.new_page(width=360, height=240)
        page.insert_text((220, 30), 'Independent before', fontsize=11)
        before = page.read_contents()
        form = doc.get_new_xref()
        doc.update_object(form, '<< /Type /XObject /Subtype /Form /BBox [0 0 360 240] '
                          '/Group << /S /Transparency /I true >> /Resources <<>> >>')
        doc.update_stream(form, b'0 0 1 rg 60 60 120 100 re f')
        # Keep the text font resources, and add local graphics state/forms.
        resources = int(doc.xref_get_key(page.xref, 'Resources')[1].split()[0])
        doc.xref_set_key(resources, 'XObject', f'<< /Fm {form} 0 R >>')
        mode = '/Multiply' if blend else '/Normal'
        doc.xref_set_key(resources, 'ExtGState', f'<< /A << /ca .5 >> /Blend << /BM {mode} /ca .5 >> >>')
        stream = doc.get_new_xref()
        doc.update_object(stream, '<<>>')
        content = before + b' q /A gs 1 0 0 rg 20 50 140 110 re f Q q /Blend gs /Fm Do Q'
        if repeat:
            content += b' q 1 0 0 1 20 0 cm /Blend gs /Fm Do Q'
        doc.update_stream(stream, content)
        page.set_contents(stream)
        page.insert_text((220, 210), 'Independent after', fontsize=11)
        doc.save(path)


def isolated_effect_pdf(path, *, isolated=True, blend=True):
    """A nested multiply depends on its isolated parent, not the page ink."""
    with fitz.open() as doc:
        page = doc.new_page(width=360, height=240)
        page.draw_rect((20, 55, 190, 195), color=None, fill=(.7, .85, 1))
        page.insert_text((210, 30), 'Independent title', fontsize=11)
        before = page.read_contents()
        child = doc.get_new_xref()
        doc.update_object(child, '<< /Type /XObject /Subtype /Form /BBox [0 0 360 240] '
                          '/Group << /S /Transparency /I true >> /Resources <<>> >>')
        doc.update_stream(child, b'0 0 1 rg 70 55 110 95 re f')
        parent = doc.get_new_xref()
        doc.update_object(parent, '<< /Type /XObject /Subtype /Form /BBox [0 0 360 240] '
                          f'/Group << /S /Transparency /I {str(isolated).lower()} >> '
                          f'/Resources << /XObject << /Child {child} 0 R >> '
                          '/ExtGState << /A << /ca .5 >> /B << /ca .6 /BM /'
                          + ('Multiply' if blend else 'Normal') + ' >> >> >> >>')
        doc.update_stream(parent, b'q /A gs 1 0 0 rg 35 65 115 105 re f Q q /B gs /Child Do Q')
        resources = int(doc.xref_get_key(page.xref, 'Resources')[1].split()[0])
        doc.xref_set_key(resources, 'XObject', f'<< /Parent {parent} 0 R >>')
        stream = doc.get_new_xref()
        doc.update_object(stream, '<<>>')
        doc.update_stream(stream, before + b' /Parent Do')
        page.set_contents(stream)
        doc.save(path)



def clipped_sequence_pdf(path, clip, *, type3=False, mask=False, type3_program=None, text_program=None):
    with fitz.open() as pdf:
        page = pdf.new_page(width=360, height=240)
        page.insert_text((200, 30), 'Independent title', fontsize=12)
        resources = int(pdf.xref_get_key(page.xref, 'Resources')[1].split()[0])
        if mask:
            form = pdf.get_new_xref()
            pdf.update_object(form, '<< /Type /XObject /Subtype /Form /BBox [0 0 360 240] '
                '/Group << /S /Transparency /CS /DeviceGray >> /Resources <<>> >>')
            pdf.update_stream(form, b'0.5 g 0 0 360 240 re f')
            if mask == 'gradient':
                shading = pdf.get_new_xref()
                pdf.update_object(shading, '<< /ShadingType 2 /ColorSpace /DeviceGray /Coords [20 0 44 0] '
                    '/Function << /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [1] /N 1 >> /Extend [true true] >>')
                pdf.xref_set_key(form, 'Resources', f'<< /Shading << /Fade {shading} 0 R >> >>')
                pdf.update_stream(form, b'/Fade sh')
            elif mask in {'tiled','tile-overlap','tile-gap','tile-different'}:
                programs={'tiled':b'0.5 g 0 0 30 240 re f 30 0 330 240 re f',
                          'tile-overlap':b'0.5 g 0 0 32 240 re f 28 0 332 240 re f',
                          'tile-gap':b'0.5 g 0 0 29.999 240 re f 30 0 330 240 re f',
                          'tile-different':b'0.5 g 0 0 30 240 re f 0.8 g 30 0 330 240 re f'}
                pdf.update_stream(form,programs[mask])
            pdf.xref_set_key(resources, 'ExtGState',
                f'<< /SM << /SMask << /S /Luminosity /G {form} 0 R >> >> >>')
        if type3:
            proc = pdf.get_new_xref(); pdf.update_object(proc, '<<>>')
            pdf.update_stream(proc, type3_program if type3_program is not None else
                              b'600 0 0 0 600 700 d1 0 0 m 600 0 l 300 700 l h f')
            font = pdf.get_new_xref()
            pdf.update_object(font, '<< /Type /Font /Subtype /Type3 /Name /T3 /FontBBox [0 0 600 700] '
                '/FontMatrix [.001 0 0 .001 0 0] /FirstChar 65 /LastChar 65 /Widths [600] '
                f'/Encoding << /Type /Encoding /Differences [65 /A] >> /CharProcs << /A {proc} 0 R >> /Resources <<>> >>')
            pdf.xref_set_key(resources, 'Font/T3', f'{font} 0 R')
        command = text_program if text_program is not None else (
            b'/T3 40 Tf (A) Tj' if type3 else b'/helv 20 Tf (Clipped text) Tj')
        stream = pdf.get_new_xref(); pdf.update_object(stream, '<<>>')
        pdf.update_stream(stream, page.read_contents() + b'q ' + clip.encode('ascii') +
            b' re W n ' + (b'/SM gs ' if mask else b'') +
            b'BT 1 0 0 1 20 120 Tm ' + command + b' ET Q')
        page.set_contents(stream); pdf.save(path)


def type3_curve_pdf(path, case='ellipse'):
    ellipse=(b'600 0 0 0 600 700 d1 300 0 m '
             b'465.685425 0 600 156.700675 600 350 c '
             b'600 543.299325 465.685425 700 300 700 c '
             b'134.314575 700 0 543.299325 0 350 c '
             b'0 156.700675 134.314575 0 300 0 c h f')
    programs={'ellipse':ellipse,
              'lens':b'600 0 0 0 600 700 d1 300 0 m 600 0 600 700 300 700 c 0 700 0 0 300 0 c h f',
              'closed-cubic':b'600 0 0 0 600 700 d1 300 0 m 600 700 0 700 300 0 c h f',
              'retraced-line':b'600 0 0 0 600 700 d1 0 0 m 600 0 l 300 700 l 0 0 l 600 0 l 300 700 l h f',
              'retraced-curve':ellipse.replace(b'h f',ellipse.split(b'300 0 m ',1)[1]),
              'unsafe-curve':ellipse.replace(b'465.685425 0',b'-300 700',1),
              'masked':ellipse,'partial':ellipse,'one-edge-top':ellipse,
              'one-edge-left':ellipse,'one-edge-masked':ellipse,
              'two-edge-corner':ellipse,'two-edge-masked':ellipse,
              'coloured':ellipse.replace(b'600 0 0 0 600 700 d1',b'600 0 d0 1 0 0 rg')}
    clips = {'partial': '20 110 12 30', 'one-edge-top': '12 100 38 36',
             'one-edge-left': '26 100 30 60', 'one-edge-masked': '12 100 38 36',
             'two-edge-corner': '26 100 30 36', 'two-edge-masked': '26 100 30 36'}
    clipped_sequence_pdf(path, clips.get(case, '0 0 360 240'),
                         type3=True, mask='gradient' if case == 'two-edge-masked' else case in {'masked', 'one-edge-masked'},
                         type3_program=programs[case])


def type3_topology_pdf(path, case='concave'):
    outer=b'0 0 m 600 0 l 600 700 l 0 700 l h '
    hole=b'100 100 m 100 600 l 500 600 l 500 100 l h '
    inner=b'100 100 m 500 100 l 500 600 l 100 600 l h '
    concave=b'0 0 m 600 0 l 300 100 l 600 700 l 0 700 l h '
    programs={'concave':concave,'hole':outer+hole,'same-winding':outer+inner,
              'island':outer+hole+b'200 200 m 400 200 l 400 500 l 200 500 l h ',
              'disjoint':b'0 0 m 250 0 l 250 700 l 0 700 l h 350 0 m 600 0 l 600 700 l 350 700 l h ',
              'touching':outer+b'0 100 m 0 600 l 500 600 l 500 100 l h ',
              'crossing':b'0 0 m 300 700 l 600 0 l 0 450 l 600 450 l h ',
              'retraced':b'0 0 m 600 0 l 300 700 l 0 0 l 600 0 l 300 700 l h ',
              'masked':concave,'partial':concave}
    clipped_sequence_pdf(path,'20 110 12 30'if case=='partial'else'0 0 360 240',type3=True,mask=case=='masked',
                         type3_program=b'600 0 0 0 600 700 d1 '+programs[case]+b'f')


def type3_compound_curve_pdf(path, case='hole'):
    def ellipse(cx, cy, rx, ry, reverse=False):
        k = .55228475
        segments = [((cx+rx,cy),(cx+rx,cy+k*ry),(cx+k*rx,cy+ry),(cx,cy+ry)),
                    ((cx,cy+ry),(cx-k*rx,cy+ry),(cx-rx,cy+k*ry),(cx-rx,cy)),
                    ((cx-rx,cy),(cx-rx,cy-k*ry),(cx-k*rx,cy-ry),(cx,cy-ry)),
                    ((cx,cy-ry),(cx+k*rx,cy-ry),(cx+rx,cy-k*ry),(cx+rx,cy))]
        if reverse:
            segments = [tuple(reversed(s)) for s in reversed(segments)]
        point = lambda p: f'{p[0]:.8f} {p[1]:.8f}'
        return (point(segments[0][0])+' m '+ ' '.join(' '.join(point(p) for p in s[1:])+' c' for s in segments)+' h ').encode('ascii')
    outer = ellipse(300,350,280,330)
    hole = ellipse(300,350,80,110,True)
    programs = {'hole':outer+hole,'same-winding':outer+ellipse(300,350,80,110),
                'island':outer+hole+ellipse(300,350,20,30),
                'disjoint':ellipse(120,350,100,300)+ellipse(480,350,100,300),
                'mixed':b'0 0 m 600 0 l 600 700 l 0 700 l h '+hole,
                'touching':outer+ellipse(500,350,80,110,True),
                'overlap':outer+ellipse(450,350,200,110,True),
                'ambiguous':outer+ellipse(300,350,240,280,True),
                'refined-same-winding':outer+ellipse(300,350,240,280),
                'nested-lens':b'300 20 m 580 20 580 680 300 680 c 20 680 20 20 300 20 c h '+hole,
                'near-touching':outer+ellipse(499.99999,350,80,110,True),
                'masked':outer+hole,'masked-gradient':outer+ellipse(300,350,240,280,True),
                'partial':outer+hole,'masked-tiled':outer+hole,'masked-overlap':outer+hole,
                'masked-gap':outer+hole,'masked-different':outer+hole,
                'masked-disjoint-glyphs':outer+hole,'masked-overlapping-glyphs':outer+hole,
                'masked-touching-glyphs':outer+hole,'masked-near-glyphs':outer+hole,
                'nonconvex':b'0 0 m 600 0 l 600 250 l 400 250 l 300 250 300 350 300 400 c 300 700 l 0 700 l h ',
                'masked-nonconvex':b'0 0 m 600 0 l 600 250 l 400 250 l 300 250 300 350 300 400 c 300 700 l 0 700 l h '}
    mask_kind={'masked-gradient':'gradient','masked-tiled':'tiled','masked-overlap':'tile-overlap',
               'masked-gap':'tile-gap','masked-different':'tile-different'}.get(
                   case,case in {'masked','masked-nonconvex','masked-disjoint-glyphs',
                                 'masked-overlapping-glyphs','masked-touching-glyphs','masked-near-glyphs'})
    clipped_sequence_pdf(path,'20 110 12 30' if case=='partial' else '0 0 360 240',
                         type3=True,mask=mask_kind,
                         type3_program=b'600 0 0 0 600 700 d1 '+programs[case]+b'f')
    multiple = {'masked-disjoint-glyphs': b'(AA) Tj',
                'masked-overlapping-glyphs': b'[(A) 300 (A)] TJ',
                'masked-touching-glyphs': b'[(A) 40 (A)] TJ',
                'masked-near-glyphs': b'[(A) 39.9 (A)] TJ'}
    if case in multiple:
        with fitz.open(path) as pdf:
            contents = pdf[0].get_contents()
            for xref in contents:
                pdf.update_stream(xref, pdf.xref_stream(xref).replace(b'(A) Tj', multiple[case]))
            pdf.saveIncr()


def type3_disjoint_curve_pdf(path, case='disjoint'):
    """Concave compound ink and unsafe neighbours in one Type3 fill paint."""
    def rounded_l(dx=0, reverse=False, scale=1, dy=0):
        segments=[[(0,0),(250,0)],[(250,0),(250,250)],[(250,250),(150,250)],
                  [(150,250),(100,250),(100,350),(100,400)],
                  [(100,400),(100,700)],[(100,700),(0,700)],[(0,700),(0,0)]]
        if reverse:segments=[list(reversed(s))for s in reversed(segments)]
        point=lambda p:f'{p[0]*scale+dx:.8f} {p[1]*scale+dy:.8f}'
        return (point(segments[0][0])+' m '+ ' '.join(
            ' '.join(point(p)for p in s[1:])+(' c'if len(s)==4 else' l')for s in segments)+' h ').encode('ascii')
    left=rounded_l()
    programs={'disjoint':left+rounded_l(350),'reversed':left+rounded_l(350,True),
              'mixed':left+b'350 0 m 600 0 l 600 700 l 350 700 l h ',
              'masked':left+rounded_l(350),'gradient':left+rounded_l(350),
              'partial':left+rounded_l(350),'touching':left+rounded_l(250),
              'near-touching':left+rounded_l(250.000001),'overlap':left+rounded_l(200),
              'nested':left+b'20 50 m 70 50 l 70 150 l 20 150 l h ',
              'nested-hole':left+b'20 50 m 20 150 l 70 150 l 70 50 l h ',
              'curved-hole':left+rounded_l(20,True,.2,20),
              'curved-same':left+rounded_l(20,False,.2,20),
              'curved-island':left+rounded_l(20,True,.2,20)+b'30 30 m 40 30 l 40 40 l 30 40 l h ',
              'pocket':left+b'160 400 m 220 400 l 220 600 l 160 600 l h ',
              'crossing':left+rounded_l(350).replace(b'450.00000000 250.00000000',b'1500 900',1)}
    clipped_sequence_pdf(path,'20 110 12 30'if case=='partial'else'0 0 360 240',type3=True,
                         mask='gradient'if case=='gradient'else case=='masked',
                         type3_program=b'600 0 0 0 600 700 d1 '+programs[case]+b'f')


def single_line_cells_pdf(path, *, row_height=14, empty=False):
    from magic_converter.core.pdf.pdf_fonts import resolve_font
    face = resolve_font('Arial')
    if face is None:
        raise RuntimeError('Arial is required for the single-line cell control')
    with fitz.open() as pdf:
        page = pdf.new_page(width=360, height=240)
        page.insert_font(fontname='local', fontfile=str(face.path))
        for x in (20, 150, 280):
            page.draw_line((x, 30), (x, 30 + 2 * row_height), width=.5)
        for y in (30, 30 + row_height, 30 + 2 * row_height):
            page.draw_line((20, y), (280, y), width=.5)
        if not empty:
            for row in range(2):
                for col in range(2):
                    page.insert_text((25 + col * 130, 30 + min(30, row_height - 4) + row * row_height),
                                     f'Cell {row + 1}{col + 1} gjpq', fontsize=10, fontname='local')
        page.insert_text((20, 210), 'Independent title', fontsize=12)
        pdf.save(path)


def opaque_overlap_pdf(path, *, opacity=1, overlap=True, text='WWWW', clipped=False, nested_counter=False, tracking=-45):
    """An ordinary title and stretched glyphs with optional tight spacing."""
    with fitz.open() as pdf:
        page = pdf.new_page(width=400, height=240)
        page.insert_text((210, 30), 'Independent title', fontsize=12)
        origin = fitz.Point(45, 135)
        page.insert_text(origin, text, fontsize=65, morph=(origin, fitz.Matrix(.7, 1)),
                         fill_opacity=opacity)
        stream = page.get_contents()[-1]
        content = pdf.xref_stream(stream)
        if overlap:
            content = content.replace(b'BT', f'BT {tracking:g} Tc'.encode('ascii'))
        if nested_counter:
            # Two glyph boxes intersect, but the small O is entirely inside
            # the large O's unpainted counter in the same text paint.
            content = (b'q .7 0 0 1 13.5 0 cm BT /helv 65 Tf '
                       b'1 0 0 1 45 105 Tm (O) Tj /helv 10 Tf '
                       b'1 0 0 1 65 118 Tm (O) Tj ET Q')
        if clipped:
            content = b'q 60 0 300 240 re W n\n' + content + b'\nQ'
        pdf.update_stream(stream, content)
        pdf.save(path)

