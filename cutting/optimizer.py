"""Deterministic shelf-based rectangular nesting for MDF cutting."""
import xml.etree.ElementTree as ET

NS = 'http://www.King-stone.com'
ET.register_namespace('', NS)

def optimize(parts, stock, kerf=4):
    bins = sorted(stock, key=lambda s: (not s.get('is_offcut', False), s['width']*s['height']))
    remaining = sorted((dict(p) for p in parts), key=lambda p: p['width']*p['height'], reverse=True)
    layouts = []
    for sheet in bins:
        usable_width=max(0, sheet['width']-10)
        usable_height=max(0, sheet['height']-10)
        placed=[]; left=[]; x=y=row_h=0
        for p in remaining:
            candidates=[(p['width'],p['height'],False)]
            if p.get('turn',True) and p['width']!=p['height']:
                candidates.append((p['height'],p['width'],True))
            # Try current shelf first, then a new shelf; choose the best fitting orientation.
            options=[]
            for w,h,rot in candidates:
                if y+h<=usable_height and x+w<=usable_width:
                    options.append((h,w,rot,x,y,False))
                ny=y+row_h+(kerf if placed else 0)
                if ny+h<=usable_height and w<=usable_width:
                    options.append((h,w,rot,0,ny,True))
            if not options:
                left.append(p); continue
            h,w,rot,nx,ny,newrow=min(options,key=lambda a:(a[0],a[1],a[2]))
            if newrow:
                x=0; y=ny; row_h=0
            placed.append({**p,'x':nx,'y':ny,'width':w,'height':h,'rotated':rot,'row':y})
            x=nx+w+kerf; row_h=max(row_h,h)
        if placed:
            layouts.append({'sheet':sheet,'parts':placed})
        remaining=left
        if not remaining: break
    if remaining:
        p=remaining[0]
        raise ValueError(f"برای قطعه «{p['label']}» ({p['width']}×{p['height']}) ورق قابل استفاده/موجودی کافی نیست.")
    return layouts

def make_autocut(plan_id, material_rows, kerf=4):
    root=ET.Element(f'{{{NS}}}AutoCUT',{'ver':'500'})
    project=ET.SubElement(root,f'{{{NS}}}Project',{'Name':f'CraftFlow-{plan_id}','Selected':'0','Update':'0','DefaultLevel':'0','UserFields':'F2,F3,F4,F5,F26,F18,F19,F20,','FieldLabels':'TLGrain=Grain,TLOrder=Order,TLType=fcfffff,F2=Part,F3=Barcode,F19=Routing,F20=Customer,'})
    for row in material_rows:
        data=ET.SubElement(project,f'{{{NS}}}Data',{'Class':'3','TotalUnit':'1000000','Type':row['type'],'Ply':str(row['thickness'])})
        objective=ET.SubElement(data,f'{{{NS}}}Objective',{'Type':'Shape','Count':str(len(row['shapes']))})
        for i,p in enumerate(row['shapes']):
            ET.SubElement(objective,f'{{{NS}}}Shape',{'Name':f'P{i+1:03d}','X':str(p['orig_width']),'Y':str(p['orig_height']),'Turn':'true' if p.get('turn',True) else 'false','Grain':p.get('grain',''),'Order':p.get('order',''),'Count':'1','F2':p.get('f2',''),'F3':p.get('f3',''),'F19':p.get('routing',''),'F20':p.get('customer','')})
        solutions=ET.SubElement(data,f'{{{NS}}}Solutions',{'Selected':'0'})
        total_area=sum(s['sheet']['width']*s['sheet']['height'] for s in row['sheets'])
        used_area=sum(p['width']*p['height'] for s in row['sheets'] for p in s['parts'])
        sol=ET.SubElement(solutions,f'{{{NS}}}Solution',{'SolveGap':str(kerf),'AlgCase':'1','AlgNest':'21','SolveRate':'10','UnitCounts':','.join('1' for _ in row['shapes'])+',','SheetCounts':','.join('1' for _ in row['sheets'])+',','SumUnitArea':str(used_area),'SumSheetArea':str(total_area)})
        layouts=ET.SubElement(sol,f'{{{NS}}}Layouts')
        for idx,s in enumerate(row['sheets']):
            sheet=s['sheet']; area=sum(p['width']*p['height'] for p in s['parts'])
            uids=','.join(str(p['uid']) for p in s['parts'])+','
            layout=ET.SubElement(layouts,f'{{{NS}}}Layout',{'ID':str(idx),'SheetID':str(idx),'Count':'1','Cut':'true','Side':'5','SheetName':sheet.get('label',row['type']),'X':str(sheet['width']),'Y':str(sheet['height']),'SX':'5','SY':'5','UIDS':uids,'UnitArea':str(area),'SheetArea':str(sheet['width']*sheet['height']),'SpareArea':str(sheet['width']*sheet['height']-area)})
            base=ET.SubElement(layout,f'{{{NS}}}Part',{'X':str(sheet['width']),'Y':str(sheet['height']),'Px':'5','Py':'5','Side':'5'})
            for p in s['parts']:
                attrs={'X':str(p['width']),'Y':str(p['height']),'Px':str(p['x']+5),'Py':str(p['y']+5),'UID':str(p['uid'])}
                if p['rotated']: attrs['Angle']='90'
                ET.SubElement(base,f'{{{NS}}}Part',attrs)
    return ET.tostring(root,encoding='utf-16',xml_declaration=True)
