import os
import xhtml2pdf.pisa as pisa
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Use the converted Vazirmatn TTF shipped in the project
font_path = os.path.abspath("static/fonts/Vazirmatn-Regular.ttf")
pdfmetrics.registerFont(TTFont("vazir", font_path))
print("registered font from", font_path, "exists", os.path.exists(font_path))

def link_callback(uri, rel):
    if uri.startswith("/static/"):
        return os.path.join(os.getcwd(), "static", uri[len("/static/"):])
    if uri.startswith("/media/"):
        return os.path.join(os.getcwd(), "media", uri[len("/media/"):])
    return uri

css = """
body { font-family: vazir; direction: rtl; text-align: right; margin: 10px; }
h1 { font-size: 16pt; }
table { width: 100%; border-collapse: collapse; }
td, th { border: 1px solid #333; padding: 4px; text-align: center; }
"""

html = (
    '<html><head><meta charset="utf-8"><style>' + css + '</style></head><body>'
    '<h1>کاتالوگ محصولات فروشگاه سلوی چوب</h1>'
    '<p>قیمت واحد: ۱۵۰,۰۰۰ ریال</p>'
    '<table><thead><tr><th>ردیف</th><th>محصول</th><th>قیمت</th></tr></thead>'
    '<tbody><tr><td>1</td><td>صندلی اداری</td><td>150,000</td></tr></tbody></table>'
    '</body></html>'
)

out = "C:/Users/Public/test_catalog_vazir.pdf"
with open(out, "wb") as f:
    result = pisa.CreatePDF(html, dest=f, link_callback=link_callback)
print("PDF written:", os.path.exists(out), "size:", os.path.getsize(out) if os.path.exists(out) else 0)
print("errors:", result.err)
