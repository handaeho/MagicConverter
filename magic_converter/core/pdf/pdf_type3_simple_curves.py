"""Certify simple nonconvex contours using exact Bezier hull bounds.

Every arc is monotone along its endpoint chord. Nonadjacent hulls must be
strictly separated; adjacent arcs must lie on opposite sides through their
only common endpoint. Strictly separated arc hulls prove that compound
boundaries do not cross. Winding is then classified through a hull-preserving
homotopy to the endpoint polygon. Subdivision never changes output coordinates.
"""
import math
from magic_converter.core.jobs.job_control import check_cancelled


def simple_curve_items(items,max_checks):
    epsilon=1e-6;contours=[];originals=[];original=[];segments=[];first=last=None
    for item in items:
        check_cancelled()
        original.append(item)
        if item[0]=='close':
            if first is None:raise ValueError('Empty contour')
            if abs(last-first)>epsilon:segments.append((last,first))
            if len(segments)<3:raise ValueError('Degenerate simple contour')
            contours.append(segments);originals.append(original)
            segments=[];original=[];first=last=None
            continue
        if item[0]not in {'l','c'}or len(item)!=(3 if item[0]=='l'else 5):raise ValueError('Unsupported simple curve')
        points=tuple(item[1:])
        if any(not math.isfinite(v)for p in points for v in p):raise ValueError('Nonfinite curve')
        if last is not None and abs(points[0]-last)>epsilon:raise ValueError('Discontinuous curve')
        if first is None:first=points[0]
        direction=points[-1]-points[0]
        if abs(direction)<=epsilon:raise ValueError('Closed or degenerate arc')
        projections=[(p-points[0]).x*direction.x+(p-points[0]).y*direction.y for p in points]
        if any(b<a for a,b in zip(projections,projections[1:])):raise ValueError('Nonmonotone arc')
        segments.append(points);last=points[-1]
    if segments or not contours:raise ValueError('Unclosed simple contour')

    def dot(a,b):return a.x*b.x+a.y*b.y
    def axes(points):
        for n,p in enumerate(points):
            for q in points[:n]:
                d=p-q
                if abs(d)>epsilon:yield type(d)(-d.y,d.x)/abs(d)
    def separated(a,b):
        for normal in [type(a[0])(1,0),type(a[0])(0,1),*axes(a),*axes(b)]:
            x=[dot(p,normal)for p in a];y=[dot(p,normal)for p in b]
            if min(x)>max(y)+epsilon or min(y)>max(x)+epsilon:return True
        return False
    def adjacent(a,b,joint):
        u=sum((p-joint for p in a),type(joint)(0,0));v=sum((p-joint for p in b),type(joint)(0,0))
        if abs(u)<=epsilon or abs(v)<=epsilon:return False
        for normal in [u/abs(u)-v/abs(v),*axes(a),*axes(b)]:
            if abs(normal)<=epsilon:continue
            normal=normal/abs(normal)
            x=[dot(p-joint,normal)for p in a];y=[dot(p-joint,normal)for p in b]
            if ((min(x)>=0 and max(y)<=0 and max(x)>epsilon and min(y)<-epsilon)
                    or(min(y)>=0 and max(x)<=0 and max(y)>epsilon and min(x)<-epsilon)):return True
        return False
    def split(points):
        if len(points)==2:return [points]
        a,b,c,d=points;ab,bc,cd=(a+b)/2,(b+c)/2,(c+d)/2
        abc,bcd=(ab+bc)/2,(bc+cd)/2;middle=(abc+bcd)/2
        return [(a,ab,abc,middle),(middle,bcd,cd,d)]
    def orientation(segments):
        # Integrate x dy - y dx exactly for the line/cubic polynomial, using
        # a local origin to avoid cancellation from large page translations.
        origin=segments[0][0];area=0
        for segment in segments:
            p=[q-origin for q in segment]
            if len(p)==2:coefficients=[p[0],p[1]-p[0]]
            else:
                a,b,c,d=p
                coefficients=[a,(b-a)*3,(a-b*2+c)*3,d-a+(b-c)*3]
            for i,a in enumerate(coefficients):
                for j,b in enumerate(coefficients[1:],1):
                    area+=j*(a.x*b.y-a.y*b.x)/(i+j)
        extent=max(abs(p-origin)for s in segments for p in s)
        if not math.isfinite(area)or abs(area)<=epsilon*max(1,extent*extent):
            raise ValueError('Unproven curve orientation')
        return 1 if area>0 else -1

    def winding(point,segments):
        result=0
        for segment in segments:
            a,b=segment[0],segment[-1]
            if a.y<=point.y<b.y or b.y<=point.y<a.y:
                side=(b.x-a.x)*(point.y-a.y)-(b.y-a.y)*(point.x-a.x)
                if abs(side)<=epsilon*max(1,abs(b-a)):
                    raise ValueError('Unproven curve containment')
                if a.y<=point.y<b.y and side>0:result+=1
                if b.y<=point.y<a.y and side<0:result-=1
        if result not in {-1,0,1}:raise ValueError('Unproven simple curve winding')
        return result

    authored=contours
    remaining=max_checks
    for depth in range(5):
        count=sum(len(s)for contour in contours for s in contour);cost=count*count
        if cost>remaining:raise ValueError('Simple curve proof budget exceeded')
        remaining-=cost;certain=True
        for index,segments in enumerate(contours):
            check_cancelled()
            for other in contours[:index]:
                for a in segments:
                    for b in other:certain &= separated(a,b)
            for n,a in enumerate(segments):
                check_cancelled()
                for k,b in enumerate(segments[:n]):
                    if n-k==1:ok=adjacent(a,b,a[0])
                    elif k==0 and n==len(segments)-1:ok=adjacent(a,b,a[-1])
                    else:ok=separated(a,b)
                    certain &= ok
        if certain:
            if len(contours)==1:return items
            directions=[orientation(s)for s in authored]
            result=[]
            for index,segments in enumerate(contours):
                outer=0
                for previous,other in enumerate(contours):
                    if previous!=index:
                        value=winding(segments[0][0],other)
                        if value and value!=directions[previous]:raise ValueError('Inconsistent curve orientation')
                        outer+=value
                if (outer!=0)!=(outer+directions[index]!=0):result.extend(originals[index])
            return items if len(result)==len(items) else result
        if depth==4:break
        contours=[[half for s in segments for half in split(s)]for segments in contours]
    raise ValueError('Unproven simple curve separation')
