"""Read printer geometry for diagnostics without changing a printer or printing."""
import os
import math


def default_printer_geometry():
    if os.name!='nt':return {'status':'unavailable','reason':'Windows printer DC required'}
    import win32print,win32ui
    try:
        name=win32print.GetDefaultPrinter();dc=win32ui.CreateDC();dc.CreatePrinterDC(name)
        try:
            caps={key:dc.GetDeviceCaps(index)for key,index in [('dpi_x',88),('dpi_y',90),('physical_width',110),('physical_height',111),('offset_x',112),('offset_y',113)]}
        finally:dc.DeleteDC()
        if min(caps['dpi_x'],caps['dpi_y'])<=0:return {'status':'unavailable','reason':'Invalid device resolution'}
        return {'status':'measured','printer':name,'caps':caps,'hard_margin_pt':[72*caps['offset_x']/caps['dpi_x'],72*caps['offset_y']/caps['dpi_y']],
                'changed_default_printer':False,'created_print_job':False}
    except Exception as e:
        return {'status':'unavailable','reason':type(e).__name__}


def predicted_native_origin(origin,source_size,native_size,hard_margin):
    if (any(len(v)!=2 for v in (origin,source_size,native_size,hard_margin))
            or not all(math.isfinite(n)for v in (origin,source_size,native_size,hard_margin)for n in v)
            or min(*source_size,*native_size)<=0):raise ValueError('Invalid page geometry')
    return [(point-offset)*after/before for point,offset,before,after in zip(origin,hard_margin,source_size,native_size)]
