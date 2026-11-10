"""Create genuine DrawingML charts with embedded, editable workbooks."""

from __future__ import annotations

import io

from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml.xmlchemy import OxmlElement
from pptx.oxml.ns import qn
from pptx.util import Pt

from magic_converter.core.pdf.pdf_charts import PdfChart, ChartLabel


def _el(tag, val=None, **attributes):
    element=OxmlElement(tag)
    if val is not None:element.set('val',str(val))
    for name,value in attributes.items():element.set(name,str(value))
    return element


def _fill(parent, color):
    fill=_el('a:solidFill');fill.append(_el('a:srgbClr',color));parent.append(fill)


def _line(parent, color=None, width=.5, dash=False):
    line=_el('a:ln',w=Pt(width))
    if color:_fill(line,color)
    else:line.append(_el('a:noFill'))
    if dash:line.append(_el('a:prstDash','dash'))
    parent.append(line)


def _sp(color=None, line_color=None, width=.5, dash=False):
    properties=_el('c:spPr')
    if color:_fill(properties,color)
    else:properties.append(_el('a:noFill'))
    _line(properties,line_color,width,dash)
    return properties


def _text(style: ChartLabel, scale, tag='c:txPr'):
    text=_el(tag);text.append(_el('a:bodyPr'));text.append(_el('a:lstStyle'))
    paragraph=_el('a:p');properties=_el('a:pPr')
    run=_el('a:defRPr',sz=round(style.size*scale*100),b=int(style.bold))
    _fill(run,style.color)
    run.append(_el('a:latin',typeface=style.font));run.append(_el('a:ea',typeface=style.font))
    properties.append(run);paragraph.append(properties);paragraph.append(_el('a:endParaRPr'))
    text.append(paragraph)
    return text


def _layout(bounds, outer, inner=False):
    x0,y0,x1,y1=outer
    left,top,right,bottom=bounds
    layout=_el('c:layout');manual=_el('c:manualLayout')
    if inner:manual.append(_el('c:layoutTarget','inner'))
    for name,val in (('xMode','edge'),('yMode','edge'),('wMode','factor'),('hMode','factor'),
                     ('x',(left-x0)/(x1-x0)),('y',(top-y0)/(y1-y0)),
                     ('w',(right-left)/(x1-x0)),('h',(bottom-top)/(y1-y0))):
        manual.append(_el('c:'+name,val))
    layout.append(manual)
    return layout


def _category_axis(source, cat_id, value_id, scale, hidden=False):
    axis=_el('c:catAx');axis.append(_el('c:axId',cat_id))
    scaling=_el('c:scaling');scaling.append(_el('c:orientation','maxMin' if source.horizontal else 'minMax'))
    axis.append(scaling);axis.append(_el('c:delete',int(hidden)))
    axis.append(_el('c:axPos','l' if source.horizontal else 'b'))
    axis.append(_el('c:majorTickMark','none'));axis.append(_el('c:minorTickMark','none'))
    axis.append(_el('c:tickLblPos','low'));axis.append(_sp(line_color=source.axis_color))
    axis.append(_text(source.category_font or source.font,scale));axis.append(_el('c:crossAx',value_id))
    axis.append(_el('c:crosses','autoZero'))
    axis.append(_el('c:auto',1));axis.append(_el('c:lblAlgn','ctr'));axis.append(_el('c:lblOffset',35))
    axis.append(_el('c:noMultiLvlLbl',1))
    return axis


