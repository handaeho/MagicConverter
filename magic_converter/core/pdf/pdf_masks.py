"""Prove bounded opaque gray rectangles cover a constant luminosity mask."""
import math
import pymupdf as fitz

m=fitz.mupdf


class UniformMask:
    def __init__(self,args,parent_alpha,nested):
        area,luminosity,colorspace,_,_=args
        self.area=fitz.Rect(fitz.JM_py_from_rect(area))
        self.parent_alpha=parent_alpha
        self.valid=(not nested and parent_alpha is not None and luminosity==1 and bool(colorspace)
                    and m.ll_fz_colorspace_is_device(colorspace) and m.ll_fz_colorspace_n(colorspace)==1
                    and not self.area.is_empty and all(math.isfinite(v)for v in self.area))
        self.paints=0;self.value=None;self.rectangles=[]

    def observe(self,name,args,clip,simple,rectangular):
        if not self.valid:return
        if name in {'begin_mask','begin_tile','clip_text','clip_stroke_text','clip_stroke_path','clip_image_mask'}:
            self.valid=False
        elif name=='begin_group':
            _,_,_,knockout,blendmode,alpha=args
            self.valid=not knockout and blendmode==0 and alpha==1
        elif name in {'fill_path','stroke_path','fill_text','stroke_text','ignore_text','fill_shade','fill_image','fill_image_mask'}:
            self.paints+=1
            if name!='fill_path' or self.paints>32:
                self.valid=False;return
            path,_,ctm,colorspace,color,alpha,_=args
            self.valid=(simple and alpha==1 and bool(colorspace) and m.ll_fz_colorspace_is_device(colorspace)
                        and m.ll_fz_colorspace_n(colorspace)==1 and rectangular(path,ctm)
                        and clip.contains(self.area))
            if self.valid:
                value=m.floats_getitem(color,0)
                self.valid=(math.isfinite(value)and 0<value<=1
                            and(self.value is None or value==self.value))
                self.value=value
                rect=fitz.Rect(fitz.JM_py_from_rect(m.ll_fz_bound_path(path,None,ctm)))&self.area
                if not rect.is_empty:self.rectangles.append(rect)

    def alpha(self,transfer):
        if self.valid and self.paints and len(transfer)==1 and transfer[0]is None:
            # Exact strip coverage, without an epsilon that could fill a
            # genuinely unpainted gap. At most 32 rectangles / 66 strip edges.
            xs=sorted({self.area.x0,self.area.x1,*(x for r in self.rectangles for x in(r.x0,r.x1))})
            for left,right in zip(xs,xs[1:]):
                intervals=sorted((r.y0,r.y1)for r in self.rectangles if r.x0<=left and r.x1>=right)
                cursor=self.area.y0
                for top,bottom in intervals:
                    if top>cursor:return None
                    cursor=max(cursor,bottom)
                if cursor<self.area.y1:return None
            return self.parent_alpha*self.value
        return None
