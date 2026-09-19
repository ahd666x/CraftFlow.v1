"""
Utility for rendering HTML templates to PDF using xhtml2pdf.

Supports RTL / Persian text (Vazirmatn font is registered from the
shipped TrueType files under ``static/fonts``) and resolves
``/static/`` and ``/media/`` references to on-disk paths so images and
fonts render inside the generated PDF.
"""
import os
import logging

from django.conf import settings
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.templatetags.static import static

import xhtml2pdf.pisa as pisa
import xhtml2pdf.default as xhtml_default
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.fonts import addMapping

logger = logging.getLogger(__name__)

_fonts_ready = False

# Maps the CSS ``font-family`` names used in our templates to the
# Vazirmatn sub-font file and the bold/italic flags that reportlab's
# ``ps2tt``/``tt2ps`` mapping machinery expects.
#
# We register the TTF under a unique pdfmetrics name (``<css_name>_00``),
# wire it through ``addMapping`` so ``ps2tt`` can resolve it (xhtml2pdf calls
# ``ps2tt`` on every font-family value), and publish the mapping in
# ``xhtml2pdf.default.DEFAULT_FONT`` so the xhtml2pdf ``fontList`` copy picks
# it up.  This avoids xhtml2pdf's ``@font-face`` code path which copies the
# font to a ``NamedTemporaryFile`` — that fails on Windows because the file
# is still open (locked) when reportlab tries to read it back.
_FONT_FILES = {
    "vazir-regular": "Vazirmatn-Regular.ttf",
    "vazir-bold": "Vazirmatn-Bold.ttf",
    "vazir-medium": "Vazirmatn-Medium.ttf",
    "vazir-semi-bold": "Vazirmatn-SemiBold.ttf",
    "vazir-light": "Vazirmatn-Light.ttf",
    "vazir-thin": "Vazirmatn-Thin.ttf",
    "vazir-extralight": "Vazirmatn-ExtraLight.ttf",
    "vazir-black": "Vazirmatn-Black.ttf",
}


def _register_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    font_dir = _find_font_dir()
    for css_name, ttf_name in _FONT_FILES.items():
        path = os.path.join(font_dir, ttf_name)
        if not os.path.isfile(path):
            # fall back to the staticfiles finder (e.g. when collected)
            path = _static_to_fs_path(static("fonts/%s" % ttf_name))
        if not os.path.isfile(path):
            logger.warning("Font file not found for %s (%s)", css_name, ttf_name)
            continue
        full_name = "%s_00" % css_name
        try:
            pdfmetrics.registerFont(TTFont(full_name, path))
            addMapping(css_name, 0, 0, full_name)
            xhtml_default.DEFAULT_FONT[css_name] = full_name
        except Exception:
            logger.warning("Could not register font %s", path)
    _fonts_ready = True


def _static_to_fs_path(path):
    if not path.startswith(settings.STATIC_URL):
        return path
    rel = path[len(settings.STATIC_URL):]
    try:
        from django.contrib.staticfiles.finders import find
        found = find(rel)
        return found or path
    except Exception:
        return path


def _find_font_dir():
    candidates = [
        os.path.join(settings.BASE_DIR, "static", "fonts"),
    ]
    static_dirs = getattr(settings, "STATICFILES_DIRS", [])
    for sd in static_dirs:
        candidates.append(os.path.join(str(sd), "fonts"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


def render_pdf(template_name, context, filename="catalog.pdf", link_callback=None):
    """Render *template_name* with *context* to an ``HttpResponse`` PDF."""
    _register_fonts()
    html = render_to_string(template_name, context)
    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="%s"' % filename
    if link_callback is None:
        link_callback = _link_callback
    pisa.CreatePDF(html, dest=response, link_callback=link_callback)
    return response


def _link_callback(uri, rel):
    """Resolve static / media URLs to filesystem paths for xhtml2pdf."""
    if uri.startswith(settings.MEDIA_URL) and settings.MEDIA_URL:
        path = os.path.join(settings.MEDIA_ROOT, uri[len(settings.MEDIA_URL):])
    elif uri.startswith(settings.STATIC_URL) and settings.STATIC_URL:
        rel_path = uri[len(settings.STATIC_URL):]
        try:
            from django.contrib.staticfiles.finders import find
            found = find(rel_path)
            path = found or os.path.join(settings.STATIC_ROOT or "", rel_path)
        except Exception:
            path = os.path.join(settings.STATIC_ROOT or "", rel_path)
    else:
        path = uri
    if not os.path.isabs(path):
        path = os.path.join(settings.BASE_DIR, path)
    return path