def _value_axis(source, value, value_id, cat_id, scale, secondary=False):
    axis=_el('c:valAx');axis.append(_el('c:axId',value_id))
    scaling=_el('c:scaling');scaling.append(_el('c:orientation','minMax'))
    scaling.append(_el('c:max',value.maximum));scaling.append(_el('c:min',value.minimum))
    axis.append(scaling);axis.append(_el('c:delete',0))
    axis.append(_el('c:axPos','b' if source.horizontal else 'r' if secondary else 'l'))
    if source.grid_color and not secondary:
        grid=_el('c:majorGridlines');grid.append(_sp(line_color=source.grid_color,width=source.grid_width*scale,dash=source.grid_dash));axis.append(grid)
    axis.append(_el('c:numFmt',formatCode=value.number_format,sourceLinked=0))
    axis.append(_el('c:majorTickMark','none'));axis.append(_el('c:minorTickMark','none'))
    axis.append(_el('c:tickLblPos','nextTo'));axis.append(_sp(line_color=source.value_axis_color))
    axis.append(_text(value.labels[0],scale));axis.append(_el('c:crossAx',cat_id))
    axis.append(_el('c:crosses','max' if secondary or source.horizontal else 'autoZero'))
    axis.append(_el('c:crossBetween','between' if source.between_categories else 'midCat'))
    axis.append(_el('c:majorUnit',value.major))
    return axis


def _data_labels(series, scale):
    if not any(series.labels):return None
    labels=_el('c:dLbls')
    for i,label in enumerate(series.labels):
        datum=_el('c:dLbl');datum.append(_el('c:idx',i))
        if label is None:
            datum.append(_el('c:delete',1))
        else:
            datum.append(_el('c:numFmt',formatCode=series.number_format,sourceLinked=0))
            datum.append(_text(label,scale))
            datum.append(_el('c:dLblPos','t' if series.kind=='line' else series.label_position))
            for name in ('showLegendKey','showVal','showCatName','showSerName','showPercent','showBubbleSize'):
                datum.append(_el('c:'+name,int(name=='showVal')))
        labels.append(datum)
    labels.append(_el('c:numFmt',formatCode=series.number_format,sourceLinked=0))
    for name in ('showLegendKey','showVal','showCatName','showSerName','showPercent','showBubbleSize'):
        labels.append(_el('c:'+name,int(name=='showVal')))
    return labels


