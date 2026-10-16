from collections import defaultdict
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from product.models import Material, Order, ProductionTask
from .models import SheetStock, CutPlan
from .optimizer import optimize, make_autocut

@login_required
def planner(request):
    materials = Material.objects.select_related('raw_material').all().order_by('name')
    if request.method == 'POST' and request.POST.get('action') == 'stock':
        material=get_object_or_404(Material,pk=request.POST.get('material'))
        try:
            w=int(request.POST.get('width')); h=int(request.POST.get('height')); qty=int(request.POST.get('quantity',1))
            if min(w,h,qty)<1: raise ValueError
        except (TypeError,ValueError):
            messages.error(request,'ابعاد و تعداد باید عدد مثبت باشند.'); return redirect('cutting:planner')
        SheetStock.objects.create(material=material,width=w,height=h,quantity=qty,is_offcut=request.POST.get('is_offcut')=='on',grain=request.POST.get('grain',''))
        messages.success(request,'موجودی ورق ثبت شد.'); return redirect('cutting:planner')
    if request.method == 'POST' and request.POST.get('action') == 'plan':
        order_ids=request.POST.getlist('order_ids')
        orders=list(Order.objects.filter(pk__in=order_ids))
        if not orders: messages.error(request,'برای برنامه‌ریزی سفارشی انتخاب کنید.'); return redirect('cutting:planner')
        tasks=list(ProductionTask.objects.filter(order__in=orders,station_name='cut',status='pending',part__isnull=False).select_related('part__material','order','order__customer'))
        if not tasks: messages.error(request,'قطعه آماده برش در سفارش‌های انتخاب‌شده پیدا نشد.'); return redirect('cutting:planner')
        kerf=int(request.POST.get('kerf',4)); grouped=defaultdict(list)
        for task in tasks:
            part=task.part
            for _ in range(task.quantity):
                grouped[part.material_id].append({'width':int(part.length),'height':int(part.width),'orig_width':int(part.length),'orig_height':int(part.width),'turn':part.turn,'grain':part.grain,'f2':part.f2 or part.name,'f3':part.f3,'routing':part.routing_code,'order':str(task.order_id),'customer':str(task.order.customer),'label':f'{part.name} / سفارش {task.order_id}','uid':len(grouped[part.material_id])})
        rows=[]; reserve=[]
        try:
            for material_id,parts in grouped.items():
                material=Material.objects.get(pk=material_id)
                rows_stock=[]
                for stock in SheetStock.objects.filter(material=material,quantity__gt=0).order_by('-is_offcut','width','height'):
                    for _ in range(stock.quantity):
                        rows_stock.append({'width':stock.width,'height':stock.height,'is_offcut':stock.is_offcut,'label':f'{stock.width}x{stock.height}','stock_id':stock.pk})
                if not rows_stock:
                    raise ValueError(f'برای «{material.name}» موجودی ورق ثبت نشده است؛ ابتدا ابعاد ورق کامل را وارد کنید.')
                layouts=optimize(parts,rows_stock,kerf)
                used_ids=[s['sheet']['stock_id'] for s in layouts]
                reserve.extend(used_ids)
                rows.append({'type':material.name.replace(' ','-'),'thickness':str(material.thickness),'sheets':layouts,'shapes':parts,'material_id':material_id})
        except ValueError as exc:
            messages.error(request,str(exc)); return redirect('cutting:planner')
        plan=CutPlan.objects.create(created_by=request.user,plan_data={'rows':rows,'stock_ids':reserve,'kerf':kerf})
        plan.orders.set(orders)
        return redirect('cutting:plan_detail',plan_id=plan.pk)
    orders=Order.objects.filter(tasks__station_name='cut',tasks__status='pending').distinct().order_by('-id')
    stock=SheetStock.objects.select_related('material').all()
    return render(request,'cutting/planner.html',{'materials':materials,'orders':orders,'stock':stock})