def add_chart(slide, source: PdfChart, scale, offset_x, offset_y, page_number):
    data=CategoryChartData()
    data.categories=source.categories
    for series in source.series:data.add_series(series.name,series.values,number_format=series.number_format)
    initial_type=XL_CHART_TYPE.BAR_CLUSTERED if source.horizontal else XL_CHART_TYPE.COLUMN_CLUSTERED
    if all(s.kind=='line' for s in source.series):initial_type=XL_CHART_TYPE.LINE
    x0,y0,x1,y1=source.bbox
    shape=slide.shapes.add_chart(initial_type,Pt(offset_x+x0*scale),Pt(offset_y+y0*scale),
                                 Pt((x1-x0)*scale),Pt((y1-y0)*scale),data)
    shape.name='PDF recovered chart'
    chart=shape.chart
    chart.has_title=False
    space=chart._chartSpace
    for tag in ('c:style','c:spPr','c:txPr'):
        for old in list(space.findall(qn(tag))):space.remove(old)
    next_element=next((child for child in space if child.tag in {qn('c:externalData'),qn('c:printSettings'),qn('c:userShapes'),qn('c:extLst')}),None)
    insertion=len(space) if next_element is None else list(space).index(next_element)
    space.insert(insertion,_sp());space.insert(insertion+1,_text(source.font,scale))
    plot=space.chart.plotArea
    for child in list(plot):
        if child.tag not in {qn('c:barChart'),qn('c:lineChart')}:plot.remove(child)
    initial_plot=plot[0]
    raw_series=list(initial_plot.findall(qn('c:ser')))
    plot.remove(initial_plot)
    plot.append(_layout(source.plot,source.bbox,True))
    cat_id,value_id,second_cat_id,second_value_id=101,102,103,104
    plots={}
    for kind,secondary in dict.fromkeys((s.kind,s.secondary) for s in source.series):
        element=_el('c:barChart' if kind=='bar' else 'c:lineChart')
        if kind=='bar':
            element.append(_el('c:barDir','bar' if source.horizontal else 'col'))
        element.append(_el('c:grouping','clustered' if kind=='bar' else 'standard'))
        element.append(_el('c:varyColors',0))
        plots[kind,secondary]=element
        plot.append(element)
    for i,(series,element) in enumerate(zip(source.series,raw_series)):
        for child in list(element):
            if child.tag not in {qn('c:idx'),qn('c:order'),qn('c:tx'),qn('c:cat'),qn('c:val')}:element.remove(child)
        insert=3
        if series.kind=='bar':
            element.insert(insert,_sp(color=series.colors[0]));insert+=1
            element.insert(insert,_el('c:invertIfNegative',0));insert+=1
            for j,color in enumerate(series.colors):
                point=_el('c:dPt');point.append(_el('c:idx',j));point.append(_el('c:invertIfNegative',0));point.append(_sp(color=color));element.insert(insert,point);insert+=1
        else:
            element.insert(insert,_sp(line_color=series.colors[0],width=series.line_width*scale));insert+=1
            marker=_el('c:marker');marker.append(_el('c:symbol','circle' if series.marker_size else 'none'))
            if series.marker_size:
                marker.append(_el('c:size',max(2,min(72,round(series.marker_size*scale)))))
                marker.append(_sp(series.marker_fill,series.colors[0],.6*scale))
            element.insert(insert,marker);insert+=1
        labels=_data_labels(series,scale)
        if labels is not None:element.insert(insert,labels)
        if series.kind=='line':element.append(_el('c:smooth',0))
        plots[series.kind,series.secondary].append(element)
    for (kind,secondary),element in plots.items():
        if kind=='bar':
            count=sum(s.kind=='bar' for s in source.series)
            gap=max(0,min(500,round(count*(1/max(.1,source.bar_fraction)-1)*100)))
            element.append(_el('c:gapWidth',gap));element.append(_el('c:overlap',0))
        element.append(_el('c:axId',second_cat_id if secondary else cat_id))
        element.append(_el('c:axId',second_value_id if secondary else value_id))
    plot.append(_category_axis(source,cat_id,value_id,scale))
    plot.append(_value_axis(source,source.primary,value_id,cat_id,scale))
    if source.secondary is not None:
        plot.append(_category_axis(source,second_cat_id,second_value_id,scale,True))
        plot.append(_value_axis(source,source.secondary,second_value_id,second_cat_id,scale,True))
    plot.append(_sp())
    chart.has_legend=source.legend is not None
    if source.legend:
        legend=space.chart.legend
        for child in list(legend):legend.remove(child)
        legend.append(_el('c:legendPos','t'));legend.append(_layout(source.legend,source.bbox))
        legend.append(_el('c:overlay',1));legend.append(_sp());legend.append(_text(source.legend_font or source.font,scale))
    # The workbook is real chart data. Additional provenance is deliberately
    # kept outside the plotted range, with every estimated value identified.
    from openpyxl import load_workbook
    workbook=load_workbook(io.BytesIO(chart.part.chart_workbook.xlsx_part.blob))
    provenance=workbook.create_sheet('PDF Recovery')
    provenance.append(['PDF page','Series','Category','Value','Source','Visible label'])
    for series in source.series:
        for category,value,estimated,label in zip(source.categories,series.values,series.estimated,series.labels):
            provenance.append([page_number,series.name,category,value,
                               'Estimated from vector geometry and axis ticks' if estimated else 'Read from visible PDF label',
                               label.text if label else ''])
    provenance.column_dimensions['B'].width=35;provenance.column_dimensions['C'].width=30
    provenance.column_dimensions['E'].width=55
    buffer=io.BytesIO();workbook.save(buffer)
    chart.part.chart_workbook.update_from_xlsx_blob(buffer.getvalue())
    estimated=sum(sum(s.estimated) for s in source.series)
    shape._element.nvGraphicFramePr.cNvPr.set('descr',f'Chart reconstructed from PDF page {page_number}. {estimated} values estimated; see embedded workbook PDF Recovery sheet.')
    return shape