@login_required
def plan_detail(request,plan_id):
    plan=get_object_or_404(CutPlan.objects.prefetch_related('orders'),pk=plan_id)
    summaries=[]
    for row in plan.plan_data['rows']:
        summaries.append({'material':row['type'],'thickness':row['thickness'],'sheet_count':len(row['sheets']),'part_count':len(row['shapes']),'sheets':row['sheets']})
    return render(request,'cutting/plan_detail.html',{'plan':plan,'summaries':summaries})

@login_required
def download_cut(request,plan_id):
    from django.http import HttpResponse
    plan=get_object_or_404(CutPlan,pk=plan_id)
    rows=[]
    for row in plan.plan_data['rows']:
        # UID references the matching shape in Objective, stable shape index per material.
        rows.append(row)
    payload=make_autocut(plan.pk,rows,plan.plan_data.get('kerf',4))
    response=HttpResponse(payload,content_type='application/xml; charset=utf-16')
    response['Content-Disposition']=f'attachment; filename="craftflow_cut_plan_{plan.pk}.CUT"'
    return response

@login_required
@require_POST
def mark_cut(request,plan_id):
    with transaction.atomic():
        plan=get_object_or_404(CutPlan.objects.select_for_update(),pk=plan_id)
        if plan.status=='cut':
            messages.info(request,'این برنامه قبلاً به‌عنوان برش‌شده ثبت شده است.'); return redirect('cutting:plan_detail',plan_id=plan.pk)
        ids=plan.plan_data.get('stock_ids',[])
        stock_rows=list(SheetStock.objects.select_for_update().filter(pk__in=ids))
        from collections import Counter
        needed=Counter(ids)
        if len(stock_rows) != len(needed) or any(s.quantity<needed[s.pk] for s in stock_rows):
            messages.error(request,'موجودی ورق از زمان برنامه‌ریزی تغییر کرده؛ برنامه را دوباره بسازید.'); return redirect('cutting:planner')
        tasks = list(ProductionTask.objects.filter(order__in=plan.orders.all(),station_name='cut',status='pending'))
        if not tasks:
            messages.error(request,'تسک‌های این سفارش قبلاً انجام یا تغییر داده شده‌اند؛ برنامه را دوباره بررسی کنید.'); return redirect('cutting:planner')
        for s in stock_rows:
            s.quantity-=needed[s.pk]
            if s.quantity: s.save(update_fields=['quantity'])
            else: s.delete()
        # Convert non-overlapping right-of-shelf and bottom-of-sheet rectangles to offcut stock.
        for row in plan.plan_data['rows']:
            for layout in row['sheets']:
                sheet=layout['sheet']; parts=layout['parts']
                shelves={}
                for p in parts:
                    shelves.setdefault(p['row'],[]).append(p)
                max_bottom=0
                for shelf_y,shelf_parts in shelves.items():
                    row_h=max(p['height'] for p in shelf_parts)
                    max_bottom=max(max_bottom,shelf_y+row_h)
                    used_w=max(p['x']+p['width'] for p in shelf_parts)
                    rw=sheet['width']-used_w-plan.plan_data.get('kerf',4)-5
                    if rw>=200 and row_h>=200:
                        SheetStock.objects.create(material_id=row['material_id'],width=rw,height=row_h,quantity=1,is_offcut=True)
                bottom_h=sheet['height']-max_bottom-plan.plan_data.get('kerf',4)-5
                if sheet['width']>=200 and bottom_h>=200:
                    SheetStock.objects.create(material_id=row['material_id'],width=sheet['width'],height=bottom_h,quantity=1,is_offcut=True)
        plan.status='cut'; plan.cut_at=timezone.now(); plan.save(update_fields=['status','cut_at'])
        # Keep production tasks pending until the operator confirms the actual cut here.
        for task in tasks:
            task.status='done'; task.scanned_by=request.user; task.save()
    messages.success(request,'برش ثبت شد؛ ورق‌های مصرف‌شده کسر و پرت‌های قابل‌استفاده به موجودی اضافه شدند.')
    return redirect('cutting:plan_detail',plan_id=plan.pk)
